"""Real FastAPI/PostgreSQL with controlled ASGI transport backpressure and short timers."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from detective.api import service
from detective.api.app import create_app
from detective.api.contracts import ActionIdentity
from detective.api.db import AnonymousSession, Member
from detective.game_engine import commands as c
from detective.realtime.protocol import Limits
from sqlalchemy import func, select

from .test_api import client, playing_room, room, snapshot

pytestmark = pytest.mark.asyncio


class Socket:
    def __init__(self, app, session, code, *, slow=False):
        self.app, self.slow = app, slow
        self.input, self.output = asyncio.Queue(), asyncio.Queue()
        self.gate = asyncio.Event()
        cookie = "; ".join(f"{c.name}={c.value}" for c in session.cookies.jar)
        self.scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "scheme": "wss",
            "path": f"/ws/v1/rooms/{code}",
            "query_string": b"",
            "root_path": "",
            "subprotocols": ["detective.v1"],
            "headers": [
                (b"host", b"test.local"),
                (b"origin", b"https://test.local"),
                (b"cookie", cookie.encode()),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("test.local", 443),
        }

    async def send(self, value):
        if self.slow and value["type"] == "websocket.send":
            await self.gate.wait()
        await self.output.put(value)

    async def __aenter__(self):
        self.task = asyncio.create_task(self.app(self.scope, self.input.get, self.send))
        await self.input.put({"type": "websocket.connect"})
        assert (await asyncio.wait_for(self.output.get(), 3))["type"] == "websocket.accept"
        return self

    async def __aexit__(self, *args):
        await self.input.put({"type": "websocket.disconnect", "code": 1000})
        try:
            await asyncio.wait_for(self.task, 3)
        finally:
            if not self.task.done():
                self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def receive(self, kind):
        async def next_frame():
            while True:
                frame = await self.output.get()
                if frame["type"] == "websocket.close":
                    assert kind == "close"
                    return frame
                if frame["type"] == "websocket.send":
                    value = json.loads(frame["text"])
                    if value["type"] == kind:
                        return value

        return await asyncio.wait_for(next_frame(), 3)

    async def message(self, value):
        await self.input.put({"type": "websocket.receive", "text": json.dumps(value)})


@asynccontextmanager
async def configured(pg, **overrides):
    app = create_app(pg.settings, realtime_limits=replace(Limits(), **overrides))
    try:
        yield app
    finally:
        await app.state.realtime.shutdown()
        await app.state.engine.dispose()


async def test_slow_sender_is_closed_and_resources_released(pg):
    async with client(pg) as host, client(pg) as guest:
        code = await playing_room(host, guest)
        before = await snapshot(host, code)
        async with configured(pg, send_timeout=0.05) as app:
            async with Socket(app, host, code, slow=True) as socket:
                assert (await socket.receive("close"))["code"] == 1013
            assert app.state.realtime.connection_count == 0
            assert not app.state.realtime.rooms
            await asyncio.sleep(0)
            assert not app.state.realtime.retiring
        unchanged = await snapshot(host, code) == before
        assert unchanged


async def test_heartbeat_timeout_preserves_member_and_host(pg):
    async with client(pg) as host:
        code = await room(host)
        before = await snapshot(host, code)
        async with configured(pg, heartbeat_interval=0.04, heartbeat_timeout=0.12) as app:
            async with Socket(app, host, code) as socket:
                await socket.receive("hello")
                await socket.receive("ping")
                assert (await socket.receive("close"))["code"] == 4408
            assert app.state.realtime.connection_count == 0
        unchanged = await snapshot(host, code) == before
        assert unchanged


async def test_application_pong_renews_liveness(pg):
    async with client(pg) as host:
        code = await room(host)
        async with configured(pg, heartbeat_interval=0.03, heartbeat_timeout=0.12) as app:
            async with Socket(app, host, code) as socket:
                await socket.receive("hello")
                for _ in range(8):
                    await socket.receive("ping")
                    await socket.message({"protocol_version": 1, "type": "pong"})
                await socket.message({"protocol_version": 1, "type": "ping"})
                await socket.receive("pong")
                assert app.state.realtime.connection_count == 1


@pytest.mark.parametrize(
    "kind,expected", [("size", 1009), ("binary", 1003), ("version", 1008), ("rate", 1008)]
)
async def test_incoming_limits(pg, kind, expected):
    async with client(pg) as host:
        code = await room(host)
        async with configured(pg) as app:
            async with Socket(app, host, code) as socket:
                await socket.receive("hello")
                if kind == "size":
                    await socket.input.put({"type": "websocket.receive", "text": "x" * 4097})
                elif kind == "binary":
                    await socket.input.put({"type": "websocket.receive", "bytes": b"binary"})
                elif kind == "version":
                    await socket.message({"type": "pong", "protocol_version": 99})
                else:
                    for _ in range(21):
                        await socket.message({"type": "pong", "protocol_version": 1})
                assert (await socket.receive("close"))["code"] == expected
            assert app.state.realtime.connection_count == 0


async def test_connection_cap_and_last_tab_presence(pg):
    async with client(pg) as host:
        code = await room(host)
        async with configured(pg, member_connections=1) as app:
            async with Socket(app, host, code) as first:
                await first.receive("hello")
                async with Socket(app, host, code) as second:
                    assert (await second.receive("close"))["code"] == 1013
                assert app.state.realtime.connection_count == 1
                await first.message({"protocol_version": 1, "type": "ping"})
                await first.receive("pong")
            assert app.state.realtime.connection_count == 0


async def test_bounded_outgoing_queue(pg):
    async with client(pg) as host:
        code = await room(host)
        async with configured(pg, queue_messages=2, send_timeout=0.1) as app:
            async with Socket(app, host, code, slow=True) as socket:
                # Writer holds hello in-flight. Initial presence and replies fill a bounded queue.
                for _ in range(4):
                    await socket.message({"protocol_version": 1, "type": "ping"})
                assert (await socket.receive("close"))["code"] == 1013
            assert app.state.realtime.connection_count == 0
        async with configured(pg, queue_bytes=1) as app:
            async with Socket(app, host, code) as socket:
                assert (await socket.receive("close"))["code"] == 1013
            assert not app.state.realtime.rooms


async def test_expired_session_closed_without_deleting_membership(pg):
    async with client(pg) as host:
        code = await room(host)
        async with configured(pg, poll_interval=0.02) as app:
            async with Socket(app, host, code) as socket:
                await socket.receive("hello")
                async with pg.app.state.factory.begin() as db:
                    session = await db.scalar(select(AnonymousSession))
                    session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                assert (await socket.receive("close"))["code"] == 4401
            async with pg.app.state.factory() as db:
                assert await db.scalar(select(func.count()).select_from(Member)) == 1


async def test_polling_gap_falls_back_to_snapshot(pg):
    async with client(pg) as host, client(pg) as guest:
        code = await room(host, guest)
        async with configured(pg, event_batch=1, poll_interval=0.02) as app:
            async with Socket(app, host, code) as socket:
                await socket.receive("hello")
                async with pg.app.state.factory.begin() as db:
                    host_member = await db.get(Member, (await snapshot(host, code))["host_id"])
                    from uuid import uuid4

                    for revision, command in [(0, c.StartGame()), (1, c.BeginPlay())]:
                        await service.execute(
                            db,
                            code,
                            host_member.session_id,
                            ActionIdentity(expected_revision=revision, client_action_id=uuid4()),
                            command,
                            "start_game" if revision == 0 else "begin_play",
                            {},
                        )
                required = await socket.receive("sync_required")
                assert required["reason"] == "event_gap_or_backlog" and required["revision"] == 2
                assert (await snapshot(host, code))["phase"] == "PLAYING"


@pytest.mark.parametrize("which", ["host", "origin", "no_origin", "query"])
async def test_guard_rejects_before_accept(pg, which):
    async with client(pg) as host:
        code = await room(host)
        async with configured(pg) as app:
            socket = Socket(app, host, code)
            if which == "query":
                socket.scope["query_string"] = b"token=DISPOSABLE_TEST"
            elif which == "no_origin":
                socket.scope["headers"] = [
                    pair for pair in socket.scope["headers"] if pair[0] != b"origin"
                ]
            else:
                key = which.encode()
                socket.scope["headers"] = [
                    (k, b"https://wrong.invalid" if k == b"origin" else b"wrong.invalid")
                    if k == key
                    else (k, v)
                    for k, v in socket.scope["headers"]
                ]
            task = asyncio.create_task(app(socket.scope, socket.input.get, socket.send))
            await socket.input.put({"type": "websocket.connect"})
            frame = await asyncio.wait_for(socket.output.get(), 2)
            assert frame == {"type": "websocket.close", "code": 1008}
            await asyncio.wait_for(task, 2)
            assert app.state.realtime.connection_count == 0
