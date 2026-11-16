"""Real PostgreSQL import transactions, room isolation and immutable package safety."""

import asyncio
import hashlib
import io
import json
import stat
import struct
import warnings
import zipfile

import pytest
from detective.api import catalog, import_service, package_storage
from detective.api.app import create_app
from detective.api.db import ScenarioRow
from detective.scenarios.validation import MAX_UNPACKED_BYTES
from PIL import Image
from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError

from .conftest import ROOT, migrate
from .test_api import all_keys, client, identity, room, snapshot, start

pytestmark = pytest.mark.asyncio


def package(*, id_="user-case", edit=None, images=False, extra=None):
    case = json.loads((ROOT / "docs/examples/demo-case.json").read_text())
    case["id"] = id_
    case["meta"]["title"] = "Пользовательское дело"
    assets = {}
    if images:
        data = io.BytesIO()
        Image.new("RGB", (2, 2), "blue").save(data, format="PNG")
        case["assets"] = []
        for i, card in enumerate(case["play_cards"]):
            id_ = f"private-img-{i}"
            path = f"assets/{i}.png"
            assets[path] = data.getvalue()
            case["assets"].append({"id": id_, "path": path, "mime": "image/png", "alt": "Test"})
            card["blocks"].append({"type": "image", "asset_id": id_, "caption": "Test"})
    if edit:
        edit(case)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("case.json", json.dumps(case))
        for path, content in assets.items():
            archive.writestr(path, content)
        if extra:
            for path, content in extra.items():
                archive.writestr(path, content)
    return output.getvalue()


def unsafe_archive(kind):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("case.json", "not json" if kind == "json" else "{}")
        if kind == "duplicate":
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr("case.json", "{}")
        if kind == "symlink":
            entry = zipfile.ZipInfo("assets/link.png")
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, "../../PRIVATE_TARGET")
    return output.getvalue()


async def upload(session, code, data, **kwargs):
    return await session.post(
        f"/api/v1/rooms/{code}/cases/import",
        files={"file": ("../../untrusted.name", data, "text/html")},
        **kwargs,
    )


async def cases(session, code):
    response = await session.get(f"/api/v1/rooms/{code}/cases")
    assert response.status_code == 200
    return response.json()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


async def test_import_scoped_catalog_and_selection_without_secrets(pg):
    async with client(pg) as host, client(pg) as guest, client(pg) as other:
        code = await room(host, guest)
        other_code = await room(other)
        response = await upload(host, code, package())
        assert response.status_code == 200
        info = response.json()
        assert set(info) == {"case", "already_imported"} and not info["already_imported"]
        case_id = info["case"]["id"]
        assert case_id.startswith("upload-")
        assert not ({"solution", "role", "correct_option_id", "logic_notes"} & all_keys(info))
        assert case_id in {c["id"] for c in await cases(guest, code)}
        assert case_id not in {c["id"] for c in await cases(other, other_code)}
        builtins = await host.get("/api/v1/cases")
        assert case_id not in {c["id"] for c in builtins.json()}
        assert (await other.get(f"/api/v1/rooms/{code}/cases")).status_code == 403
        select = await other.post(
            f"/api/v1/rooms/{other_code}/cases/select", json={"case_id": case_id}
        )
        assert select.status_code == 404
        selected = await host.post(f"/api/v1/rooms/{code}/cases/select", json={"case_id": case_id})
        assert selected.status_code == 200
        await start(host, code)
        view = await snapshot(host, code)
        assert view["case"]["id"] == "user-case" and len(view["private"]["hand"]) == 3
        assert not ({"solution", "correct_option_id", "role"} & all_keys(view))


