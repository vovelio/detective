"""Authenticated read-only sockets. Mutable game commands stay behind REST+CSRF."""

import asyncio
from time import monotonic
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import WebSocket
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from detective.api import repository as repo
from detective.api.config import Settings
from detective.api.db import AnonymousSession
from detective.api.security import APIError, authenticate

from .connection import Connection
from .hub import Hub, RoomStatus, read_status
from .protocol import SUBPROTOCOL, CardPreview, Cursor, Ping, Resume, decode, message


class HandshakeGuard:
    """Runs before TrustedHostMiddleware; reject WS using WS ASGI close, not HTTP sends."""

    def __init__(self, app: ASGIApp, settings: Settings):
        self.app, self.settings = app, settings
        self.hosts = {urlsplit(origin).netloc.lower() for origin in settings.allowed_origins}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            headers = Headers(scope=scope)
            protocols = scope.get("subprotocols", [])
            if (
                len(headers.getlist("host")) != 1
                or headers.get("host", "").lower() not in self.hosts
                or len(headers.getlist("origin")) != 1
                or headers.get("origin") not in self.settings.allowed_origins
                or headers.get("sec-fetch-site") == "cross-site"
                or scope.get("query_string")
                or SUBPROTOCOL not in protocols
                or (self.settings.cookie_secure and scope.get("scheme") != "wss")
            ):
                await send({"type": "websocket.close", "code": 1008})
                return
        await self.app(scope, receive, send)


async def _authenticate(
    socket: WebSocket, code: str, settings: Settings, hub: Hub
) -> tuple[RoomStatus, AnonymousSession, UUID]:
    async with hub.factory.begin() as db:
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        session = await authenticate(db, socket, settings)
        room = await repo.room_by_code(db, code)
        member = await repo.membership(db, room, session.id)
        status = await read_status(db, room)
        return status, session, member.id


async def serve(socket: WebSocket, code: str, settings: Settings, hub: Hub) -> None:
    connection: Connection | None = None
    status: RoomStatus | None = None
    tasks: list[asyncio.Task[None]] = []
    try:
        initial, session, member_id = await asyncio.wait_for(
            _authenticate(socket, code, settings, hub), hub.limits.database_timeout
        )
        status = initial
        await asyncio.wait_for(socket.accept(subprotocol=SUBPROTOCOL), hub.limits.send_timeout)
        connection = Connection(
            socket, member_id, session.id, session.expires_at, status.revision, hub.limits
        )
        if not hub.register(connection, status):
            await asyncio.wait_for(socket.close(1013), 1)
            return
        # A shared poller may already have passed a new socket's handshake revision.
        stream = hub.rooms[status.room_id]
        if stream.last_revision > connection.notified_revision:
            connection.notified_revision = stream.last_revision
            connection.offer(
                message("sync_required", revision=stream.last_revision, reason="subscription_race")
            )
        tasks = [
            asyncio.create_task(connection.writer()),
            asyncio.create_task(_receiver(connection, status, hub)),
            asyncio.create_task(_heartbeat(connection)),
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (APIError, SQLAlchemyError, TimeoutError):
        if connection is None:
            try:
                await asyncio.wait_for(socket.close(1008), 1)
            except (WebSocketDisconnect, RuntimeError, OSError, TimeoutError):
                pass
        else:
            connection.stop(1013)
    except (WebSocketDisconnect, RuntimeError, OSError):
        if connection is not None:
            connection.stop(1001)
    finally:
        if connection is not None:
            connection.stop(connection.close_code)
        # Let the writer send the close before cancellation; all child tasks are awaited.
        if tasks:
            try:
                await asyncio.wait_for(asyncio.shield(tasks[0]), 1.5)
            except (TimeoutError, WebSocketDisconnect, RuntimeError, OSError):
                pass
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if connection is not None and status is not None:
            hub.remove(connection, status.room_id)


async def _receiver(connection: Connection, status: RoomStatus, hub: Hub) -> None:
    while not connection.stopped.is_set():
        value = await connection.socket.receive()
        if value["type"] == "websocket.disconnect":
            connection.stop(1000)
            return
        raw = value.get("text")
        if not isinstance(raw, str):
            connection.stop(1003)
            return
        if len(raw.encode()) > connection.limits.input_bytes:
            connection.stop(1009)
            return
        if connection.expired():
            connection.stop(4401)
            return
        if not connection.budget.allow():
            connection.stop(1008)
            return
        try:
            incoming = decode(raw)
        except (ValueError, RecursionError):
            connection.offer(message("error", code="INVALID_MESSAGE"))
            connection.stop(1008)
            return
        connection.last_seen = monotonic()
        match incoming:
            case Resume():
                await asyncio.wait_for(
                    hub.resume(connection, status, incoming.revision),
                    connection.limits.database_timeout,
                )
            case Ping():
                connection.offer(message("pong"))
            case CardPreview():
                if connection.preview_budget.allow():
                    await asyncio.wait_for(
                        hub.card_preview(connection, status, incoming),
                        connection.limits.database_timeout,
                    )
            case Cursor():
                if connection.cursor_budget.allow():
                    hub.cursor(connection, status.room_id, incoming.x, incoming.y)
            case _:
                pass  # pong renews liveness; it carries no client identity


async def _heartbeat(connection: Connection) -> None:
    while not connection.stopped.is_set():
        await asyncio.sleep(connection.limits.heartbeat_interval)
        if monotonic() - connection.last_seen >= connection.limits.heartbeat_timeout:
            connection.stop(4408)
            return
        connection.offer(message("ping"))
