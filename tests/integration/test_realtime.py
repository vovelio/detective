"""Real Uvicorn + PostgreSQL + independent cookie/WebSocket clients."""

import asyncio
import json
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from detective.api import service
from detective.api.contracts import ActionIdentity
from detective.api.db import Game, GameEvent, Member
from detective.game_engine import commands as c
from sqlalchemy import delete, select
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .test_api import action, all_keys, client, play, playing_room, room, snapshot, start

pytestmark = pytest.mark.asyncio


def ws(session, code, *, origin=None, headers=None, query="", protocols=None):
    cookie = "; ".join(f"{c.name}={c.value}" for c in session.cookies.jar)
    return connect(
        str(session.base_url).replace("http", "ws", 1).rstrip("/") + f"/ws/v1/rooms/{code}" + query,
        origin=origin or str(session.base_url).rstrip("/"),
        subprotocols=["detective.v1"] if protocols is None else protocols,
        additional_headers={"Cookie": cookie, **(headers or {})},
        proxy=None,
        compression=None,
        max_size=8192,
        close_timeout=1,
    )


async def receive_type(socket, kind, *, predicate=lambda m: True, timeout=3):
    async def receive():
        while True:
            message = json.loads(await socket.recv())
            assert message["protocol_version"] == 1
            if message["type"] == "ping":
                await socket.send(json.dumps({"protocol_version": 1, "type": "pong"}))
            if message["type"] == kind and predicate(message):
                return message

    return await asyncio.wait_for(receive(), timeout)


@asynccontextmanager
async def peers(pg, live):
    async with (
        client(pg, base_url=live.origin, transport=False) as host,
        client(pg, base_url=live.origin, transport=False) as guest,
    ):
        yield host, guest


async def test_handshake_and_two_participant_notifications(pg, live):
    async with peers(pg, live) as (host, guest):
        code = await room(host, guest)
        async with ws(host, code) as host_ws, ws(guest, code) as guest_ws:
            host_hello = await receive_type(host_ws, "hello")
            guest_hello = await receive_type(guest_ws, "hello")
            assert host_hello["revision"] == guest_hello["revision"] == 0
            assert "snapshot_url" in host_hello and "private" not in host_hello
            presence = await receive_type(
                host_ws, "presence", predicate=lambda m: sum(p["online"] for p in m["players"]) == 2
            )
            assert len(presence["players"]) == 2
            await start(host, code)
            for socket in (host_ws, guest_ws):
                event = await receive_type(socket, "state_changed")
                assert event == {
                    "protocol_version": 1,
                    "type": "state_changed",
                    "revision": 1,
                    "phase": "INTRO",
                }
            assert (await action(host, code, "begin_play")).status_code == 200
            for socket in (host_ws, guest_ws):
                event = await receive_type(socket, "state_changed")
                assert event["revision"] == 2 and event["phase"] == "PLAYING"
            await host_ws.send(json.dumps({"protocol_version": 1, "type": "resume", "revision": 2}))
            assert (await receive_type(host_ws, "synced"))["revision"] == 2


@pytest.mark.parametrize("invalid", ["outsider", "origin", "host", "query", "protocol", "cookie"])
async def test_handshake_rejection(pg, live, invalid):
    async with peers(pg, live) as (host, guest):
        code = await room(host)
        selected, kwargs = host, {}
        if invalid == "outsider":
            selected = guest
        elif invalid == "origin":
            kwargs["origin"] = "https://untrusted.invalid"
        elif invalid == "host":
            kwargs["headers"] = {"Host": "untrusted.invalid"}
        elif invalid == "query":
            kwargs["query"] = "?token=DISPOSABLE_TEST_SENTINEL"
        elif invalid == "protocol":
            kwargs["protocols"] = ["unsupported.v99"]
        elif invalid == "cookie":
            kwargs["headers"] = {"Cookie": "detective_session=" + "A" * 43}
        with pytest.raises(InvalidStatus) as error:
            async with ws(selected, code, **kwargs):
                pytest.fail("Rejected handshake was accepted")
        assert error.value.response.status_code in ((400, 403) if invalid == "host" else (403,))


