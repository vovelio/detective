"""Active rooms poll committed PostgreSQL outbox rows; memory is only a delivery cache."""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from detective.api.db import AnonymousSession, BoardObject, Game, GameEvent, Member, Room
from detective.game_engine import Phase, restore_state

from .connection import Connection
from .protocol import CardPreview, Limits, message


@dataclass(frozen=True)
class RoomStatus:
    room_id: UUID
    code: str
    game_id: UUID
    revision: int
    phase: str
    members: tuple[UUID, ...]
    valid_sessions: frozenset[tuple[UUID, UUID]]
    signature: str


async def read_status(db: AsyncSession, room: Room) -> RoomStatus:
    # Do not select games.state: pollers have no need for private game contents.
    row = (
        await db.execute(select(Game.id, Game.revision, Game.phase).where(Game.room_id == room.id))
    ).one()
    players = (
        await db.execute(
            select(
                Member.id,
                Member.session_id,
                Member.nickname,
                Member.seat,
                AnonymousSession.expires_at,
            )
            .join(AnonymousSession, AnonymousSession.id == Member.session_id)
            .where(Member.room_id == room.id)
            .order_by(Member.seat)
        )
    ).all()
    signature = json.dumps(
        {
            "host": str(room.host_member_id),
            "case": room.selected_case_id,
            "catalog_revision": room.catalog_revision,
            "players": [(str(p.id), p.nickname, p.seat) for p in players],
        }
    )
    return RoomStatus(
        room.id,
        room.code,
        row.id,
        row.revision,
        Phase(row.phase).value,
        tuple(p.id for p in players),
        frozenset((p.id, p.session_id) for p in players if p.expires_at > datetime.now(UTC)),
        signature,
    )


@dataclass
class RoomStream:
    status: RoomStatus
    last_revision: int
    connections: set[Connection] = field(default_factory=set)
    presence_sequence: int = 0
    task: asyncio.Task[None] | None = None


