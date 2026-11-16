"""Built-in catalogue, room-scoped imports and authorized package resources."""

import asyncio
import hashlib
import io
import re
import sys
import zipfile
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from detective.scenarios.validation import MAX_ZIP_BYTES, validate_package

from . import package_storage
from . import repository as repo
from .config import Settings
from .db import ScenarioRow, make_engine, session_factory
from .security import APIError
from .service import snapshot


async def install_case(db: AsyncSession, source: Path, settings: Settings) -> str:
    with source.open("rb") as stream:
        data = stream.read(MAX_ZIP_BYTES + 1)
    manifest = validate_package(data)
    checksum = hashlib.sha256(data).hexdigest()
    name = f"{checksum}.detective"
    key = int.from_bytes(bytes.fromhex(checksum)[:8], "big", signed=True)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    package_storage.publish(settings.case_storage_path, data, checksum)
    await db.execute(
        insert(ScenarioRow)
        .values(
            id=manifest["id"],
            version=manifest["format_version"],
            manifest=manifest,
            checksum=checksum,
            package_name=name,
        )
        .on_conflict_do_nothing(index_elements=[ScenarioRow.id])
    )
    row = await db.get(ScenarioRow, manifest["id"])
    if row is None or row.checksum != checksum or row.scope_room_id is not None:
        raise ValueError("CASE_ID_ALREADY_INSTALLED_WITH_DIFFERENT_CONTENT")
    return str(manifest["id"])


async def list_cases(db: AsyncSession, room_id: UUID | None = None) -> list[dict[str, Any]]:
    return [
        {"id": row.id, "meta": row.manifest["meta"]}
        for row in await db.scalars(
            select(ScenarioRow)
            .where(
                or_(ScenarioRow.scope_room_id.is_(None), ScenarioRow.scope_room_id == room_id)
                if room_id is not None
                else ScenarioRow.scope_room_id.is_(None)
            )
            .order_by(ScenarioRow.id)
        )
    ]


async def asset_bytes(
    db: AsyncSession, code: str, session_id: UUID, asset_id: str, settings: Settings
) -> tuple[bytes, str]:
    view = await snapshot(db, code, session_id)
    visible = [*view.get("assets", []), *view.get("private", {}).get("assets", [])]
    asset = next((a for a in visible if a["id"] == asset_id), None)
    if asset is None:
        raise APIError("ASSET_NOT_FOUND", 404)
    room = await repo.room_by_code(db, code)
    game = await repo.game_for(db, room)
    row = await db.get(ScenarioRow, game.scenario_id) if game.scenario_id else None
    if row is None or not re.fullmatch(r"[0-9a-f]{64}\.detective", row.package_name):
        raise APIError("ASSET_UNAVAILABLE", 503)
    try:
        path = settings.case_storage_path / row.package_name
        if path.is_symlink():
            raise ValueError("invalid storage")
        with path.open("rb") as stream:
            data = stream.read(MAX_ZIP_BYTES + 1)
        if len(data) > MAX_ZIP_BYTES or hashlib.sha256(data).hexdigest() != row.checksum:
            raise ValueError("invalid checksum")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            content = archive.read(asset["path"])
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        raise APIError("ASSET_UNAVAILABLE", 503) from None
    return content, asset["mime"]


async def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m detective.api.catalog path/to/case.detective")
    settings = Settings.from_env()
    engine = make_engine(settings)
    try:
        async with session_factory(engine).begin() as db:
            await install_case(db, Path(sys.argv[1]), settings)
        print("Case installed")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
