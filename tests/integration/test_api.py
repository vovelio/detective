import asyncio
import json
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from detective.api.db import AnonymousSession, Game, GameAction, GameEvent
from sqlalchemy import event, func, inspect, select
from sqlalchemy.exc import SQLAlchemyError

from .conftest import ROOT, migrate

pytestmark = pytest.mark.asyncio


@asynccontextmanager
async def client(pg, *, base_url="https://test.local", transport=True):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pg.app, raise_app_exceptions=False)
        if transport
        else None,
        base_url=base_url,
        headers={"Origin": base_url},
    ) as session:
        response = await session.post("/api/v1/sessions", json={})
        assert response.status_code == 200
        session.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        yield session


async def room(host, guest=None, *, case_id="envelope-demo"):
    response = await host.post("/api/v1/rooms", json={"nickname": " Ведущий "})
    assert response.status_code == 200
    info = response.json()
    code = info["code"]
    if guest:
        response = await guest.post(f"/api/v1/rooms/{code}/join", json={"nickname": "Участник"})
        assert response.status_code == 200
    response = await host.post(f"/api/v1/rooms/{code}/cases/select", json={"case_id": case_id})
    assert response.status_code == 200
    return code


def identity(revision, action_id=None):
    return {"expected_revision": revision, "client_action_id": action_id or str(uuid4())}


async def snapshot(session, code):
    response = await session.get(f"/api/v1/rooms/{code}/snapshot")
    assert response.status_code == 200
    return response.json()


async def action(session, code, type_, payload=None, *, revision=None, action_id=None):
    if revision is None:
        revision = (await snapshot(session, code))["revision"]
    return await session.post(
        f"/api/v1/rooms/{code}/actions",
        json={
            **identity(revision, action_id),
            "type": type_,
            "payload": payload or {},
        },
    )


async def start(host, code):
    response = await host.post(f"/api/v1/rooms/{code}/start", json=identity(0))
    assert response.status_code == 200
    return response


async def play(host, guest, code):
    code_view = await snapshot(host, code)
    host_id = (await host.get(f"/api/v1/rooms/{code}")).json()["member_id"]
    current = host if code_view["active_player_id"] == host_id else guest
    view = await snapshot(current, code)
    payload = {"card_id": view["private"]["hand"][0]["id"], "mode": "discard", "confirmed": True}
    return current, payload


async def playing_room(host, guest, *, case_id="envelope-demo"):
    code = await room(host, guest, case_id=case_id)
    await start(host, code)
    assert (await action(host, code, "begin_play")).status_code == 200
    return code