async def test_host_lobby_csrf_origin_and_json_limits_stay_enforced(pg):
    async with client(pg) as host, client(pg) as guest, client(pg) as outsider:
        code = await room(host, guest)
        for session, status in ((guest, 403), (outsider, 403)):
            response = await upload(session, code, package())
            assert response.status_code == status
        response = await upload(host, code, package(), headers={"X-CSRF-Token": "wrong"})
        assert response.status_code == 403
        response = await upload(host, code, package(), headers={"Origin": "https://evil.invalid"})
        assert response.status_code == 403
        response = await host.post(f"/api/v1/rooms/{code}/cases/import", json={})
        assert response.status_code == 422
        response = await host.post(f"/api/v1/rooms/{code}/actions", files={"file": ("x", b"x")})
        assert response.status_code == 415
        response = await host.post(f"/api/v1/rooms/{code}/actions", json={"x": "x" * 33000})
        assert response.status_code == 413
        await start(host, code)
        assert (await upload(host, code, package())).status_code == 409


async def test_repeat_same_id_conflict_and_parallel_imports(pg):
    data = package()
    async with client(pg) as host, client(pg) as other:
        code = await room(host)
        other_code = await room(other)
        results = await asyncio.gather(upload(host, code, data), upload(host, code, data))
        assert all(r.status_code == 200 for r in results)
        assert {r.json()["already_imported"] for r in results} == {False, True}
        id_ = results[0].json()["case"]["id"]
        response = await upload(
            host, code, package(edit=lambda c: c["meta"].update(title="Changed"))
        )
        assert (
            response.status_code == 409 and response.json()["error"]["code"] == "CASE_ID_CONFLICT"
        )
        # Identical bytes may be referenced privately from two rooms, with distinct opaque IDs.
        response = await upload(other, other_code, data)
        assert response.status_code == 200 and response.json()["case"]["id"] != id_
        assert (
            len(list(pg.settings.case_storage_path.glob(hashlib.sha256(data).hexdigest() + "*")))
            == 1
        )
        async with pg.app.state.factory() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(ScenarioRow)
                    .where(ScenarioRow.scope_room_id.is_not(None))
                )
                == 2
            )


@pytest.mark.parametrize(
    "data,code",
    [
        (b"not a ZIP", "INVALID_ZIP"),
        (unsafe_archive("json"), "INVALID_JSON"),
        (unsafe_archive("duplicate"), "UNSAFE_OR_DUPLICATE_PATH"),
        (unsafe_archive("symlink"), "NON_REGULAR_FILE"),
        (package(edit=lambda c: c.update(format_version="2.0.0")), "SCHEMA_INVALID"),
        (
            package(edit=lambda c: c["play_cards"][1].update(id=c["play_cards"][0]["id"])),
            "DUPLICATE",
        ),
        (package(extra={"../SECRET_VALUE": b"bad"}), "UNSAFE_OR_DUPLICATE_PATH"),
        (package(extra={"assets/undeclared.js": b"code"}), "MISSING_OR_UNDECLARED_FILE"),
    ],
)
async def test_invalid_packages_return_safe_diagnostics_and_no_partial_case(pg, data, code):
    async with client(pg) as host:
        room_code = await room(host)
        response = await upload(host, room_code, data)
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "CASE_VALIDATION_FAILED"
        assert code in {i["code"] for i in error["issues"]}
        assert "SECRET_VALUE" not in response.text
        assert not list(pg.settings.case_storage_path.glob(hashlib.sha256(data).hexdigest() + "*"))
        assert not any(c["id"].startswith("upload-") for c in await cases(host, room_code))


