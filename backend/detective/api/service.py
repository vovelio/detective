"""Transactions are owned by the HTTP/CLI caller, never committed mid-operation."""

import hashlib
import json
import secrets
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from detective.game_engine import (
    GameError,
    Phase,
    Player,
    apply,
    create_game,
    dump_state,
    private_state_for,
    restore_state,
)
from detective.game_engine import commands as c
from detective.game_engine.models import StateChanged, Transition

from . import board
from . import repository as repo
from .contracts import ActionIdentity
from .db import Game, GameAction, GameEvent, Member, Room, ScenarioRow
from .security import APIError


def host_only(room: Room, member: Member) -> None:
    if member.id != room.host_member_id:
        raise APIError("HOST_ONLY", 403)


async def create_room(db: AsyncSession, session_id: UUID, nickname: str) -> dict[str, Any]:
    room_id, member_id = uuid4(), uuid4()
    room = Room(id=room_id, code=secrets.token_urlsafe(9), host_member_id=member_id)
    db.add(room)
    await db.flush()
    db.add(
        Member(
            id=member_id,
            room_id=room_id,
            session_id=session_id,
            nickname=nickname,
            nickname_key=nickname.casefold(),
            seat=0,
        )
    )
    db.add(Game(room_id=room_id))
    await db.flush()
    return await lobby_view(db, room, session_id)


async def join_room(db: AsyncSession, code: str, session_id: UUID, nickname: str) -> dict[str, Any]:
    room = await repo.room_by_code(db, code, lock=True)
    existing = await db.scalar(
        select(Member).where(Member.room_id == room.id, Member.session_id == session_id)
    )
    if existing is not None:
        return await lobby_view(db, room, session_id)
    game = await repo.game_for(db, room)
    if game.state is not None:
        raise APIError("GAME_ALREADY_STARTED", 409)
    players = await repo.members(db, room)
    if len(players) >= 8:
        raise APIError("ROOM_FULL", 409)
    if nickname.casefold() in {p.nickname_key for p in players}:
        raise APIError("NICKNAME_TAKEN", 409)
    db.add(
        Member(
            room_id=room.id,
            session_id=session_id,
            nickname=nickname,
            nickname_key=nickname.casefold(),
            seat=len(players),
        )
    )
    await db.flush()
    return await lobby_view(db, room, session_id)


async def lobby_view(db: AsyncSession, room: Room, session_id: UUID) -> dict[str, Any]:
    member = await repo.membership(db, room, session_id)
    game = await repo.game_for(db, room)
    return {
        "code": room.code,
        "member_id": str(member.id),
        "host_id": str(room.host_member_id),
        "selected_case_id": room.selected_case_id,
        "phase": game.phase,
        "revision": game.revision,
        "players": [
            {"id": str(p.id), "nickname": p.nickname, "seat": p.seat}
            for p in await repo.members(db, room)
        ],
    }


async def select_case(
    db: AsyncSession, code: str, session_id: UUID, case_id: str
) -> dict[str, Any]:
    room = await repo.room_by_code(db, code, lock=True)
    host_only(room, await repo.membership(db, room, session_id))
    if (await repo.game_for(db, room)).state is not None:
        raise APIError("GAME_ALREADY_STARTED", 409)
    scenario = await db.get(ScenarioRow, case_id)
    if scenario is None or scenario.scope_room_id not in (None, room.id):
        raise APIError("CASE_NOT_FOUND", 404)
    room.selected_case_id = case_id
    await db.flush()
    return await lobby_view(db, room, session_id)


async def snapshot(
    db: AsyncSession, code: str, session_id: UUID, *, result_only: bool = False
) -> dict[str, Any]:
    room = await repo.room_by_code(db, code)
    member = await repo.membership(db, room, session_id)
    # Keep board rows and the game projection at one committed revision.
    game = await repo.game_for(db, room, lock=True)
    if game.state is None:
        if result_only:
            raise APIError("RESULT_NOT_AVAILABLE", 409, game.revision)
        return await lobby_view(db, room, session_id)
    state = restore_state(json.dumps(game.state))
    if result_only and state.phase not in (Phase.RESULTS, Phase.FINISHED):
        raise APIError("RESULT_NOT_AVAILABLE", 409, state.revision)
    view = private_state_for(state, str(member.id))
    await board.project(db, game.id, state, view)
    return view


async def execute(
    db: AsyncSession,
    code: str,
    session_id: UUID,
    identity: ActionIdentity,
    command: c.Command | board.BoardMutation,
    command_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    # Consistent order for start vs join/selection and concurrent actions.
    room = await repo.room_by_code(db, code, lock=True)
    member = await repo.membership(db, room, session_id)
    game = await repo.game_for(db, room, lock=True)
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "type": command_type,
                "payload": payload,
                "expected_revision": identity.expected_revision,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    previous = await db.scalar(
        select(GameAction).where(
            GameAction.game_id == game.id,
            GameAction.session_id == session_id,
            GameAction.client_action_id == identity.client_action_id,
        )
    )
    # Retry is checked BEFORE expected_revision: a committed response may have been lost.
    if previous is not None:
        if previous.request_hash != fingerprint:
            raise APIError("ACTION_ID_REUSED", 409, game.revision)
        return previous.result_json
    if identity.expected_revision > game.revision or (
        not isinstance(command, board.BoardMutation) and identity.expected_revision != game.revision
    ):
        raise APIError("REVISION_CONFLICT", 409, game.revision)
    if game.state is None:
        if not isinstance(command, c.StartGame):
            raise APIError("GAME_NOT_STARTED", 409, game.revision)
        host_only(room, member)
        scenario = (
            await db.get(ScenarioRow, room.selected_case_id) if room.selected_case_id else None
        )
        if scenario is None:
            raise APIError("CASE_REQUIRED", 409, game.revision)
        players = tuple(Player(str(p.id), p.nickname) for p in await repo.members(db, room))
        try:
            state = create_game(str(game.id), scenario.manifest, players, str(room.host_member_id))
        except GameError as error:
            raise APIError(error.code, 409, game.revision) from None
        game.scenario_id = scenario.id
    else:
        state = restore_state(json.dumps(game.state))
    try:
        if isinstance(command, board.BoardMutation):
            await board.mutate(db, game.id, state, command)
            new = replace(state, revision=state.revision + 1)
            transition = Transition(new, (StateChanged(new.revision, new.phase),))
        else:
            transition = apply(state, str(member.id), command)
    except GameError as error:
        status = 403 if error.code in ("HOST_ONLY", "NOT_A_MEMBER") else 409
        raise APIError(error.code, status, game.revision) from None
    new = transition.state
    game.state, game.revision, game.phase = (
        json.loads(dump_state(new)),
        new.revision,
        new.phase.value,
    )
    receipt = {
        "client_action_id": str(identity.client_action_id),
        "revision": new.revision,
        "phase": new.phase.value,
    }
    db.add(
        GameAction(
            game_id=game.id,
            session_id=session_id,
            client_action_id=identity.client_action_id,
            request_hash=fingerprint,
            game_revision=new.revision,
            command_type=command_type,
            result_json=receipt,
        )
    )
    for event in transition.events:
        db.add(
            GameEvent(
                game_id=game.id,
                revision=event.revision,
                event_type="state_changed",
                public_json={"revision": event.revision, "phase": event.phase.value},
            )
        )
    # Force constraint errors before returning; caller commits state and receipt together.
    await db.flush()
    return receipt