def all_keys(value):
    if isinstance(value, dict):
        return set(value) | set().union(*(all_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(all_keys(v) for v in value))
    return set()


async def test_session_cookie_memberships_and_rejoin(pg):
    async with client(pg) as host, client(pg) as guest:
        cookie = next(iter(host.cookies.jar))
        assert cookie.secure and cookie.has_nonstandard_attr("HttpOnly")
        assert cookie.get_nonstandard_attr("SameSite") == "lax"
        assert cookie.name.startswith("__Host-") and cookie.path == "/"
        code = await room(host, guest)
        original = await snapshot(guest, code)
        repeated = await guest.post(f"/api/v1/rooms/{code}/join", json={"nickname": "Другое имя"})
        assert repeated.json() == original
        boot = await guest.post("/api/v1/sessions", json={})
        assert (
            boot.status_code == 200
            and boot.json()["rooms"][0]["member_id"] == original["member_id"]
        )
        assert len((await snapshot(host, code))["players"]) == 2
        async with pg.app.state.factory() as db:
            sessions = list(await db.scalars(select(AnonymousSession)))
            assert len(sessions) == 2
            assert all(len(s.token_hash) == 64 and s.token_hash != cookie.value for s in sessions)
        await start(host, code)
        assert (
            await guest.post(f"/api/v1/rooms/{code}/join", json={"nickname": "Участник"})
        ).status_code == 200


async def test_room_permissions_and_identity_spoof(pg):
    async with client(pg) as host, client(pg) as guest, client(pg) as outsider:
        code = await room(host, guest)
        for suffix in ("", "/snapshot", "/result", "/assets/photo-00"):
            response = await outsider.get(f"/api/v1/rooms/{code}{suffix}")
            assert response.status_code == 403
        assert (
            await guest.post(f"/api/v1/rooms/{code}/start", json=identity(0))
        ).status_code == 403
        assert (
            await guest.post(
                f"/api/v1/rooms/{code}/cases/select", json={"case_id": "envelope-demo"}
            )
        ).status_code == 403
        await start(host, code)
        assert (
            await outsider.post(f"/api/v1/rooms/{code}/join", json={"nickname": "Новый"})
        ).status_code == 409
        forged = {**identity(1), "type": "begin_play", "payload": {}, "actor_id": "host"}
        response = await guest.post(f"/api/v1/rooms/{code}/actions", json=forged)
        assert response.status_code == 422 and "actor_id" not in response.text
        response = await action(guest, code, "begin_play", {"actor_id": "host"})
        assert response.status_code == 422
        assert (await action(guest, code, "begin_play")).status_code == 403
        # Replacing the cookie with a public member UUID cannot authenticate a session.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(pg.app), base_url="https://test.local"
        ) as forged_client:
            member_id = (await host.get(f"/api/v1/rooms/{code}")).json()["member_id"]
            forged_client.cookies.set(pg.settings.cookie_name, member_id)
            assert (await forged_client.get(f"/api/v1/rooms/{code}/snapshot")).status_code == 401


@pytest.mark.parametrize("nickname", ["a", " " * 5, "x" * 21, "abc\nxyz", "xx\x00yy", "ab\u200bcd"])
async def test_invalid_nicknames(pg, nickname):
    async with client(pg) as host:
        response = await host.post("/api/v1/rooms", json={"nickname": nickname})
        assert response.status_code == 422
        assert "nickname" not in response.text


async def test_duplicate_nickname_capacity_and_parallel_join(pg):
    async with client(pg) as host, client(pg) as guest:
        code = await room(host)
        response = await guest.post(f"/api/v1/rooms/{code}/join", json={"nickname": "ведущий"})
        assert response.status_code == 409

        async def join(index):
            async with client(pg) as participant:
                return await participant.post(
                    f"/api/v1/rooms/{code}/join", json={"nickname": f"Игрок {index}"}
                )

        joined = await asyncio.gather(*(join(i) for i in range(10)))
        assert sum(r.status_code == 200 for r in joined) == 7
        assert sum(r.status_code == 409 for r in joined) == 3
        view = await snapshot(host, code)
        assert [p["seat"] for p in view["players"]] == list(range(8))


