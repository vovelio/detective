"""Stage 5: PostgreSQL object concurrency, privacy and durable board projections."""

import asyncio
import hashlib
import json
from uuid import uuid4

import httpx
import pytest
from detective.api.app import create_app
from detective.api.db import BoardObject, Game, GameAction, GameEvent
from sqlalchemy import event, func, select

from .test_api import action, client, playing_room, room, snapshot, start

pytestmark = pytest.mark.asyncio


def digest(value):
    # Failing comparisons must never print private cards from a snapshot.
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def note(text="Public note"):
    return {"op": "note_create", "object_id": str(uuid4()), "x": 15, "y": -20, "text": text}


def transform(card_id, version=0, **values):
    return {
        "op": "card_transform",
        "card_id": card_id,
        "expected_version": version,
        "x": 125,
        "y": -80,
        "rotation": -30,
        **values,
    }


async def board(session, code, payload, **kwargs):
    return await action(session, code, "board_mutation", payload, **kwargs)


async def test_public_card_projection_and_nonactive_equal_rights(pg):
    async with client(pg) as host, client(pg) as guest:
        code = await playing_room(host, guest)
        before = await snapshot(host, code)
        start_id = before["published"][0]["card"]["id"]
        obj = before["board"]["objects"][0]
        assert obj["id"] == "card:" + start_id and obj["version"] == 0
        host_id = (await host.get(f"/api/v1/rooms/{code}")).json()["member_id"]
        nonactive = guest if before["active_player_id"] == host_id else host
        response = await board(nonactive, code, transform(start_id))
        assert response.status_code == 200
        after = await snapshot(guest, code)
        assert after["active_player_id"] == before["active_player_id"]
        assert after["discarded_count"] == before["discarded_count"]
        assert after["deck_count"] == before["deck_count"]
        assert after["published"][0]["position"] == {"x": 125, "y": -80}
        obj = after["board"]["objects"][0]
        assert obj["version"] == 1 and obj["payload"]["rotation"] == 330
        assert (await snapshot(host, code))["board"] == after["board"]


async def test_annotations_create_edit_delete_and_free_arrow(pg):
    async with client(pg) as host, client(pg) as guest:
        code = await playing_room(host, guest)
        n = note("<script>plain text only</script>")
        assert (await board(guest, code, n)).status_code == 200
        stroke_id, arrow_id = str(uuid4()), str(uuid4())
        assert (
            await board(
                host,
                code,
                {
                    "op": "stroke_create",
                    "object_id": stroke_id,
                    "points": [{"x": 1, "y": 2}, {"x": 4, "y": 5}],
                },
            )
        ).status_code == 200
        arrow = {
            "op": "arrow_create",
            "object_id": arrow_id,
            "from": {"x": 700, "y": -500},
            "to": {"x": 990, "y": 80},
        }
        assert (await board(guest, code, arrow)).status_code == 200
        assert (
            await board(
                host,
                code,
                {
                    "op": "note_update",
                    "object_id": n["object_id"],
                    "expected_version": 1,
                    "x": 30,
                    "y": 40,
                    "text": "Updated",
                },
            )
        ).status_code == 200
        objects = (await snapshot(host, code))["board"]["objects"]
        assert next(o for o in objects if o["id"] == arrow_id)["payload"] == {
            "from": arrow["from"],
            "to": arrow["to"],
        }
        for id_, version in ((n["object_id"], 2), (stroke_id, 1), (arrow_id, 1)):
            assert (
                await board(
                    guest,
                    code,
                    {"op": "object_delete", "object_id": id_, "expected_version": version},
                )
            ).status_code == 200
        assert len((await snapshot(host, code))["board"]["objects"]) == 1
        assert (await board(host, code, n)).json()["error"]["code"] == "BOARD_VERSION_CONFLICT"


