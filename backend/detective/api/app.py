"""FastAPI adapter. Each request owns a separate AsyncSession/transaction."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from detective.game_engine import commands as c
from detective.realtime.hub import Hub
from detective.realtime.protocol import Limits
from detective.realtime.transport import HandshakeGuard, serve

from . import board, catalog, import_service, service
from . import repository as repo
from .config import Settings
from .contracts import ActionIdentity, ActionRequest, Nickname, SelectCase, decode_command
from .db import Member, Room, make_engine, session_factory
from .rate_limit import HTTPRateLimiter
from .security import APIError, authenticate, csrf_token, new_session

MAX_REQUEST_BYTES = 32 * 1024
JSON_BODY_SECONDS = 10
SCHEMA_REVISION = "c6e20a4f9137"


def error_response(error: APIError) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": error.code, "message": error.code}}
    if isinstance(error, import_service.ImportErrorResponse):
        body["error"]["issues"] = error.issues
    if error.revision is not None:
        body["revision"] = error.revision
    return JSONResponse(
        body,
        status_code=error.status,
        headers={
            "Cache-Control": "no-store, private",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cross-Origin-Resource-Policy": "same-origin",
        },
    )


def create_app(
    settings: Settings | None = None, *, realtime_limits: Limits | None = None
) -> FastAPI:
    settings = settings or Settings.from_env()
    config = settings
    engine = make_engine(config)
    factory = session_factory(engine)
    hub = Hub(factory, realtime_limits or Limits())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await hub.shutdown()
        await engine.dispose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.state.factory = factory
    app.state.settings = config
    app.state.realtime = hub
    import_slots = import_service.ImportSlots()
    http_limits = HTTPRateLimiter()
    app.state.http_limits = http_limits
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list({urlsplit(origin).hostname for origin in config.allowed_origins}),
    )

    app.add_middleware(HandshakeGuard, settings=config)

    @app.middleware("http")
    async def safeguards(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response: Response
        allowed_hosts = {urlsplit(origin).netloc.lower() for origin in config.allowed_origins}
        host_headers = request.headers.getlist("host")
        if len(host_headers) != 1 or host_headers[0].lower() not in allowed_hosts:
            return error_response(APIError("HOST_DENIED", 400))
        if (
            config.environment == "production"
            and request.url.path.startswith("/api/")
            and request.scope["scheme"] != "https"
        ):
            return error_response(APIError("HTTPS_REQUIRED", 403))
        if config.rate_limit_enabled and request.url.path.startswith("/api/"):
            retry = http_limits.check(
                request.client.host if request.client else "unknown",
                request.method,
                request.url.path,
            )
            if retry:
                limited = error_response(APIError("RATE_LIMITED", 429))
                limited.headers["Retry-After"] = str(retry)
                return limited
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if (
                request.headers.get("origin") not in config.allowed_origins
                or request.headers.get("sec-fetch-site") == "cross-site"
            ):
                response = error_response(APIError("ORIGIN_DENIED", 403))
            elif request.method == "POST" and import_service.IMPORT_PATH.fullmatch(
                request.url.path
            ):
                response = await call_next(request)
            elif (
                request.headers.get("content-type", "").split(";")[0].strip() != "application/json"
            ):
                response = error_response(APIError("JSON_REQUIRED", 415))
            else:
                chunks, size = [], 0
                try:
                    async with asyncio.timeout(JSON_BODY_SECONDS):
                        async for chunk in request.stream():
                            size += len(chunk)
                            if size > MAX_REQUEST_BYTES:
                                break
                            chunks.append(chunk)
                except TimeoutError:
                    return error_response(APIError("REQUEST_TIMEOUT", 408))
                if size > MAX_REQUEST_BYTES:
                    response = error_response(APIError("REQUEST_TOO_LARGE", 413))
                else:
                    request._body = b"".join(chunks)
                    response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        return response

    @app.exception_handler(APIError)
    async def api_error(request: Request, error: APIError) -> JSONResponse:
        return error_response(error)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        # Default FastAPI errors include input values. Never echo a submitted body.
        return error_response(APIError("INVALID_REQUEST", 422))

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, error: SQLAlchemyError) -> JSONResponse:
        return error_response(APIError("DATABASE_UNAVAILABLE", 503))

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception) -> JSONResponse:
        return error_response(APIError("INTERNAL_ERROR", 500))

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        async with factory() as db:
            revision = await db.scalar(text("SELECT version_num FROM alembic_version"))
            if revision != SCHEMA_REVISION:
                raise APIError("SCHEMA_NOT_READY", 503)
        return {"status": "ok"}

    @app.post("/api/v1/sessions")
    async def sessions(request: Request, response: Response) -> dict[str, Any]:
        # Origin+JSON required even for bootstrap. Existing cookie is never replaced by client ID.
        async with factory.begin() as db:
            if config.cookie_name in request.cookies:
                session = await authenticate(db, request, config)
            else:
                session, token = new_session(config)
                db.add(session)
                await db.flush()
                response.set_cookie(
                    config.cookie_name,
                    token,
                    httponly=True,
                    secure=config.cookie_secure,
                    samesite="lax",
                    path="/",
                    max_age=config.session_days * 86400,
                )
            memberships = list(
                await db.scalars(select(Member.room_id).where(Member.session_id == session.id))
            )
            rooms = []
            for room_id in memberships:
                room = await db.get(Room, room_id)
                if room is not None:
                    rooms.append(await service.lobby_view(db, room, session.id))
            return {"csrf_token": csrf_token(session, config), "rooms": rooms}

    @app.post("/api/v1/rooms")
    async def create(request: Request, body: Nickname) -> dict[str, Any]:
        async with factory.begin() as db:
            session = await authenticate(db, request, config, csrf=True)
            return await service.create_room(db, session.id, body.nickname)

    @app.post("/api/v1/rooms/{code}/join")
    async def join(code: str, request: Request, body: Nickname) -> dict[str, Any]:
        async with factory.begin() as db:
            session = await authenticate(db, request, config, csrf=True)
            return await service.join_room(db, code, session.id, body.nickname)

    @app.get("/api/v1/rooms/{code}")
    async def room_info(code: str, request: Request) -> dict[str, Any]:
        async with factory.begin() as db:
            session = await authenticate(db, request, config)
            room = await repo.room_by_code(db, code)
            return await service.lobby_view(db, room, session.id)

    @app.get("/api/v1/cases")
    async def cases(request: Request) -> list[dict[str, Any]]:
        async with factory.begin() as db:
            await authenticate(db, request, config)
            return await catalog.list_cases(db)

    @app.get("/api/v1/rooms/{code}/cases")
    async def room_cases(code: str, request: Request) -> list[dict[str, Any]]:
        async with factory.begin() as db:
            session = await authenticate(db, request, config)
            room = await repo.room_by_code(db, code)
            await repo.membership(db, room, session.id)
            return await catalog.list_cases(db, room.id)

    @app.post("/api/v1/rooms/{code}/cases/import")
    async def import_case(code: str, request: Request) -> dict[str, Any]:
        async with factory.begin() as db:
            await import_service.check_access(db, request, config, code)
        async with import_slots.acquire():
            data = await import_service.read_file(request)
            manifest = await import_service.validate(data)
            return await import_service.persist(factory, request, config, code, data, manifest)

    @app.post("/api/v1/rooms/{code}/cases/select")
    async def select_case_route(code: str, request: Request, body: SelectCase) -> dict[str, Any]:
        async with factory.begin() as db:
            session = await authenticate(db, request, config, csrf=True)
            return await service.select_case(db, code, session.id, body.case_id)

    @app.post("/api/v1/rooms/{code}/start")
    async def start(code: str, request: Request, body: ActionIdentity) -> dict[str, Any]:
        async with factory.begin() as db:
            session = await authenticate(db, request, config, csrf=True)
            return await service.execute(
                db, code, session.id, body, c.StartGame(), "start_game", {}
            )

    @app.post("/api/v1/rooms/{code}/actions")
    async def actions(code: str, request: Request, body: ActionRequest) -> dict[str, Any]:
        async with factory.begin() as db:
            session = await authenticate(db, request, config, csrf=True)
            try:
                command = (
                    board.decode(body.payload)
                    if body.type == "board_mutation"
                    else decode_command(body)
                )
            except (ValueError, ValidationError):
                raise APIError("INVALID_REQUEST", 422) from None
            return await service.execute(
                db, code, session.id, body, command, body.type, body.payload
            )

    @app.get("/api/v1/rooms/{code}/snapshot")
    async def get_snapshot(code: str, request: Request) -> dict[str, Any]:
        async with factory.begin() as db:
            session = await authenticate(db, request, config)
            return await service.snapshot(db, code, session.id)

    @app.get("/api/v1/rooms/{code}/result")
    async def get_result(code: str, request: Request) -> dict[str, Any]:
        async with factory.begin() as db:
            session = await authenticate(db, request, config)
            return await service.snapshot(db, code, session.id, result_only=True)

    @app.get("/api/v1/rooms/{code}/assets/{asset_id}")
    async def asset(code: str, asset_id: str, request: Request) -> Response:
        async with factory.begin() as db:
            session = await authenticate(db, request, config)
            content, mime = await catalog.asset_bytes(db, code, session.id, asset_id, config)
            return Response(content, media_type=mime)

    @app.websocket("/ws/v1/rooms/{code}")
    async def websocket_room(socket: WebSocket, code: str) -> None:
        await serve(socket, code, config, hub)

    return app