async def test_origin_csrf_body_limits_and_sanitized_errors(pg):
    transport = httpx.ASGITransport(pg.app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test.local") as unknown:
        assert (await unknown.post("/api/v1/sessions", json={})).status_code == 403
        assert (
            await unknown.post(
                "/api/v1/sessions", json={}, headers={"Origin": "https://evil.local"}
            )
        ).status_code == 403
    async with client(pg) as host, client(pg) as guest:
        response = await host.post(
            "/api/v1/rooms", json={"nickname": "Ведущий"}, headers={"X-CSRF-Token": "incorrect"}
        )
        assert response.status_code == 403
        response = await host.post(
            "/api/v1/rooms",
            json={"nickname": "Ведущий"},
            headers={"X-CSRF-Token": guest.headers["X-CSRF-Token"]},
        )
        assert response.status_code == 403
        response = await host.post(
            "/api/v1/rooms", content=b"x" * 40000, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 413
        response = await host.post(
            "/api/v1/rooms",
            content='{"nickname": "SECRET_SENTINEL",',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422 and "SECRET_SENTINEL" not in response.text
        assert response.headers["cache-control"] == "no-store, private"
        assert (await host.post("/api/v1/rooms", data={"nickname": "Ведущий"})).status_code == 415


async def test_expired_or_missing_cookie_cannot_recover(pg):
    async with client(pg) as host:
        code = await room(host)
        async with pg.app.state.factory.begin() as db:
            session = await db.scalar(select(AnonymousSession))
            session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        assert (await host.get(f"/api/v1/rooms/{code}/snapshot")).status_code == 401
        assert (await host.post("/api/v1/sessions", json={})).status_code == 401
    async with client(pg) as new:
        assert (await new.get(f"/api/v1/rooms/{code}/snapshot")).status_code == 403


async def test_duplicate_start_does_not_redeal(pg):
    async with client(pg) as host, client(pg) as guest:
        code = await room(host, guest)
        body = identity(0)
        responses = await asyncio.gather(
            *(host.post(f"/api/v1/rooms/{code}/start", json=body) for _ in range(2))
        )
        assert all(r.status_code == 200 for r in responses)
        assert responses[0].json() == responses[1].json()
        before = await snapshot(host, code)
        assert before["revision"] == 1
        assert (await host.post(f"/api/v1/rooms/{code}/start", json=body)).json() == responses[
            0
        ].json()
        assert await snapshot(host, code) == before
        different = await host.post(f"/api/v1/rooms/{code}/start", json=identity(1))
        assert different.status_code == 409


async def test_duplicate_action_and_conflicting_revisions(pg):
    async with client(pg) as host, client(pg) as guest:
        code = await playing_room(host, guest)
        current, payload = await play(host, guest, code)
        revision = (await snapshot(current, code))["revision"]
        action_id = str(uuid4())
        responses = await asyncio.gather(
            *(
                action(current, code, "play_card", payload, revision=revision, action_id=action_id)
                for _ in range(2)
            )
        )
        assert all(r.status_code == 200 for r in responses)
        assert responses[0].json() == responses[1].json()
        view = await snapshot(current, code)
        assert view["revision"] == revision + 1 and view["discarded_count"] == 1
        # A lost response can be retried after other successful actions; old receipt is stable.
        next_player, next_payload = await play(host, guest, code)
        await action(next_player, code, "play_card", next_payload)
        retry = await action(
            current, code, "play_card", payload, revision=revision, action_id=action_id
        )
        assert retry.json() == responses[0].json()
        reused = await action(
            current,
            code,
            "play_card",
            {**payload, "confirmed": False},
            revision=revision,
            action_id=action_id,
        )
        assert reused.status_code == 409 and reused.json()["error"]["code"] == "ACTION_ID_REUSED"
        current, payload = await play(host, guest, code)
        revision = (await snapshot(current, code))["revision"]
        conflicts = await asyncio.gather(
            *(action(current, code, "play_card", payload, revision=revision) for _ in range(2))
        )
        assert sorted(r.status_code for r in conflicts) == [200, 409]
        rejected = next(r for r in conflicts if r.status_code == 409)
        assert rejected.json()["revision"] == revision + 1
        assert rejected.json()["error"]["code"] == "REVISION_CONFLICT"
        async with pg.app.state.factory() as db:
            count = await db.scalar(select(func.count()).select_from(GameAction))
            assert count == 5  # start, begin, and three actual turns


async def test_invalid_action_cancel_and_atomic_rollback(pg):
    async with client(pg) as host, client(pg) as guest:
        code = await playing_room(host, guest)
        current, payload = await play(host, guest, code)
        before = await snapshot(current, code)
        other = guest if current is host else host
        other_view = await snapshot(other, code)
        response = await action(
            current,
            code,
            "play_card",
            {**payload, "card_id": other_view["private"]["hand"][0]["id"]},
        )
        assert response.status_code == 409
        assert await snapshot(current, code) == before
        cancelled = await action(current, code, "play_card", {**payload, "confirmed": False})
        assert cancelled.status_code == 200 and cancelled.json()["revision"] == before["revision"]
        assert await snapshot(current, code) == before
        response = await action(
            current, code, "play_card", payload, revision=before["revision"] - 1
        )
        assert response.status_code == 409
        async with pg.app.state.factory() as db:
            count_before = await db.scalar(select(func.count()).select_from(GameAction))
            events_before = await db.scalar(select(func.count()).select_from(GameEvent))
        fired = []

        def fail_after_update(mapper, connection, target):
            fired.append(True)
            raise SQLAlchemyError("INJECTED_FAILURE_AFTER_SQL_UPDATE")

        event.listen(Game, "after_update", fail_after_update)
        try:
            response = await action(current, code, "play_card", payload)
            assert response.status_code == 503 and fired
        finally:
            event.remove(Game, "after_update", fail_after_update)
        assert await snapshot(current, code) == before
        async with pg.app.state.factory() as db:
            assert await db.scalar(select(func.count()).select_from(GameAction)) == count_before
            assert await db.scalar(select(func.count()).select_from(GameEvent)) == events_before
        assert (await action(current, code, "play_card", payload)).status_code == 200


async def test_secret_hands_assets_and_publication(pg):
    async with client(pg) as host, client(pg) as guest, client(pg) as outsider:
        code = await playing_room(host, guest, case_id="media-demo")
        current, discard = await play(host, guest, code)
        other = guest if current is host else host
        view = await snapshot(current, code)
        card = view["private"]["hand"][0]
        photo_id = card["blocks"][-1]["asset_id"]
        endpoint = f"/api/v1/rooms/{code}/assets/{photo_id}"
        response = await current.get(endpoint)
        assert response.status_code == 200 and response.headers["content-type"] == "image/png"
        assert response.headers["cache-control"] == "no-store, private"
        assert response.headers["cross-origin-resource-policy"] == "same-origin"
        assert (await other.get(endpoint)).status_code == 404
        assert (await outsider.get(endpoint)).status_code == 403
        for player in (current, other):
            projection = await snapshot(player, code)
            assert not {
                "role",
                "logic_notes",
                "supports_questions",
                "correct_option_id",
                "solution",
                "manifest_json",
                "token_hash",
            } & all_keys(projection)
        other_serialized = json.dumps(await snapshot(other, code), ensure_ascii=False)
        assert card["id"] not in other_serialized and card["title"] not in other_serialized
        assert (await action(current, code, "play_card", discard)).status_code == 200
        assert (await current.get(endpoint)).status_code == 404
        assert (await other.get(endpoint)).status_code == 404
        current, payload = await play(host, guest, code)
        card = (await snapshot(current, code))["private"]["hand"][0]
        photo_id = card["blocks"][-1]["asset_id"]
        response = await action(
            current, code, "play_card", {**payload, "mode": "reveal", "x": 5, "y": 10}
        )
        assert response.status_code == 200
        for player in (host, guest):
            projection = await snapshot(player, code)
            assert projection["published"][-1]["position"] == {"x": 5.0, "y": 10.0}
            assert (await player.get(f"/api/v1/rooms/{code}/assets/{photo_id}")).status_code == 200
        assert (await host.get(f"/api/v1/rooms/{code}/result")).status_code == 409
        assert (await host.get("/docs/examples/demo-case.detective")).status_code == 404


async def test_full_api_game_review_and_results(pg):
    async with client(pg) as host, client(pg) as guest:
        code = await playing_room(host, guest)
        for _ in range(16):
            current, payload = await play(host, guest, code)
            assert (await action(current, code, "play_card", payload)).status_code == 200
        assert (await snapshot(host, code))["phase"] == "DISCUSSION"
        assert (await action(guest, code, "propose_finish")).status_code == 200
        assert (await action(host, code, "confirm_finish")).status_code == 200
        for i in range(1, 5):
            assert (
                await action(guest, code, "answer", {"question_id": f"q{i}", "option_id": "a"})
            ).status_code == 200
        assert (await action(host, code, "submit_answers")).status_code == 200
        host_view, guest_view = await snapshot(host, code), await snapshot(guest, code)
        assert "answer_key" in host_view["private"] and "correct_option_id" in all_keys(host_view)
        assert "correct_option_id" not in all_keys(guest_view)
        assert "solution" not in all_keys(host_view) | all_keys(guest_view)
        assert "role" not in all_keys(host_view) | all_keys(guest_view)
        assert (
            await action(guest, code, "review_answer", {"question_id": "q1", "correct": True})
        ).status_code == 403
        assert (
            await action(host, code, "answer", {"question_id": "q1", "option_id": "b"})
        ).status_code == 409
        for i in range(1, 5):
            assert (
                await action(host, code, "review_answer", {"question_id": f"q{i}", "correct": True})
            ).status_code == 200
        result = (await guest.get(f"/api/v1/rooms/{code}/result")).json()
        assert result["result"]["score"] == 8 and "solution" in result
        assert (await action(host, code, "finish_game")).status_code == 200
        assert (await snapshot(guest, code))["phase"] == "FINISHED"
        assert (await action(host, code, "begin_play")).status_code == 409


async def test_migration_roundtrip_and_metadata(pg):
    await asyncio.to_thread(migrate, pg.env, "check")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(pg.app), base_url="https://test.local"
    ) as probe:
        assert (await probe.get("/health/ready")).status_code == 200
    await asyncio.to_thread(migrate, pg.env, "downgrade", "base")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(pg.app), base_url="https://test.local"
    ) as probe:
        assert (await probe.get("/health/ready")).status_code == 503
    async with pg.app.state.engine.connect() as connection:
        tables = await connection.run_sync(lambda c: inspect(c).get_table_names(schema=pg.schema))
        assert set(tables) <= {"alembic_version"}
    await asyncio.to_thread(migrate, pg.env, "upgrade", "head")
    await asyncio.to_thread(migrate, pg.env, "check")


async def test_waits_for_actual_postgres_row_lock(pg):
    async with client(pg) as host, client(pg) as guest:
        code = await playing_room(host, guest)
        current, payload = await play(host, guest, code)
        revision = (await snapshot(current, code))["revision"]
        async with pg.app.state.factory.begin() as blocker:
            await blocker.scalar(select(Game).with_for_update())
            task = asyncio.create_task(
                action(current, code, "play_card", payload, revision=revision)
            )
            await asyncio.sleep(0.1)
            assert not task.done()
        response = await asyncio.wait_for(task, timeout=5)
        assert response.status_code == 200


async def test_restart_real_uvicorn_process_preserves_cookie_and_game(pg):
    # Both runs are real independent processes against PostgreSQL, bound only to loopback.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    env = {**pg.env, "ALLOWED_ORIGINS": origin, "SESSION_COOKIE_SECURE": "false"}
    process = None

    async def launch():
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "detective.api.app:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-access-log",
                "--log-level",
                "critical",
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        async with httpx.AsyncClient(base_url=origin) as probe:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("Local test API process exited")
                try:
                    if (await probe.get("/health/ready")).status_code == 200:
                        return process
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
        process.terminate()
        process.wait(timeout=5)
        raise RuntimeError("Local test API did not become ready")

    try:
        process = await launch()
        async with (
            client(pg, base_url=origin, transport=False) as host,
            client(pg, base_url=origin, transport=False) as guest,
        ):
            code = await playing_room(host, guest)
            last = None
            for _ in range(3):
                current, payload = await play(host, guest, code)
                revision = (await snapshot(current, code))["revision"]
                action_id = str(uuid4())
                receipt = await action(
                    current, code, "play_card", payload, revision=revision, action_id=action_id
                )
                assert receipt.status_code == 200
                last = (current, payload, revision, action_id, receipt.json())
            before_host, before_guest = await snapshot(host, code), await snapshot(guest, code)
            process.kill()  # simulate abrupt server death after committed actions
            await asyncio.to_thread(process.wait, timeout=5)
            process = await launch()
            assert await snapshot(host, code) == before_host
            assert await snapshot(guest, code) == before_guest
            resumed = await host.post("/api/v1/sessions", json={})
            assert resumed.status_code == 200
            assert resumed.json()["csrf_token"] == host.headers["X-CSRF-Token"]
            assert resumed.json()["rooms"][0]["host_id"] == before_host["host_id"]
            current, payload, revision, action_id, receipt = last
            retry = await action(
                current, code, "play_card", payload, revision=revision, action_id=action_id
            )
            assert retry.json() == receipt
            assert await snapshot(host, code) == before_host
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, timeout=5)