async def test_concurrent_independent_objects_both_commit_but_same_target_conflicts(pg):
    async with client(pg) as host, client(pg) as guest:
        code = await playing_room(host, guest)
        view = await snapshot(host, code)
        rev = view["revision"]
        results = await asyncio.gather(
            board(host, code, note(), revision=rev), board(guest, code, note(), revision=rev)
        )
        assert [r.status_code for r in results] == [200, 200]
        card_id = view["published"][0]["card"]["id"]
        results = await asyncio.gather(
            board(host, code, transform(card_id), revision=rev),
            board(guest, code, transform(card_id), revision=rev),
        )
        assert sorted(r.status_code for r in results) == [200, 409]
        assert (
            next(r for r in results if r.status_code == 409).json()["error"]["code"]
            == "BOARD_VERSION_CONFLICT"
        )
        # Stale global is allowed for board only, never for a gameplay command.
        assert (await action(host, code, "propose_finish", revision=rev)).json()["error"][
            "code"
        ] == "REVISION_CONFLICT"
        assert (await board(host, code, note(), revision=10_000)).json()["error"][
            "code"
        ] == "REVISION_CONFLICT"
        assert (await snapshot(host, code))["revision"] == rev + 3


async def test_board_idempotency_receipt_and_private_targets(pg):
    async with client(pg) as host, client(pg) as guest, client(pg) as outsider:
        code = await playing_room(host, guest)
        view = await snapshot(host, code)
        secret_id = view["private"]["hand"][0]["id"]
        response = await board(host, code, transform(secret_id))
        assert (
            response.status_code == 409
            and response.json()["error"]["code"] == "BOARD_TARGET_INVALID"
        )
        assert secret_id not in response.text
        assert (await board(outsider, code, note(), revision=view["revision"])).status_code == 403
        payload, action_id = note(), str(uuid4())
        response = await board(host, code, payload, revision=view["revision"], action_id=action_id)
        retry = await board(host, code, payload, revision=view["revision"], action_id=action_id)
        assert response.json() == retry.json() and retry.status_code == 200
        assert len((await snapshot(guest, code))["board"]["objects"]) == 2
        async with pg.app.state.factory() as db:
            events = list(await db.scalars(select(GameEvent)))
            assert all(set(e.public_json) == {"phase", "revision"} for e in events)
            assert secret_id not in json.dumps([e.public_json for e in events])


@pytest.mark.parametrize(
    "payload",
    [
        {"op": "stroke_create", "object_id": str(uuid4()), "points": [{"x": 0, "y": 0}]},
        {"op": "note_create", "object_id": str(uuid4()), "x": 1e9, "y": 0, "text": "x"},
        {"op": "note_create", "object_id": str(uuid4()), "x": 0, "y": 0, "text": ""},
        {"op": "note_create", "object_id": str(uuid4()), "x": True, "y": 0, "text": "x"},
        {
            "op": "note_create",
            "object_id": str(uuid4()),
            "x": 0,
            "y": 0,
            "text": "x",
            "actor_id": "x",
        },
        {
            "op": "arrow_create",
            "object_id": str(uuid4()),
            "from_card_id": "hidden",
            "to_card_id": "hidden2",
        },
    ],
)
async def test_board_strict_invalid_payload_no_revision_change(pg, payload):
    async with client(pg) as host, client(pg) as guest:
        code = await playing_room(host, guest)
        view = await snapshot(host, code)
        assert (await board(host, code, payload)).status_code == 422
        assert (await snapshot(host, code))["revision"] == view["revision"]


async def test_board_rollback_and_new_app_restore(pg):
    async with client(pg) as host, client(pg) as guest:
        code = await playing_room(host, guest)
        payload = note()
        before = await snapshot(host, code)

        def fail_flush(session, flush_context, instances):
            if any(isinstance(row, BoardObject) for row in session.new):
                raise RuntimeError("Injected failure")

        from sqlalchemy.orm import Session

        event.listen(Session, "before_flush", fail_flush)
        try:
            assert (await board(host, code, payload)).status_code == 500
        finally:
            event.remove(Session, "before_flush", fail_flush)
        assert digest(await snapshot(host, code)) == digest(before)
        assert (await board(host, code, payload)).status_code == 200
        stored = await snapshot(host, code)
        app = create_app(pg.settings)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="https://test.local",
                cookies=host.cookies,
            ) as restored:
                assert (await snapshot(restored, code))["board"] == stored["board"]
        finally:
            await app.state.engine.dispose()
        async with pg.app.state.factory() as db:
            assert await db.scalar(select(func.count()).select_from(BoardObject)) == 1
            assert await db.scalar(select(Game.revision)) == before["revision"] + 1
            assert await db.scalar(select(func.count()).select_from(GameAction)) == 3


