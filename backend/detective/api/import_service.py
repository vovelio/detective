"""Host-only room-scoped import, bounded HTTP/validation and immutable package writes."""

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from python_multipart import MultipartParser
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import parse_options_header
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from detective.scenarios.validation import MAX_ZIP_BYTES

from . import package_storage, repository, service
from .config import Settings
from .db import ScenarioRow
from .import_worker import MAX_OUTPUT_BYTES
from .security import APIError, authenticate

UPLOAD_SECONDS = 30
VALIDATION_SECONDS = 10
MAX_BODY_BYTES = MAX_ZIP_BYTES + 64 * 1024
MAX_ROOM_CASES = 16
IMPORT_PATH = re.compile(r"^/api/v1/rooms/[A-Za-z0-9_-]{8,24}/cases/import$")


class ImportErrorResponse(APIError):
    def __init__(self, issues: list[dict[str, str]]) -> None:
        super().__init__("CASE_VALIDATION_FAILED", 422)
        self.issues = issues


class ImportSlots:
    def __init__(self) -> None:
        self.active = 0

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        if self.active >= 2:
            raise APIError("IMPORT_BUSY", 503)
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1


async def check_access(db: AsyncSession, request: Request, config: Settings, code: str) -> UUID:
    session = await authenticate(db, request, config, csrf=True)
    room = await repository.room_by_code(db, code)
    service.host_only(room, await repository.membership(db, room, session.id))
    game = await repository.game_for(db, room)
    if game.state is not None or game.phase != "LOBBY":
        raise APIError("IMPORT_LOBBY_ONLY", 409)
    return session.id


async def read_file(request: Request) -> bytes:
    """Streaming parser: one named file, no fields, bounded headers/body/file/time."""
    content_type, options = parse_options_header(request.headers.get("content-type", ""))
    boundary = options.get(b"boundary", b"")
    if content_type != b"multipart/form-data" or not 1 <= len(boundary) <= 200:
        raise APIError("INVALID_MULTIPART", 422)
    if any(c < 32 or c > 126 for c in boundary):
        raise APIError("INVALID_MULTIPART", 422)
    try:
        length = int(request.headers.get("content-length", "0"))
    except ValueError:
        raise APIError("INVALID_MULTIPART", 422) from None
    if length > MAX_BODY_BYTES:
        raise APIError("IMPORT_TOO_LARGE", 413)
    data = bytearray()
    headers: dict[bytes, bytes] = {}
    header_name, header_value = bytearray(), bytearray()
    parts, completed, size, header_bytes = 0, False, 0, 0

    def begin() -> None:
        nonlocal parts
        parts += 1
        if parts != 1:
            raise APIError("INVALID_MULTIPART", 422)

    def header_field(value: bytes, start: int, end: int) -> None:
        nonlocal header_bytes
        header_bytes += end - start
        if header_bytes > 65536:
            raise APIError("INVALID_MULTIPART", 422)
        header_name.extend(value[start:end])

    def header_data(value: bytes, start: int, end: int) -> None:
        nonlocal header_bytes
        header_bytes += end - start
        if header_bytes > 65536:
            raise APIError("INVALID_MULTIPART", 422)
        header_value.extend(value[start:end])

    def header_end() -> None:
        name = bytes(header_name).lower()
        if name in headers:
            raise APIError("INVALID_MULTIPART", 422)
        headers[name] = bytes(header_value)
        header_name.clear()
        header_value.clear()

    def headers_done() -> None:
        kind, attrs = parse_options_header(headers.get(b"content-disposition", b""))
        if kind != b"form-data" or attrs.get(b"name") != b"file" or b"filename" not in attrs:
            raise APIError("INVALID_MULTIPART", 422)

    def part_data(value: bytes, start: int, end: int) -> None:
        if len(data) + end - start > MAX_ZIP_BYTES:
            raise APIError("IMPORT_TOO_LARGE", 413)
        data.extend(value[start:end])

    def done() -> None:
        nonlocal completed
        completed = True

    try:
        parser = MultipartParser(
            boundary,
            {
                "on_part_begin": begin,
                "on_header_field": header_field,
                "on_header_value": header_data,
                "on_header_end": header_end,
                "on_headers_finished": headers_done,
                "on_part_data": part_data,
                "on_end": done,
            },
            max_size=MAX_BODY_BYTES,
            max_header_count=8,
            max_header_size=8192,
        )
        # The parser's diagnostics can contain input bytes; never send them to logs.
        parser.logger = logging.Logger("detective.multipart.private", level=logging.CRITICAL + 1)
        async with asyncio.timeout(UPLOAD_SECONDS):
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    raise APIError("IMPORT_TOO_LARGE", 413)
                for offset in range(0, len(chunk), 65536):
                    parser.write(chunk[offset : offset + 65536])
            parser.finalize()
    except TimeoutError:
        raise APIError("IMPORT_TIMEOUT", 504) from None
    except (FormParserError, ValueError):
        raise APIError("INVALID_MULTIPART", 422) from None
    if not completed or parts != 1:
        raise APIError("INVALID_MULTIPART", 422)
    return bytes(data)