async def test_start_prerequisites_and_cross_room_isolation(pg):
    async with client(pg) as host, client(pg) as guest, client(pg) as other:
        response = await host.post("/api/v1/rooms", json={"nickname": "Ведущий"})
        code = response.json()["code"]
        assert (await host.post(f"/api/v1/rooms/{code}/start", json=identity(0))).status_code == 409
        await host.post(f"/api/v1/rooms/{code}/cases/select", json={"case_id": "envelope-demo"})
        response = await host.post(f"/api/v1/rooms/{code}/start", json=identity(0))
        assert response.status_code == 409
        assert (await snapshot(host, code))["revision"] == 0
        async with pg.app.state.factory() as db:
            assert await db.scalar(select(func.count()).select_from(GameAction)) == 0
        await guest.post(f"/api/v1/rooms/{code}/join", json={"nickname": "Участник"})
        other_code = await room(other)
        other_before = await snapshot(other, other_code)
        await start(host, code)
        assert (await action(host, code, "begin_play")).status_code == 200
        current, payload = await play(host, guest, code)
        assert (await action(current, code, "play_card", payload)).status_code == 200
        assert await snapshot(other, other_code) == other_before
        response = await current.get(f"/api/v1/rooms/{other_code}/snapshot")
        assert response.status_code == 403
        # Query parameters do not choose the viewer; membership is always resolved by cookie.
        victim = (await guest.get(f"/api/v1/rooms/{code}")).json()["member_id"]
        own = await snapshot(host, code)
        response = await host.get(f"/api/v1/rooms/{code}/snapshot", params={"member_id": victim})
        assert response.json() == own
        response = await host.post(
            f"/api/v1/rooms/{code}/cases/select", json={"case_id": "media-demo"}
        )
        assert response.status_code == 409