async def test_lobby_changes_presence_tabs_and_host_disconnect(pg, live):
    async with peers(pg, live) as (host, guest):
        response = await host.post("/api/v1/rooms", json={"nickname": "Ведущий"})
        code = response.json()["code"]
        host_id = response.json()["host_id"]
        async with ws(host, code) as first, ws(host, code) as second:
            await receive_type(first, "hello")
            await receive_type(second, "hello")
            await guest.post(f"/api/v1/rooms/{code}/join", json={"nickname": "Участник"})
            changed = await receive_type(first, "room_changed")
            assert changed["revision"] == 0
            view = await snapshot(host, code)
            assert len(view["players"]) == 2
            await host.post(f"/api/v1/rooms/{code}/cases/select", json={"case_id": "envelope-demo"})
            await receive_type(first, "room_changed")
            async with ws(guest, code) as guest_ws:
                await receive_type(guest_ws, "hello")
                await start(host, code)
                assert (await action(host, code, "begin_play")).status_code == 200
                before = await snapshot(host, code)
                await first.close()
                # Closing one of two tabs must not make the host offline.
                await second.send(json.dumps({"protocol_version": 1, "type": "ping"}))
                await receive_type(second, "pong")
                await second.close()
                offline = await receive_type(
                    guest_ws,
                    "presence",
                    predicate=lambda m: any(
                        p["member_id"] == host_id and not p["online"] for p in m["players"]
                    ),
                )
                assert offline
                after = await snapshot(host, code)
                assert after == before
                async with ws(host, code) as returned:
                    hello = await receive_type(returned, "hello")
                    assert hello["member_id"] == host_id
                    unchanged = await snapshot(host, code) == before
                    assert unchanged


async def test_private_hands_resources_publication_and_discard(pg, live):
    async with peers(pg, live) as (host, guest):
        code = await playing_room(host, guest, case_id="media-demo")
        async with ws(host, code) as a, ws(guest, code) as b:
            await receive_type(a, "hello")
            await receive_type(b, "hello")
            before = await snapshot(host, code)
            current, payload = await play(host, guest, code)
            other = guest if current is host else host
            current_view = await snapshot(current, code)
            private = current_view["private"]["hand"][0]
            asset = private["blocks"][-1]["asset_id"]
            other_before = json.dumps(await snapshot(other, code), ensure_ascii=False)
            no_foreign_content = (
                private["id"] not in other_before and private["title"] not in other_before
            )
            assert no_foreign_content
            assert (await other.get(f"/api/v1/rooms/{code}/assets/{asset}")).status_code == 404
            assert (await action(current, code, "play_card", payload)).status_code == 200
            for socket in (a, b):
                event = await receive_type(socket, "state_changed")
                assert set(event) == {"protocol_version", "type", "revision", "phase"}
                assert event["revision"] == before["revision"] + 1
            assert (await current.get(f"/api/v1/rooms/{code}/assets/{asset}")).status_code == 404
            assert (await snapshot(current, code))["active_player_id"] != before["active_player_id"]
            current, payload = await play(host, guest, code)
            card = (await snapshot(current, code))["private"]["hand"][0]
            assert (
                await action(
                    current, code, "play_card", {**payload, "mode": "reveal", "x": 1, "y": 2}
                )
            ).status_code == 200
            for socket, participant in ((a, host), (b, guest)):
                event = await receive_type(socket, "state_changed")
                assert not {
                    "role",
                    "logic_notes",
                    "private",
                    "correct_option_id",
                    "solution",
                } & all_keys(event)
                view = await snapshot(participant, code)
                assert view["published"][-1]["card"]["id"] == card["id"]
                assert (
                    await participant.get(
                        f"/api/v1/rooms/{code}/assets/{card['blocks'][-1]['asset_id']}"
                    )
                ).status_code == 200