async def test_unpacked_bomb_bad_image_and_duplicate_resource_rejected(pg):
    data = bytearray(package())
    # Central-directory declared uncompressed size is rejected before extraction/read.
    central = data.index(b"PK\x01\x02")
    struct.pack_into("<I", data, central + 24, MAX_UNPACKED_BYTES + 1)
    broken_image = package(images=True)
    stream = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(broken_image)) as original,
        zipfile.ZipFile(stream, "w") as archive,
    ):
        for item in original.infolist():
            archive.writestr(
                item.filename,
                b"not image" if item.filename.endswith(".png") else original.read(item),
            )
    duplicate = package(images=True, edit=lambda c: c["assets"][1].update(id=c["assets"][0]["id"]))
    async with client(pg) as host:
        code = await room(host)
        for candidate, expected in (
            (bytes(data), "UNPACKED_TOO_LARGE"),
            (stream.getvalue(), "INVALID_IMAGE"),
            (duplicate, "DUPLICATE"),
        ):
            response = await upload(host, code, candidate)
            assert response.status_code == 422
            assert expected in {i["code"] for i in response.json()["error"]["issues"]}


async def test_imported_images_authorized_and_restored_in_new_app(pg):
    async with client(pg) as host, client(pg) as guest:
        code = await room(host, guest)
        imported = (await upload(host, code, package(images=True))).json()["case"]["id"]
        assert (
            await host.post(f"/api/v1/rooms/{code}/cases/select", json={"case_id": imported})
        ).status_code == 200
        await start(host, code)
        before = await snapshot(host, code)
        asset = before["private"]["assets"][0]["id"]
        assert (await host.get(f"/api/v1/rooms/{code}/assets/{asset}")).status_code == 200
        assert (await guest.get(f"/api/v1/rooms/{code}/assets/{asset}")).status_code == 404
        old = pg.app
        pg.app = create_app(pg.settings)
        try:
            # Existing cookie/session and package reference survive a new application.
            import httpx

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=pg.app),
                base_url="https://test.local",
                cookies=host.cookies,
            ) as restored:
                assert digest(await snapshot(restored, code)) == digest(before)
                assert imported in {c["id"] for c in await cases(restored, code)}
                assert (
                    await restored.get(f"/api/v1/rooms/{code}/assets/{asset}")
                ).status_code == 200
        finally:
            await pg.app.state.engine.dispose()
            pg.app = old


async def test_rollback_removes_only_unreferenced_digest_and_retry_recovers(pg):
    data = package()
    checksum = hashlib.sha256(data).hexdigest()
    async with client(pg) as host:
        code = await room(host)

        def fail_commit(session, *args):
            raise SQLAlchemyError("forced test rollback")

        # before_flush runs after file publication and before metadata can commit.
        from sqlalchemy.orm import Session

        event.listen(Session, "before_flush", fail_commit)
        try:
            response = await upload(host, code, data)
            assert response.status_code == 503
        finally:
            event.remove(Session, "before_flush", fail_commit)
        assert not (pg.settings.case_storage_path / f"{checksum}.detective").exists()
        response = await upload(host, code, data)
        assert response.status_code == 200
        assert (pg.settings.case_storage_path / f"{checksum}.detective").exists()


async def test_shared_digest_not_removed_on_other_room_rollback(pg):
    data = package()
    async with client(pg) as host, client(pg) as other:
        code, other_code = await room(host), await room(other)
        assert (await upload(host, code, data)).status_code == 200
        from sqlalchemy.orm import Session

        def fail_commit(session, *args):
            raise SQLAlchemyError("forced test rollback")

        event.listen(Session, "before_flush", fail_commit)
        try:
            assert (await upload(other, other_code, data)).status_code == 503
        finally:
            event.remove(Session, "before_flush", fail_commit)
        path = pg.settings.case_storage_path / f"{hashlib.sha256(data).hexdigest()}.detective"
        assert path.exists()
        package_storage.verify(path, hashlib.sha256(data).hexdigest())
        assert (await upload(other, other_code, data)).status_code == 200


async def test_file_publish_failure_after_link_is_cleaned_and_no_metadata(pg, monkeypatch):
    data = package()
    async with client(pg) as host:
        code = await room(host)
        original = package_storage.sync_directory
        calls = 0

        def failure(directory):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("forced test directory fsync failure")
            original(directory)

        monkeypatch.setattr(package_storage, "sync_directory", failure)
        response = await upload(host, code, data)
        assert response.status_code == 503
        assert not (
            pg.settings.case_storage_path / f"{hashlib.sha256(data).hexdigest()}.detective"
        ).exists()
        assert not any(c["id"].startswith("upload-") for c in await cases(host, code))