async def validate(data: bytes) -> dict[str, Any]:
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "detective.api.import_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )
    try:
        async with asyncio.timeout(VALIDATION_SECONDS):
            output, _ = await process.communicate(data)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise
        raise APIError("IMPORT_TIMEOUT", 504) from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode != 0 or len(output) > MAX_OUTPUT_BYTES:
        raise APIError("IMPORT_PROCESSING_FAILED", 422)
    result = json.loads(output)
    if "issues" in result:
        raise ImportErrorResponse(result["issues"])
    if "manifest" not in result:
        raise APIError("IMPORT_PROCESSING_LIMIT", 422)
    manifest: dict[str, Any] = result["manifest"]
    return manifest


async def digest_lock(db: AsyncSession, checksum: str) -> None:
    key = int.from_bytes(bytes.fromhex(checksum)[:8], "big", signed=True)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def cleanup_unreferenced(
    factory: async_sessionmaker[AsyncSession], config: Settings, checksum: str
) -> None:
    try:
        async with factory() as db, db.begin():
            await digest_lock(db, checksum)
            referenced = await db.scalar(
                select(ScenarioRow.id).where(ScenarioRow.checksum == checksum)
            )
            if referenced is None:
                target = config.case_storage_path / f"{checksum}.detective"
                target.unlink(missing_ok=True)
                package_storage.sync_directory(config.case_storage_path)
    except Exception:
        # A failed/ambiguous COMMIT must never cause deletion of a referenced package.
        # Private orphan bytes are safe to reuse on retry. No manifest/error is logged.
        return


async def persist(
    factory: async_sessionmaker[AsyncSession],
    request: Request,
    config: Settings,
    code: str,
    data: bytes,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    checksum = hashlib.sha256(data).hexdigest()
    touched = False
    try:
        async with factory() as db, db.begin():
            session = await authenticate(db, request, config, csrf=True)
            room = await repository.room_by_code(db, code, lock=True)
            service.host_only(room, await repository.membership(db, room, session.id))
            game = await repository.game_for(db, room, lock=True)
            if game.state is not None or game.phase != "LOBBY":
                raise APIError("IMPORT_LOBBY_ONLY", 409)
            id_ = (
                "upload-"
                + hashlib.sha256((str(room.id) + ":" + manifest["id"]).encode()).hexdigest()
            )
            existing = await db.get(ScenarioRow, id_)
            if existing is not None and existing.checksum != checksum:
                raise APIError("CASE_ID_CONFLICT", 409)
            if existing is None:
                count = await db.scalar(
                    select(func.count())
                    .select_from(ScenarioRow)
                    .where(ScenarioRow.scope_room_id == room.id)
                )
                if count is not None and count >= MAX_ROOM_CASES:
                    raise APIError("CASE_LIMIT", 409)
            await digest_lock(db, checksum)
            touched = True
            try:
                package_storage.publish(config.case_storage_path, data, checksum)
            except OSError:
                raise APIError("CASE_STORAGE_UNAVAILABLE", 503) from None
            if existing is None:
                row = ScenarioRow(
                    id=id_,
                    scope_room_id=room.id,
                    version=manifest["format_version"],
                    manifest=manifest,
                    checksum=checksum,
                    package_name=f"{checksum}.detective",
                )
                db.add(row)
                room.catalog_revision += 1
                await db.flush()
            return {
                "case": {"id": id_, "meta": manifest["meta"]},
                "already_imported": existing is not None,
            }
    except BaseException:
        if touched:
            await asyncio.shield(cleanup_unreferenced(factory, config, checksum))
        raise