class Hub:
    def __init__(self, factory: async_sessionmaker[AsyncSession], limits: Limits):
        self.factory, self.limits = factory, limits
        self.rooms: dict[UUID, RoomStream] = {}
        self.closed = False
        self.retiring: set[asyncio.Task[None]] = set()

    @property
    def connection_count(self) -> int:
        return sum(len(room.connections) for room in self.rooms.values())

    def register(self, connection: Connection, status: RoomStatus) -> bool:
        room = self.rooms.get(status.room_id)
        if (
            self.closed
            or self.connection_count >= self.limits.total_connections
            or (
                room is not None
                and (
                    len(room.connections) >= self.limits.room_connections
                    or sum(c.member_id == connection.member_id for c in room.connections)
                    >= self.limits.member_connections
                )
            )
        ):
            return False
        if room is None:
            room = RoomStream(status, status.revision)
            self.rooms[status.room_id] = room
        connection.offer(
            message(
                "hello",
                member_id=str(connection.member_id),
                revision=status.revision,
                phase=status.phase,
                snapshot_url=f"/api/v1/rooms/{status.code}/snapshot",
                heartbeat_seconds=self.limits.heartbeat_interval,
            )
        )
        room.connections.add(connection)
        self.presence(room)
        if room.task is None or room.task.done():
            room.task = asyncio.create_task(self.poll(room))
        return True

    def presence(self, room: RoomStream) -> None:
        room.presence_sequence += 1
        online = {
            c.member_id for c in room.connections if not c.stopped.is_set() and not c.expired()
        }
        value = message(
            "presence",
            sequence=room.presence_sequence,
            players=[
                {"member_id": str(mid), "online": mid in online} for mid in room.status.members
            ],
        )
        for connection in tuple(room.connections):
            connection.offer(value)

    def update_lobby(self, room: RoomStream, status: RoomStatus) -> None:
        changed = status.signature != room.status.signature
        room.status = status
        if changed:
            for connection in tuple(room.connections):
                connection.offer(
                    message(
                        "room_changed",
                        revision=status.revision,
                        snapshot_url=f"/api/v1/rooms/{status.code}/snapshot",
                    )
                )
            self.presence(room)

    def remove(self, connection: Connection, room_id: UUID) -> None:
        room = self.rooms.get(room_id)
        if room is None or connection not in room.connections:
            return
        room.connections.remove(connection)
        if room.connections:
            self.presence(room)
        else:
            self.rooms.pop(room_id, None)
            if room.task is not None:
                room.task.cancel()
                self.retiring.add(room.task)
                room.task.add_done_callback(self.retiring.discard)

    def notify(self, room: RoomStream, revision: int, type_: str, **fields: Any) -> None:
        for connection in tuple(room.connections):
            if revision > connection.notified_revision:
                if connection.offer(message(type_, revision=revision, **fields)):
                    connection.notified_revision = revision

    async def poll(self, room: RoomStream) -> None:
        try:
            while room.connections and not self.closed:
                await asyncio.wait_for(self.poll_once(room), self.limits.database_timeout)
                await asyncio.sleep(self.limits.poll_interval)
        except asyncio.CancelledError:
            raise
        except (SQLAlchemyError, TimeoutError, ValueError, KeyError):
            # Never expose DB exceptions or emit uncommitted/private data.
            for connection in tuple(room.connections):
                connection.stop(1013)

    async def poll_once(self, room: RoomStream) -> None:
        async with self.factory.begin() as db:
            await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            current = await db.get(Room, room.status.room_id)
            if current is None:
                for connection in tuple(room.connections):
                    connection.stop(4403)
                return
            status = await read_status(db, current)
            rows = (
                await db.execute(
                    select(GameEvent.revision, GameEvent.public_json)
                    .where(
                        GameEvent.game_id == status.game_id,
                        GameEvent.revision > room.last_revision,
                        GameEvent.revision <= status.revision,
                    )
                    .order_by(GameEvent.revision)
                    .limit(self.limits.event_batch + 1)
                )
            ).all()
        # Transaction has ended. Rows came from one committed MVCC snapshot.
        for connection in tuple(room.connections):
            if (connection.member_id, connection.session_id) not in status.valid_sessions:
                connection.stop(4401)
        self.update_lobby(room, status)
        difference = status.revision - room.last_revision
        if difference < 0:
            # Database restore/reset: a fresh handshake establishes a new authoritative baseline.
            for connection in tuple(room.connections):
                connection.stop(1012)
        elif difference:
            contiguous = (
                len(rows) == difference
                and len(rows) <= self.limits.event_batch
                and all(
                    row.revision == room.last_revision + index + 1 for index, row in enumerate(rows)
                )
            )
            if contiguous:
                for row in rows:
                    self.notify(
                        room,
                        row.revision,
                        "state_changed",
                        phase=Phase(row.public_json["phase"]).value,
                    )
            else:
                self.notify(room, status.revision, "sync_required", reason="event_gap_or_backlog")
        room.last_revision = status.revision

    async def resume(self, connection: Connection, status: RoomStatus, revision: int) -> None:
        async with self.factory.begin() as db:
            await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            current = await db.scalar(select(Game.revision).where(Game.id == status.game_id))
            if current is None:
                connection.stop(4403)
                return
            count = await db.scalar(
                select(func.count())
                .select_from(GameEvent)
                .where(
                    GameEvent.game_id == status.game_id,
                    GameEvent.revision > revision,
                    GameEvent.revision <= current,
                )
            )
        target = max(current, connection.notified_revision)
        if revision == target:
            value = message("synced", revision=target)
        else:
            reason = (
                "client_ahead"
                if revision > target
                else ("missed_events" if count == current - revision else "event_gap")
            )
            value = message("sync_required", revision=target, reason=reason)
        connection.notified_revision = target
        connection.offer(value)

    def cursor(self, sender: Connection, room_id: UUID, x: float, y: float) -> None:
        room = self.rooms.get(room_id)
        if room is not None:
            for connection in tuple(room.connections):
                if connection is not sender:
                    connection.offer(message("cursor", member_id=str(sender.member_id), x=x, y=y))

    async def card_preview(
        self, sender: Connection, status: RoomStatus, incoming: CardPreview
    ) -> None:
        async with self.factory.begin() as db:
            await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            session = await db.get(AnonymousSession, sender.session_id)
            member = await db.get(Member, sender.member_id)
            game = await db.get(Game, status.game_id)
            if (
                session is None
                or session.expires_at <= datetime.now(UTC)
                or member is None
                or member.session_id != sender.session_id
                or member.room_id != status.room_id
            ):
                sender.stop(4401)
                return
            if game is None or game.state is None:
                sender.offer(message("error", code="BOARD_TARGET_INVALID"))
                return
            state = restore_state(json.dumps(game.state))
            if state.phase not in (Phase.INTRO, Phase.PLAYING, Phase.DISCUSSION):
                sender.offer(message("error", code="BOARD_PHASE_INVALID"))
                return
            if incoming.card_id not in {card.card_id for card in state.published}:
                sender.offer(message("error", code="BOARD_TARGET_INVALID"))
                return
            row = await db.get(BoardObject, (game.id, "card:" + incoming.card_id))
            if incoming.version != (row.version if row else 0):
                sender.offer(message("error", code="BOARD_VERSION_CONFLICT"))
                return
        room = self.rooms.get(status.room_id)
        if room is not None:
            value = message(
                "card_preview",
                member_id=str(sender.member_id),
                card_id=incoming.card_id,
                x=incoming.x,
                y=incoming.y,
                rotation=incoming.rotation % 360,
                version=incoming.version,
            )
            for connection in tuple(room.connections):
                if connection is not sender:
                    connection.offer(value)

    async def shutdown(self) -> None:
        self.closed = True
        tasks = set(self.retiring)
        for room in self.rooms.values():
            if room.task is not None:
                room.task.cancel()
                tasks.add(room.task)
            for connection in room.connections:
                connection.stop(1001)
        await asyncio.gather(*tasks, return_exceptions=True)
