"""Durable public annotations; no hands, scenario catalogue or HTTP dependencies."""

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, StrictInt, TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from detective.game_engine import GameState, Phase

from .contracts import Input
from .db import BoardObject
from .security import APIError

MAX_ACTIVE_OBJECTS = 512
MAX_TOTAL_OBJECTS = 1024
MAX_POINTS = 65536


class Point(Input):
    x: float = Field(ge=-1_000_000, le=1_000_000, allow_inf_nan=False, strict=True)
    y: float = Field(ge=-1_000_000, le=1_000_000, allow_inf_nan=False, strict=True)


class CardTransform(Point):
    op: Literal["card_transform"]
    card_id: str = Field(min_length=2, max_length=100)
    rotation: float = Field(allow_inf_nan=False, strict=True)
    expected_version: StrictInt = Field(ge=0)


class StrokeCreate(Input):
    op: Literal["stroke_create"]
    object_id: UUID
    points: list[Point] = Field(min_length=2, max_length=256)


class ArrowCreate(Input):
    op: Literal["arrow_create"]
    object_id: UUID
    from_point: Point = Field(alias="from")
    to_point: Point = Field(alias="to")


class ArrowUpdate(ArrowCreate):
    op: Literal["arrow_update"]  # type: ignore[assignment]
    expected_version: StrictInt = Field(ge=1)


class NoteCreate(Point):
    op: Literal["note_create"]
    object_id: UUID
    text: str = Field(min_length=1, max_length=1000)


class NoteUpdate(Point):
    op: Literal["note_update"]
    object_id: UUID
    expected_version: StrictInt = Field(ge=1)
    text: str = Field(min_length=1, max_length=1000)


class ObjectDelete(Input):
    op: Literal["object_delete"]
    object_id: UUID
    expected_version: StrictInt = Field(ge=1)


type Mutation = (
    CardTransform
    | StrokeCreate
    | ArrowCreate
    | ArrowUpdate
    | NoteCreate
    | NoteUpdate
    | ObjectDelete
)
ADAPTER: TypeAdapter[Mutation] = TypeAdapter(Mutation)


@dataclass(frozen=True)
class BoardMutation:
    value: Mutation


def decode(payload: dict[str, Any]) -> BoardMutation:
    result = ADAPTER.validate_python(payload)
    if isinstance(result, NoteCreate | NoteUpdate) and any(
        ord(char) < 32 and char not in "\n\t" for char in result.text
    ):
        raise ValueError("Invalid note")
    return BoardMutation(result)


async def rows_for(db: AsyncSession, game_id: UUID) -> list[BoardObject]:
    return list(
        await db.scalars(
            select(BoardObject).where(BoardObject.game_id == game_id).order_by(BoardObject.id)
        )
    )


def card_payload(card_id: str, x: float, y: float, rotation: float = 0) -> dict[str, Any]:
    return {"card_id": card_id, "x": x, "y": y, "rotation": rotation}


async def project(db: AsyncSession, game_id: UUID, state: GameState, view: dict[str, Any]) -> None:
    rows = {row.id: row for row in await rows_for(db, game_id) if row.deleted_at is None}
    objects: list[dict[str, Any]] = []
    for published, item in zip(state.published, view["published"], strict=True):
        id_ = "card:" + published.card_id
        row = rows.pop(id_, None)
        payload = (
            card_payload(published.card_id, published.position.x, published.position.y)
            if row is None
            else card_payload(
                published.card_id, row.payload["x"], row.payload["y"], row.payload["rotation"]
            )
        )
        objects.append(
            {
                "id": id_,
                "kind": "card_position",
                "version": row.version if row else 0,
                "payload": payload,
            }
        )
        item["position"] = {"x": payload["x"], "y": payload["y"]}
    for row in rows.values():
        if row.kind == "stroke":
            payload = {"points": [{"x": p["x"], "y": p["y"]} for p in row.payload["points"]]}
        elif row.kind == "arrow":
            payload = {
                key: {axis: row.payload[key][axis] for axis in ("x", "y")} for key in ("from", "to")
            }
        elif row.kind == "note":
            payload = {key: row.payload[key] for key in ("x", "y", "text")}
        else:
            continue
        objects.append({"id": row.id, "kind": row.kind, "version": row.version, "payload": payload})
    view["board"] = {"objects": objects}