async def test_room_limit_and_invalid_multipart_and_size(pg, monkeypatch):
    async with client(pg) as host:
        code = await room(host)
        monkeypatch.setattr(import_service, "MAX_ROOM_CASES", 1)
        assert (await upload(host, code, package(id_="one"))).status_code == 200
        assert (await upload(host, code, package(id_="two"))).json()["error"][
            "code"
        ] == "CASE_LIMIT"
        response = await host.post(
            f"/api/v1/rooms/{code}/cases/import",
            files=[("file", ("a", b"x")), ("file", ("b", b"x"))],
        )
        assert response.status_code == 422
        response = await host.post(f"/api/v1/rooms/{code}/cases/import", data={"file": "x"})
        assert response.status_code == 422
        response = await host.post(
            f"/api/v1/rooms/{code}/cases/import",
            content=b"bad",
            headers={"Content-Type": "multipart/form-data; boundary=abc"},
        )
        assert response.status_code == 422
        monkeypatch.setattr(import_service, "MAX_BODY_BYTES", 100)
        assert (await upload(host, code, b"x" * 101)).status_code == 413


async def test_scope_start_validation_and_migration_check(pg):
    async with client(pg) as host, client(pg) as other:
        code = await room(host)
        other_code = await room(other)
        id_ = (await upload(host, code, package())).json()["case"]["id"]
        # Even a deliberately corrupted stored selection cannot bypass start's scope check.
        async with pg.app.state.factory.begin() as db:
            from detective.api.db import Room

            stored = await db.scalar(select(Room).where(Room.code == other_code))
            stored.selected_case_id = id_
        response = await other.post(f"/api/v1/rooms/{other_code}/start", json=identity(0))
        assert response.status_code == 409
        await asyncio.to_thread(migrate, pg.env, "check")
        # Downgrade is deliberately refused with imports: never turn them public.
        with pytest.raises(RuntimeError):
            await asyncio.to_thread(migrate, pg.env, "downgrade", "9ab31d5e42c0")
        assert id_ not in {c["id"] for c in (await other.get("/api/v1/cases")).json()}


async def test_start_race_rechecks_lobby_after_validation(pg, monkeypatch):
    begun, release = asyncio.Event(), asyncio.Event()
    original = import_service.validate

    async def waiting(data):
        begun.set()
        await release.wait()
        return await original(data)

    monkeypatch.setattr(import_service, "validate", waiting)
    data = package()
    async with client(pg) as host, client(pg) as guest:
        code = await room(host, guest)
        importing = asyncio.create_task(upload(host, code, data))
        await begun.wait()
        await start(host, code)
        before = digest(await snapshot(host, code))
        release.set()
        response = await importing
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "IMPORT_LOBBY_ONLY"
        assert digest(await snapshot(host, code)) == before
        assert not (
            pg.settings.case_storage_path / f"{hashlib.sha256(data).hexdigest()}.detective"
        ).exists()


async def test_import_busy_and_validation_timeout_kills_worker(pg, monkeypatch):
    original = import_service.validate
    count = 0
    begun, release = asyncio.Event(), asyncio.Event()

    async def waiting(data):
        nonlocal count
        count += 1
        if count == 2:
            begun.set()
        await release.wait()
        return await original(data)

    monkeypatch.setattr(import_service, "validate", waiting)
    async with client(pg) as host:
        code = await room(host)
        tasks = [
            asyncio.create_task(upload(host, code, package(id_=f"case-{i}"))) for i in range(2)
        ]
        await begun.wait()
        rejected = await upload(host, code, package())
        assert rejected.status_code == 503 and rejected.json()["error"]["code"] == "IMPORT_BUSY"
        release.set()
        assert all(r.status_code == 200 for r in await asyncio.gather(*tasks))
        monkeypatch.setattr(import_service, "validate", original)
        monkeypatch.setattr(import_service, "VALIDATION_SECONDS", 0.00001)
        failed = await upload(host, code, package())
        assert failed.status_code == 504 and failed.json()["error"]["code"] == "IMPORT_TIMEOUT"
        assert len([c for c in await cases(host, code) if c["id"].startswith("upload-")]) == 2


