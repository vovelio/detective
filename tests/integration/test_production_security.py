"""Real PostgreSQL sessions, production HTTP budgets and native Uvicorn proxy trust."""

import asyncio
import socket
from dataclasses import replace

import httpx
import pytest
from detective.api.app import create_app

from .conftest import Live
from .test_api import client

pytestmark = pytest.mark.asyncio


async def test_production_cookie_csrf_https_and_exact_host(pg):
    old = pg.app
    pg.app = create_app(
        replace(pg.settings, environment="production", forwarded_allow_ips=("172.30.0.2",))
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=pg.app),
            base_url="https://test.local",
            headers={"Origin": "https://test.local"},
        ) as actor:
            response = await actor.post("/api/v1/sessions", json={})
            actor.headers["X-CSRF-Token"] = response.json()["csrf_token"]
            cookie = response.headers.get("set-cookie", "")
            metadata = {
                flag: flag in cookie
                for flag in (
                    "__Host-detective_session=",
                    "Secure",
                    "HttpOnly",
                    "SameSite=lax",
                    "Path=/",
                )
            }
            metadata["no_domain"] = "Domain=" not in cookie
            assert all(metadata.values())
            assert (
                await actor.post("/api/v1/rooms", json={"nickname": "Игрок"})
            ).status_code == 200
            denied = await actor.post(
                "/api/v1/rooms", json={"nickname": "Игрок"}, headers={"X-CSRF-Token": "wrong"}
            )
            assert denied.status_code == 403
            denied = await actor.get("/api/v1/cases", headers={"Host": "test.local:8443"})
            assert denied.status_code == 400
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=pg.app), base_url="http://test.local"
        ) as insecure:
            response = await insecure.post(
                "/api/v1/sessions", json={}, headers={"Origin": "https://test.local"}
            )
            assert (
                response.status_code == 403 and response.json()["error"]["code"] == "HTTPS_REQUIRED"
            )
            assert (await insecure.get("/health/ready")).status_code == 200
    finally:
        await pg.app.state.engine.dispose()
        pg.app = old


async def test_production_rate_limit_ignores_untrusted_forwarded_headers(pg):
    old = pg.app
    pg.app = create_app(
        replace(pg.settings, environment="production", forwarded_allow_ips=("172.30.0.2",))
    )
    try:
        async with client(pg) as actor:
            for index in range(5):
                response = await actor.post(
                    "/api/v1/rooms",
                    json={"nickname": "Игрок"},
                    headers={"X-Forwarded-For": f"198.51.100.{index + 1}"},
                )
                assert response.status_code == 200
            response = await actor.post(
                "/api/v1/rooms",
                json={"nickname": "Игрок"},
                headers={"X-Forwarded-For": "203.0.113.99"},
            )
            assert response.status_code == 429
            assert response.json()["error"]["code"] == "RATE_LIMITED"
            assert 1 <= int(response.headers["retry-after"]) <= 6
            assert response.headers["cache-control"] == "no-store, private"
            assert (await actor.get("/api/v1/cases")).status_code == 200
    finally:
        await pg.app.state.engine.dispose()
        pg.app = old


async def test_json_slow_body_is_bounded_without_weakening_import_limits(pg, monkeypatch):
    import detective.api.app as module

    monkeypatch.setattr(module, "JSON_BODY_SECONDS", 0.01)
    async with client(pg) as actor:

        async def slow():
            yield b"{"
            await asyncio.sleep(0.05)
            yield b"}"

        response = await actor.post(
            "/api/v1/rooms", content=slow(), headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 408 and response.json()["error"]["code"] == "REQUEST_TIMEOUT"
        response = await actor.post("/api/v1/rooms", json={"nickname": "x" * 33000})
        assert response.status_code == 413


async def test_native_uvicorn_trusts_only_configured_proxy_peer(pg):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    public = origin.replace("http:", "https:")
    server = Live(
        origin,
        {
            **pg.env,
            "APP_ENV": "production",
            "ALLOWED_ORIGINS": public,
            "SESSION_COOKIE_SECURE": "true",
            "FORWARDED_ALLOW_IPS": "127.0.0.1",
        },
        port,
    )
    try:
        await server.launch()
        async with httpx.AsyncClient(base_url=origin) as probe:
            headers = {"Origin": public, "X-Forwarded-Proto": "https"}
            assert (
                await probe.post("/api/v1/sessions", json={}, headers=headers)
            ).status_code == 200
            assert (
                await probe.post("/api/v1/sessions", json={}, headers={"Origin": public})
            ).status_code == 403
        await server.stop()
        server.env["FORWARDED_ALLOW_IPS"] = "172.30.0.2"
        await server.launch()
        async with httpx.AsyncClient(base_url=origin) as probe:
            response = await probe.post(
                "/api/v1/sessions",
                json={},
                headers={
                    "Origin": public,
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-For": "1.2.3.4",
                },
            )
            assert (
                response.status_code == 403 and response.json()["error"]["code"] == "HTTPS_REQUIRED"
            )
    finally:
        await server.stop()


async def test_alembic_uses_mounted_database_secret_without_application_env(pg, tmp_path):
    from .conftest import migrate

    secret = tmp_path / "database_url"
    secret.write_text(pg.settings.database_url + "\n")
    secret.chmod(0o444)
    env = {**pg.env, "DATABASE_URL_FILE": str(secret)}
    for name in ("DATABASE_URL", "ALLOWED_ORIGINS", "TOKEN_HASH_PEPPER", "TOKEN_HASH_PEPPER_FILE"):
        env.pop(name, None)
    await asyncio.to_thread(migrate, env, "check")