async def test_concurrent_team_answers_from_different_sessions(pg):
    async with client(pg) as host, client(pg) as guest:
        code = await playing_room(host, guest)
        for _ in range(16):
            current, payload = await play(host, guest, code)
            assert (await action(current, code, "play_card", payload)).status_code == 200
        await action(host, code, "propose_finish")
        await action(host, code, "confirm_finish")
        revision = (await snapshot(host, code))["revision"]
        replies = await asyncio.gather(
            action(
                host, code, "answer", {"question_id": "q1", "option_id": "a"}, revision=revision
            ),
            action(
                guest, code, "answer", {"question_id": "q1", "option_id": "b"}, revision=revision
            ),
        )
        assert sorted(r.status_code for r in replies) == [200, 409]
        view = await snapshot(host, code)
        assert view["revision"] == revision + 1 and len(view["answers"]) == 1
        assert "correct_option_id" not in all_keys(view)


async def test_review_progress_is_private_and_survives_new_snapshot(pg):
    async with client(pg) as host, client(pg) as guest:
        code = await playing_room(host, guest)
        for _ in range(16):
            current, payload = await play(host, guest, code)
            assert (await action(current, code, "play_card", payload)).status_code == 200
        await action(guest, code, "propose_finish")
        await action(host, code, "confirm_finish")
        for index in range(1, 5):
            await action(guest, code, "answer", {"question_id": f"q{index}", "option_id": "a"})
        await action(host, code, "submit_answers")
        await action(host, code, "review_answer", {"question_id": "q1", "correct": True})
        host_view, guest_view = await snapshot(host, code), await snapshot(guest, code)
        restored_mark = next(
            a["reviewed_correct"]
            for a in host_view["private"]["reviewed_answers"]
            if a["question_id"] == "q1"
        )
        assert restored_mark is True
        assert "reviewed_answers" not in guest_view["private"]
        assert "correct_option_id" not in all_keys(guest_view)
        assert "solution" not in all_keys(host_view)