async def test_isolation_cursor_and_forbidden_payload(pg, live):
    async with peers(pg, live) as (host, guest):
        code = await room(host, guest)
        async with client(pg, base_url=live.origin, transport=False) as outsider:
            other_code = await room(outsider)
            async with ws(host, code) as a, ws(guest, code) as b, ws(outsider, other_code) as other:
                await receive_type(a, "hello")
                await receive_type(b, "hello")
                await receive_type(other, "hello")
                await a.send(
                    json.dumps({"protocol_version": 1, "type": "cursor", "x": 12, "y": -4})
                )
                cursor = await receive_type(b, "cursor")
                assert cursor["x"] == 12 and cursor["y"] == -4
                assert (
                    cursor["member_id"]
                    == (await host.get(f"/api/v1/rooms/{code}")).json()["member_id"]
                )
                await start(host, code)
                await receive_type(b, "state_changed")
                await other.send(
                    json.dumps({"protocol_version": 1, "type": "resume", "revision": 0})
                )
                assert (await receive_type(other, "synced"))["revision"] == 0
                with pytest.raises(TimeoutError):
                    await receive_type(other, "state_changed", timeout=0.4)
                await a.send(
                    json.dumps(
                        {
                            "protocol_version": 1,
                            "type": "cursor",
                            "x": 0,
                            "y": 0,
                            "actor_id": "SPOOF",
                            "text": "PRIVATE_SENTINEL",
                        }
                    )
                )
                with pytest.raises(ConnectionClosed):
                    while True:
                        await a.recv()
                assert "PRIVATE_SENTINEL" not in json.dumps(await snapshot(guest, code))


async def test_reconnect_missed_revisions_and_stale_client(pg, live):
    async with peers(pg, live) as (host, guest):
        code = await playing_room(host, guest)
        async with ws(guest, code) as socket:
            baseline = (await receive_type(socket, "hello"))["revision"]
        for _ in range(3):
            current, payload = await play(host, guest, code)
            assert (await action(current, code, "play_card", payload)).status_code == 200
        before = await snapshot(guest, code)
        async with ws(guest, code) as returned:
            hello = await receive_type(returned, "hello")
            assert hello["revision"] == baseline + 3
            await returned.send(
                json.dumps({"protocol_version": 1, "type": "resume", "revision": baseline})
            )
            required = await receive_type(returned, "sync_required")
            assert (
                required["revision"] == before["revision"] and required["reason"] == "missed_events"
            )
            unchanged = await snapshot(guest, code) == before
            assert unchanged
            await returned.send(
                json.dumps(
                    {"protocol_version": 1, "type": "resume", "revision": before["revision"]}
                )
            )
            await receive_type(returned, "synced")
            await returned.send(
                json.dumps(
                    {"protocol_version": 1, "type": "resume", "revision": before["revision"] + 5}
                )
            )
            assert (await receive_type(returned, "sync_required"))["reason"] == "client_ahead"
            unchanged = await snapshot(guest, code) == before
            assert unchanged


async def test_commit_only_retries_conflicts_and_rollback(pg, live):
    async with peers(pg, live) as (host, guest):
        code = await playing_room(host, guest)
        async with ws(guest, code) as socket:
            await receive_type(socket, "hello")
            current, payload = await play(host, guest, code)
            revision = (await snapshot(current, code))["revision"]
            action_id = str(uuid4())
            replies = await asyncio.gather(
                *(
                    action(
                        current, code, "play_card", payload, revision=revision, action_id=action_id
                    )
                    for _ in range(2)
                )
            )
            assert all(r.status_code == 200 for r in replies)
            assert (await receive_type(socket, "state_changed"))["revision"] == revision + 1
            with pytest.raises(TimeoutError):
                await receive_type(socket, "state_changed", timeout=0.4)
            current, payload = await play(host, guest, code)
            revision = (await snapshot(current, code))["revision"]
            replies = await asyncio.gather(
                *(action(current, code, "play_card", payload, revision=revision) for _ in range(2))
            )
            assert sorted(r.status_code for r in replies) == [200, 409]
            assert (await receive_type(socket, "state_changed"))["revision"] == revision + 1
            # Apply via the SAME transactional service in an intentionally uncommitted transaction.
            async with pg.app.state.factory() as db:
                await db.begin()
                game = await db.scalar(select(Game))
                actor = game.state["state"]["active_player_id"]
                member = await db.get(Member, actor)
                # A valid play is visible only inside this transaction, never to the poller.
                from detective.game_engine import PlayMode, restore_state

                state = restore_state(json.dumps(game.state))
                card_id = state.hand_for(actor)[0]
                await service.execute(
                    db,
                    code,
                    member.session_id,
                    ActionIdentity(client_action_id=uuid4(), expected_revision=game.revision),
                    c.PlayCard(card_id, PlayMode.DISCARD),
                    "play_card",
                    {"card_id": card_id, "mode": "discard", "confirmed": True},
                )
                with pytest.raises(TimeoutError):
                    await receive_type(socket, "state_changed", timeout=0.4)
                await db.rollback()
            with pytest.raises(TimeoutError):
                await receive_type(socket, "state_changed", timeout=0.4)
            assert (await snapshot(host, code))["revision"] == revision + 1