async def test_board_intro_allowed_lobby_and_final_phase_rejected(pg):
    async with client(pg) as host, client(pg) as guest:
        code = await room(host, guest)
        assert (await board(host, code, note())).json()["error"]["code"] == "GAME_NOT_STARTED"
        await start(host, code)
        assert (await board(guest, code, note())).status_code == 200
        assert (await action(host, code, "begin_play")).status_code == 200
        host_id = (await host.get(f"/api/v1/rooms/{code}")).json()["member_id"]
        while (view := await snapshot(host, code))["phase"] == "PLAYING":
            current = host if view["active_player_id"] == host_id else guest
            hand = (await snapshot(current, code))["private"]["hand"]
            assert (
                await action(
                    current,
                    code,
                    "play_card",
                    {"card_id": hand[0]["id"], "mode": "discard", "confirmed": True},
                )
            ).status_code == 200
        assert (await board(guest, code, note())).status_code == 200
        assert (await action(guest, code, "propose_finish")).status_code == 200
        assert (await action(host, code, "confirm_finish")).status_code == 200
        assert (await board(host, code, note())).json()["error"]["code"] == "BOARD_PHASE_INVALID"


async def test_public_preview_transient_private_target_rejected(pg, live):
    from .test_realtime import peers, receive_type, ws

    async with peers(pg, live) as (host, guest):
        code = await playing_room(host, guest)
        view = await snapshot(host, code)
        card_id = view["published"][0]["card"]["id"]
        async with ws(host, code) as sender, ws(guest, code) as observer:
            await receive_type(sender, "hello")
            await receive_type(observer, "hello")
            preview = {
                "protocol_version": 1,
                "type": "card_preview",
                "card_id": card_id,
                "x": 400,
                "y": 200,
                "rotation": 45,
                "version": 0,
            }
            await sender.send(json.dumps(preview))
            message = await receive_type(observer, "card_preview")
            assert set(message) == {
                "protocol_version",
                "type",
                "member_id",
                "card_id",
                "x",
                "y",
                "rotation",
                "version",
            }
            assert message["member_id"] == view["host_id"]
            assert message["x"] == 400 and message["card_id"] == card_id
            assert digest(await snapshot(host, code)) == digest(view)
            hidden_id = view["private"]["hand"][0]["id"]
            await sender.send(json.dumps({**preview, "card_id": hidden_id}))
            error = await receive_type(sender, "error")
            assert error["code"] == "BOARD_TARGET_INVALID" and hidden_id not in json.dumps(error)
            await sender.send(json.dumps({**preview, "version": 7}))
            assert (await receive_type(sender, "error"))["code"] == "BOARD_VERSION_CONFLICT"
            assert (await board(host, code, transform(card_id))).status_code == 200
            await sender.send(json.dumps(preview))
            assert (await receive_type(sender, "error"))["code"] == "BOARD_VERSION_CONFLICT"
        async with pg.app.state.factory() as db:
            assert await db.scalar(select(func.count()).select_from(BoardObject)) == 1
            assert await db.scalar(select(Game.revision)) == view["revision"] + 1


async def test_preview_rejects_arbitrary_private_fields(pg, live):
    from websockets.exceptions import ConnectionClosed

    from .test_realtime import peers, receive_type, ws

    async with peers(pg, live) as (host, guest):
        code = await playing_room(host, guest)
        view = await snapshot(host, code)
        async with ws(host, code) as sender:
            await receive_type(sender, "hello")
            await sender.send(
                json.dumps(
                    {
                        "protocol_version": 1,
                        "type": "card_preview",
                        "card_id": view["published"][0]["card"]["id"],
                        "x": 0,
                        "y": 0,
                        "rotation": 0,
                        "version": 0,
                        "text": "PRIVATE SENTINEL",
                    }
                )
            )
            try:
                error = await receive_type(sender, "error")
                assert error["code"] == "INVALID_MESSAGE"
            except ConnectionClosed as closed:
                assert closed.rcvd is not None and closed.rcvd.code == 1008
            await sender.wait_closed()
            assert sender.close_code == 1008


