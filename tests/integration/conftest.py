"""Every test gets an isolated migrated schema in an explicitly named test database."""

import asyncio
import io
import json
import os
import socket
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from detective.api.app import create_app
from detective.api.catalog import install_case
from detective.api.config import Settings
from detective.api.db import make_engine, session_factory
from PIL import Image
from sqlalchemy import text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Harness:
    settings: Settings
    app: object
    env: dict[str, str]
    schema: str


def migrate(env, *args):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True
    )
    if result.returncode:
        raise RuntimeError("Migration failed: " + result.stderr)
    return result.stdout


@pytest_asyncio.fixture
async def pg(tmp_path):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to an isolated PostgreSQL detective_test database")
    if not (make_url(url).database or "").startswith("detective_test"):
        pytest.fail("Refusing integration tests outside a detective_test* database")
    schema = "test_" + uuid4().hex
    # Disposable test key, not a real credential. Never printed or saved in the repository.
    import secrets

    settings = Settings(
        url,
        secrets.token_hex(32),
        ("https://test.local",),
        tmp_path / "cases",
        database_schema=schema,
    )
    env = {
        **os.environ,
        "DATABASE_URL": url,
        "DATABASE_SCHEMA": schema,
        "TOKEN_HASH_PEPPER": settings.token_hash_pepper,
        "ALLOWED_ORIGINS": "https://test.local",
        "CASE_STORAGE_PATH": str(settings.case_storage_path),
        "PYTHONPATH": str(ROOT / "backend"),
    }
    admin = make_engine(settings)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    app = None
    try:
        await asyncio.to_thread(migrate, env, "upgrade", "head")
        app = create_app(settings)
        async with session_factory(admin).begin() as db:
            await install_case(db, ROOT / "docs/examples/demo-case.detective", settings)
            media = json.loads((ROOT / "docs/examples/demo-case.json").read_text())
            media["id"] = "media-demo"
            media["assets"] = []
            package = tmp_path / "media.detective"
            with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
                for index, card in enumerate([media["start_card"], *media["play_cards"]]):
                    asset_id, path = f"photo-{index:02}", f"assets/photo-{index:02}.png"
                    card["blocks"].append(
                        {"type": "image", "asset_id": asset_id, "caption": "Фото"}
                    )
                    media["assets"].append(
                        {
                            "id": asset_id,
                            "path": path,
                            "mime": "image/png",
                            "alt": "PRIVATE RESOURCE",
                        }
                    )
                    image = io.BytesIO()
                    Image.new("RGB", (2, 2), (index, 0, 0)).save(image, format="PNG")
                    archive.writestr(path, image.getvalue())
                archive.writestr("case.json", json.dumps(media))
            await install_case(db, package, settings)
        yield Harness(settings, app, env, schema)
    finally:
        if app is not None:
            await app.state.engine.dispose()
        # Only our random schema in the explicitly opted-in test database is removed.
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@dataclass
class Live:
    origin: str
    env: dict
    port: int
    process: object = None

    async def launch(self):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "detective.api", "--port", str(self.port)],
            cwd=ROOT,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        async with httpx.AsyncClient(base_url=self.origin) as probe:
            for _ in range(100):
                if self.process.poll() is not None:
                    raise RuntimeError("Local API exited")
                try:
                    if (await probe.get("/health/ready")).status_code == 200:
                        return
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
        raise RuntimeError("Local API did not become ready")

    async def stop(self, *, kill=False):
        if self.process is not None and self.process.poll() is None:
            self.process.kill() if kill else self.process.terminate()
            await asyncio.to_thread(self.process.wait, timeout=5)


@pytest_asyncio.fixture
async def live(pg):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    server = Live(
        origin, {**pg.env, "ALLOWED_ORIGINS": origin, "SESSION_COOKIE_SECURE": "false"}, port
    )
    try:
        await server.launch()
        yield server
    finally:
        await server.stop()