async def test_saved_event_gap_requires_snapshot(pg, live):
    async with peers(pg, live) as (host, guest):
        code = await playing_room(host, guest)
        # Simulate retained history missing an old event; state remains authoritative.
        async with pg.app.state.factory.begin() as db:
            await db.execute(delete(GameEvent).where(GameEvent.revision == 1))
        async with ws(guest, code) as socket:
            await receive_type(socket, "hello")
            await socket.send(json.dumps({"protocol_version": 1, "type": "resume", "revision": 0}))
            event = await receive_type(socket, "sync_required")
            assert event["reason"] == "event_gap" and event["revision"] == 2


async def test_process_restart_and_session_reconnect(pg, live):
    async with peers(pg, live) as (host, guest):
        code = await playing_room(host, guest)
        async with ws(guest, code) as old:
            await receive_type(old, "hello")
            current, payload = await play(host, guest, code)
            assert (await action(current, code, "play_card", payload)).status_code == 200
            before = await snapshot(guest, code)
            await live.stop(kill=True)
            with pytest.raises(ConnectionClosed):
                while True:
                    await old.recv()
        await live.launch()
        async with ws(guest, code) as returned:
            hello = await receive_type(returned, "hello")
            assert hello["revision"] == before["revision"]
            unchanged = await snapshot(guest, code) == before
            assert unchanged


async def test_snapshot_resume_race_is_detected(pg, live):
    async with peers(pg, live) as (host, guest):
        code = await playing_room(host, guest)
        async with ws(guest, code) as socket:
            await receive_type(socket, "hello")
            fetched = await snapshot(guest, code)
            current, payload = await play(host, guest, code)
            assert (await action(current, code, "play_card", payload)).status_code == 200
            # A COMMIT between fetching a snapshot and acknowledging it cannot be lost.
            await socket.send(
                json.dumps(
                    {"type": "resume", "protocol_version": 1, "revision": fetched["revision"]}
                )
            )
            required = await receive_type(socket, "sync_required")
            assert required["revision"] == fetched["revision"] + 1
            fresh = await snapshot(guest, code)
            await socket.send(
                json.dumps({"type": "resume", "protocol_version": 1, "revision": fresh["revision"]})
            )
            assert (await receive_type(socket, "synced"))["revision"] == fresh["revision"]
            current, payload = await play(host, guest, code)
            assert (await action(current, code, "play_card", payload)).status_code == 200
            event = await receive_type(socket, "state_changed")
            assert event["revision"] == fresh["revision"] + 1


async def test_ordered_phases_review_key_and_results(pg, live):
    async with peers(pg, live) as (host, guest):
        code = await playing_room(host, guest)
        async with ws(host, code) as a, ws(guest, code) as b:
            await receive_type(a, "hello")
            await receive_type(b, "hello")
            for _ in range(16):
                current, payload = await play(host, guest, code)
                assert (await action(current, code, "play_card", payload)).status_code == 200
            await action(guest, code, "propose_finish")
            await action(host, code, "confirm_finish")
            for i in range(1, 5):
                await action(guest, code, "answer", {"question_id": f"q{i}", "option_id": "a"})
            await action(host, code, "submit_answers")
            assert "answer_key" in (await snapshot(host, code))["private"]
            assert "correct_option_id" not in all_keys(await snapshot(guest, code))
            for socket in (a, b):
                for revision in range(3, 26):
                    event = await receive_type(socket, "state_changed")
                    assert event["revision"] == revision
                    assert set(event) == {"protocol_version", "type", "phase", "revision"}
                assert event["phase"] == "REVIEW"
            for i in range(1, 5):
                await action(host, code, "review_answer", {"question_id": f"q{i}", "correct": True})
            await action(host, code, "finish_game")
            for socket in (a, b):
                for revision in range(26, 31):
                    event = await receive_type(socket, "state_changed")
                    assert event["revision"] == revision
                assert event["phase"] == "FINISHED"
            for participant in (host, guest):
                assert "solution" in await snapshot(participant, code)