async def mutate(db: AsyncSession, game_id: UUID, state: GameState, command: BoardMutation) -> None:
    revision = state.revision
    if state.phase not in (Phase.INTRO, Phase.PLAYING, Phase.DISCUSSION):
        raise APIError("BOARD_PHASE_INVALID", 409, revision)
    mutation = command.value
    rows = await rows_for(db, game_id)
    by_id = {row.id: row for row in rows}
    published = {card.card_id for card in state.published}
    if isinstance(mutation, CardTransform):
        if mutation.card_id not in published:
            raise APIError("BOARD_TARGET_INVALID", 409, revision)
        id_ = "card:" + mutation.card_id
        row = by_id.get(id_)
        if mutation.expected_version != (row.version if row else 0):
            raise APIError("BOARD_VERSION_CONFLICT", 409, revision)
        payload = card_payload(mutation.card_id, mutation.x, mutation.y, mutation.rotation % 360)
        if not math.isfinite(payload["rotation"]):
            raise APIError("BOARD_TARGET_INVALID", 409, revision)
        kind = "card_position"
    elif isinstance(mutation, NoteUpdate | ArrowUpdate | ObjectDelete):
        row = by_id.get(str(mutation.object_id))
        if row is None or row.deleted_at is not None or row.kind == "card_position":
            raise APIError("BOARD_TARGET_INVALID", 409, revision)
        if mutation.expected_version != row.version:
            raise APIError("BOARD_VERSION_CONFLICT", 409, revision)
        if isinstance(mutation, ObjectDelete):
            row.deleted_at, row.version = datetime.now(UTC), row.version + 1
            return
        if isinstance(mutation, ArrowUpdate):
            if row.kind != "arrow":
                raise APIError("BOARD_TARGET_INVALID", 409, revision)
            row.payload = {
                "from": mutation.from_point.model_dump(),
                "to": mutation.to_point.model_dump(),
            }
            row.version += 1
            return
        if row.kind != "note":
            raise APIError("BOARD_TARGET_INVALID", 409, revision)
        row.payload = {"x": mutation.x, "y": mutation.y, "text": mutation.text}
        row.version += 1
        return
    else:
        id_ = str(mutation.object_id)
        if id_ in by_id:
            raise APIError("BOARD_VERSION_CONFLICT", 409, revision)
        row = None
        if isinstance(mutation, StrokeCreate):
            kind, payload = "stroke", {"points": [point.model_dump() for point in mutation.points]}
            count = sum(
                len(item.payload["points"])
                for item in rows
                if item.kind == "stroke" and item.deleted_at is None
            )
            if count + len(mutation.points) > MAX_POINTS:
                raise APIError("BOARD_LIMIT", 409, revision)
        elif isinstance(mutation, ArrowCreate):
            kind, payload = (
                "arrow",
                {"from": mutation.from_point.model_dump(), "to": mutation.to_point.model_dump()},
            )
        else:
            kind, payload = "note", {"x": mutation.x, "y": mutation.y, "text": mutation.text}
    if row is None:
        if (
            len(rows) >= MAX_TOTAL_OBJECTS
            or sum(item.deleted_at is None for item in rows) >= MAX_ACTIVE_OBJECTS
        ):
            raise APIError("BOARD_LIMIT", 409, revision)
        db.add(BoardObject(id=id_, game_id=game_id, kind=kind, payload=payload, version=1))
    else:
        row.payload, row.version = payload, row.version + 1
