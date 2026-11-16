"""Opaque cookie authentication and session-bound CSRF proof."""

import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection

from .config import Settings
from .db import AnonymousSession


class APIError(Exception):
    def __init__(self, code: str, status: int = 400, revision: int | None = None):
        self.code, self.status, self.revision = code, status, revision


def token_hash(token: str, settings: Settings) -> str:
    return hmac.new(
        settings.token_hash_pepper.encode(), b"session:" + token.encode(), hashlib.sha256
    ).hexdigest()


def csrf_token(session: AnonymousSession, settings: Settings) -> str:
    return hmac.new(
        settings.token_hash_pepper.encode(), b"csrf:" + str(session.id).encode(), hashlib.sha256
    ).hexdigest()


async def authenticate(
    db: AsyncSession, request: HTTPConnection, settings: Settings, *, csrf: bool = False
) -> AnonymousSession:
    token = request.cookies.get(settings.cookie_name, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise APIError("SESSION_REQUIRED", 401)
    session = await db.scalar(
        select(AnonymousSession).where(
            AnonymousSession.token_hash == token_hash(token, settings),
            AnonymousSession.expires_at > datetime.now(UTC),
        )
    )
    if session is None:
        raise APIError("SESSION_EXPIRED_OR_INVALID", 401)
    if csrf and not secrets.compare_digest(
        request.headers.get("x-csrf-token", "").encode(), csrf_token(session, settings).encode()
    ):
        raise APIError("CSRF_FAILED", 403)
    return session


def new_session(settings: Settings) -> tuple[AnonymousSession, str]:
    token = secrets.token_urlsafe(32)
    session = AnonymousSession(
        token_hash=token_hash(token, settings),
        expires_at=datetime.now(UTC) + timedelta(days=settings.session_days),
    )
    return session, token