async def test_body_wall_timeout_and_chunked_size_limit(pg, monkeypatch):
    async with client(pg) as host:
        code = await room(host)

        async def slow():
            yield b"--abc\r\n"
            await asyncio.sleep(0.05)
            yield b"rest"

        monkeypatch.setattr(import_service, "UPLOAD_SECONDS", 0.01)
        response = await host.post(
            f"/api/v1/rooms/{code}/cases/import",
            content=slow(),
            headers={"Content-Type": "multipart/form-data; boundary=abc"},
        )
        assert response.status_code == 504
        monkeypatch.setattr(import_service, "UPLOAD_SECONDS", 30)
        monkeypatch.setattr(import_service, "MAX_BODY_BYTES", 100)

        async def oversized():
            yield b'--abc\r\nContent-Disposition: form-data; name="file"; filename="a"\r\n\r\n'
            yield b"x" * 101

        response = await host.post(
            f"/api/v1/rooms/{code}/cases/import",
            content=oversized(),
            headers={"Content-Type": "multipart/form-data; boundary=abc"},
        )
        assert response.status_code == 413


async def test_cross_room_concurrent_same_digest_and_cli_reference(pg):
    data = package()
    async with client(pg) as first, client(pg) as second:
        a, b = await room(first), await room(second)
        results = await asyncio.gather(upload(first, a, data), upload(second, b, data))
        assert all(r.status_code == 200 for r in results)
        assert results[0].json()["case"]["id"] != results[1].json()["case"]["id"]
        source = pg.settings.case_storage_path / "operator-input.detective"
        source.write_bytes(data)
        async with pg.app.state.factory.begin() as db:
            assert await catalog.install_case(db, source, pg.settings) == "user-case"
        await import_service.cleanup_unreferenced(
            pg.app.state.factory, pg.settings, hashlib.sha256(data).hexdigest()
        )
        package_storage.verify(
            pg.settings.case_storage_path / f"{hashlib.sha256(data).hexdigest()}.detective",
            hashlib.sha256(data).hexdigest(),
        )


@pytest.mark.parametrize("ambiguous", [False, True])
async def test_commit_failure_cleanup_or_preserve_ambiguous_commit(pg, ambiguous):
    from sqlalchemy.orm import Session

    data = package()
    checksum = hashlib.sha256(data).hexdigest()
    async with client(pg) as host:
        code = await room(host)

        def remember_import(session, *args):
            if any(
                isinstance(row, ScenarioRow) and row.scope_room_id is not None
                for row in session.new
            ):
                session.info["test_import_write"] = True

        def failed_commit(session):
            if session.info.get("test_import_write"):
                raise SQLAlchemyError("forced test commit failure")

        phase = "after_commit" if ambiguous else "before_commit"
        event.listen(Session, "before_flush", remember_import)
        event.listen(Session, phase, failed_commit)
        try:
            response = await upload(host, code, data)
            assert response.status_code == 503
        finally:
            event.remove(Session, "before_flush", remember_import)
            event.remove(Session, phase, failed_commit)
        target = pg.settings.case_storage_path / f"{checksum}.detective"
        assert target.exists() is ambiguous
        response = await upload(host, code, data)
        assert response.status_code == 200
        assert response.json()["already_imported"] is ambiguous
        package_storage.verify(target, checksum)
