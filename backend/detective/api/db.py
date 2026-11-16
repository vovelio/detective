"""PostgreSQL persistence. ORM objects are never response models."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import Settings


class Base(DeclarativeBase):
    pass


class AnonymousSession(Base):
    __tablename__ = "anonymous_sessions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ScenarioRow(Base):
    __tablename__ = "scenarios"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    version: Mapped[str] = mapped_column(String(20))
    scope_room_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("rooms.id", name="fk_scenario_scope_room", use_alter=True),
        nullable=True,
        index=True,
    )
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    checksum: Mapped[str] = mapped_column(String(64))
    package_name: Mapped[str] = mapped_column(String(80))


class Room(Base):
    __tablename__ = "rooms"
    __table_args__ = (
        ForeignKeyConstraint(
            ["host_member_id", "id"],
            ["members.id", "members.room_id"],
            name="fk_room_host",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(24), unique=True)
    host_member_id: Mapped[UUID] = mapped_column(nullable=False)
    selected_case_id: Mapped[str | None] = mapped_column(ForeignKey("scenarios.id"))
    catalog_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Member(Base):
    __tablename__ = "members"
    __table_args__ = (
        UniqueConstraint("room_id", "session_id", name="uq_member_session"),
        UniqueConstraint("room_id", "nickname_key", name="uq_member_nickname"),
        UniqueConstraint("room_id", "seat", name="uq_member_seat"),
        UniqueConstraint("id", "room_id", name="uq_member_room"),
        CheckConstraint("seat >= 0 AND seat < 8", name="ck_member_seat"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    room_id: Mapped[UUID] = mapped_column(ForeignKey("rooms.id"), index=True)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("anonymous_sessions.id"), index=True)
    nickname: Mapped[str] = mapped_column(String(20))
    nickname_key: Mapped[str] = mapped_column(String(100))
    seat: Mapped[int] = mapped_column(Integer)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Game(Base):
    __tablename__ = "games"
    __table_args__ = (CheckConstraint("revision >= 0", name="ck_game_revision"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    room_id: Mapped[UUID] = mapped_column(ForeignKey("rooms.id"), unique=True)
    scenario_id: Mapped[str | None] = mapped_column(ForeignKey("scenarios.id"))
    phase: Mapped[str] = mapped_column(String(20), default="LOBBY")
    revision: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class BoardObject(Base):
    __tablename__ = "board_objects"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_board_version"),
        CheckConstraint(
            "kind IN ('card_position', 'stroke', 'arrow', 'note')", name="ck_board_kind"
        ),
    )
    game_id: Mapped[UUID] = mapped_column(ForeignKey("games.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String(110), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GameAction(Base):
    __tablename__ = "game_actions"
    __table_args__ = (
        UniqueConstraint("game_id", "session_id", "client_action_id", name="uq_game_action"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    game_id: Mapped[UUID] = mapped_column(ForeignKey("games.id"), index=True)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("anonymous_sessions.id"))
    client_action_id: Mapped[UUID] = mapped_column()
    request_hash: Mapped[str] = mapped_column(String(64))
    game_revision: Mapped[int] = mapped_column(Integer)
    command_type: Mapped[str] = mapped_column(String(32))
    result_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GameEvent(Base):
    __tablename__ = "game_events"
    __table_args__ = (UniqueConstraint("game_id", "revision", name="uq_game_event"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    game_id: Mapped[UUID] = mapped_column(ForeignKey("games.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(32))
    public_json: Mapped[dict[str, Any]] = mapped_column(JSONB)


def make_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        echo=False,
        hide_parameters=True,
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": settings.database_schema}},
    )


def session_factory(engine: AsyncEngine) -> async_sessionmaker["AsyncSession"]:
    return async_sessionmaker(engine, expire_on_commit=False)