async def test_board_migration_previous_head_preserves_game_and_sessions(pg):
    from sqlalchemy import inspect

    from .conftest import migrate

    async with client(pg) as host, client(pg) as guest:
        code = await playing_room(host, guest)
        before = await snapshot(host, code)
        await asyncio.to_thread(migrate, pg.env, "downgrade", "15d25b13ab98")
        async with pg.app.state.engine.connect() as connection:
            assert not await connection.run_sync(
                lambda c: inspect(c).has_table("board_objects", schema=pg.schema)
            )
        # Current code also requires the import/catalog columns added after the
        # board head. An intentionally old schema is not a supported live API.
        assert (await host.get("/health/ready")).status_code == 503
        from sqlalchemy import text

        async with pg.app.state.engine.connect() as connection:
            revision = await connection.scalar(
                text("SELECT revision FROM games WHERE id=:id"), {"id": before["game_id"]}
            )
            assert revision == before["revision"]
        await asyncio.to_thread(migrate, pg.env, "upgrade", "head")
        await asyncio.to_thread(migrate, pg.env, "check")
        assert digest(await snapshot(host, code)) == digest(before)
        assert (await host.get("/health/ready")).status_code == 200


async def test_board_resource_limit_rolls_back_and_delete_releases_active_slot(pg, monkeypatch):
    from detective.api import board as module

    monkeypatch.setattr(module, "MAX_ACTIVE_OBJECTS", 1)
    async with client(pg) as host, client(pg) as guest:
        code = await playing_room(host, guest)
        n = note()
        assert (await board(host, code, n)).status_code == 200
        view = await snapshot(host, code)
        response = await board(guest, code, note())
        assert response.status_code == 409 and response.json()["error"]["code"] == "BOARD_LIMIT"
        assert digest(await snapshot(host, code)) == digest(view)
        assert (
            await board(
                guest,
                code,
                {"op": "object_delete", "object_id": n["object_id"], "expected_version": 1},
            )
        ).status_code == 200
        assert (await board(guest, code, note())).status_code == 200
        monkeypatch.setattr(module, "MAX_TOTAL_OBJECTS", 2)
        response = await board(guest, code, note())
        assert response.json()["error"]["code"] == "BOARD_LIMIT"


async def test_arrow_update_versions_targets_and_deleted_objects(pg):
    async with client(pg) as host, client(pg) as guest:
        code = await playing_room(host, guest)
        arrow_id = str(uuid4())
        original = {
            "op": "arrow_create",
            "object_id": arrow_id,
            "from": {"x": 10, "y": 20},
            "to": {"x": 100, "y": 200},
        }
        assert (await board(host, code, original)).status_code == 200
        update = {
            **original,
            "op": "arrow_update",
            "expected_version": 1,
            "from": {"x": -30, "y": 50},
            "to": {"x": 220, "y": 300},
        }
        assert (await board(guest, code, update)).status_code == 200
        assert (await board(host, code, update)).status_code == 409
        obj = next(
            o for o in (await snapshot(host, code))["board"]["objects"] if o["id"] == arrow_id
        )
        assert obj["version"] == 2
        assert obj["payload"] == {"from": update["from"], "to": update["to"]}
        n = note()
        assert (await board(host, code, n)).status_code == 200
        assert (await board(host, code, {**update, "object_id": n["object_id"]})).status_code == 409
        assert (await board(host, code, {**update, "object_id": str(uuid4())})).status_code == 409
        assert (
            await board(host, code, {**update, "expected_version": 2, "to": {"x": 1000001, "y": 0}})
        ).status_code == 422
        assert (
            await board(
                host, code, {"op": "object_delete", "object_id": arrow_id, "expected_version": 2}
            )
        ).status_code == 200
        assert (await board(host, code, {**update, "expected_version": 3})).status_code == 409
