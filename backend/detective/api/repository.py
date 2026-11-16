"""Small query layer. Callers own the transaction; mutations lock room then game."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import Game, Member, Room
from .security import APIError


async def room_by_code(db: AsyncSession, code: str, *, lock: bool = False) -> Room:
    query = select(Room).where(Room.code == code)
    if lock:
        query = query.with_for_update()
    room = await db.scalar(query)
    if room is None:
        raise APIError("ROOM_NOT_FOUND", 404)
    return room


async def membership(db: AsyncSession, room: Room, session_id: UUID) -> Member:
    member = await db.scalar(
        select(Member).where(Member.room_id == room.id, Member.session_id == session_id)
    )
    if member is None:
        raise APIError("NOT_A_MEMBER", 403)
    return member


async def members(db: AsyncSession, room: Room) -> list[Member]:
    return list(
        await db.scalars(select(Member).where(Member.room_id == room.id).order_by(Member.seat))
    )


async def game_for(db: AsyncSession, room: Room, *, lock: bool = False) -> Game:
    query = select(Game).where(Game.room_id == room.id)
    if lock:
        query = query.with_for_update()
    game = await db.scalar(query)
    if game is None:
        raise APIError("GAME_NOT_FOUND", 404)
    return game
