"""Versioned SERVER-ONLY saves. Never use these functions for HTTP/WS responses."""

import json
from dataclasses import asdict, fields
from typing import Any

from detective.scenarios.validation import ValidationError

from .invariants import check_invariants, require
from .models import (
    Answer,
    GameError,
    GameState,
    Hand,
    Phase,
    Player,
    Position,
    PublishedCard,
    Result,
    Scenario,
)


def dump_state(state: GameState) -> str:
    check_invariants(state)
    return json.dumps(
        {"state_version": 1, "state": asdict(state)}, ensure_ascii=False, allow_nan=False
    )


def _record(value: Any, keys: set[str]) -> dict[str, Any]:
    require(type(value) is dict and set(value) == keys, "INVALID_SAVED_STATE")
    result: dict[str, Any] = value
    return result


def _array(value: Any) -> list[Any]:
    require(type(value) is list, "INVALID_SAVED_STATE")
    result: list[Any] = value
    return result


def _string(value: Any) -> str:
    require(type(value) is str, "INVALID_SAVED_STATE")
    result: str = value
    return result


def _optional_string(value: Any) -> str | None:
    return None if value is None else _string(value)


def _integer(value: Any) -> int:
    require(type(value) is int, "INVALID_SAVED_STATE")
    result: int = value
    return result


def _strings(value: Any) -> tuple[str, ...]:
    return tuple(_string(v) for v in _array(value))


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, "INVALID_SAVED_STATE")
        result[key] = value
    return result


def restore_state(data: str) -> GameState:
    """Checks persisted shape and invariants, without executing commands or shuffling."""
    try:
        envelope = _record(json.loads(data, object_pairs_hook=_pairs), {"state_version", "state"})
        require(_integer(envelope["state_version"]) == 1, "UNSUPPORTED_STATE_VERSION")
        raw = _record(envelope["state"], {f.name for f in fields(GameState)})
        scenario = _record(raw["scenario"], {"manifest_json"})
        players = []
        for item in _array(raw["players"]):
            p = _record(item, {"id", "nickname"})
            players.append(Player(_string(p["id"]), _string(p["nickname"])))
        hands = []
        for item in _array(raw["hands"]):
            h = _record(item, {"player_id", "card_ids"})
            hands.append(Hand(_string(h["player_id"]), _strings(h["card_ids"])))
        published = []
        for item in _array(raw["published"]):
            card = _record(item, {"card_id", "position"})
            pos = _record(card["position"], {"x", "y"})
            require(all(type(v) in (int, float) for v in pos.values()), "INVALID_SAVED_STATE")
            published.append(PublishedCard(_string(card["card_id"]), Position(pos["x"], pos["y"])))
        answers = []
        for item in _array(raw["answers"]):
            a = _record(item, {"question_id", "option_id", "reviewed_correct"})
            require(
                a["reviewed_correct"] is None or type(a["reviewed_correct"]) is bool,
                "INVALID_SAVED_STATE",
            )
            answers.append(
                Answer(_string(a["question_id"]), _string(a["option_id"]), a["reviewed_correct"])
            )
        result = None
        if raw["result"] is not None:
            r = _record(raw["result"], {f.name for f in fields(Result)})
            result = Result(**{key: _integer(value) for key, value in r.items()})
        state = GameState(
            game_id=_string(raw["game_id"]),
            scenario=Scenario(_string(scenario["manifest_json"])),
            players=tuple(players),
            host_id=_string(raw["host_id"]),
            phase=Phase(_string(raw["phase"])),
            revision=_integer(raw["revision"]),
            hands=tuple(hands),
            deck=_strings(raw["deck"]),
            published=tuple(published),
            discarded=_strings(raw["discarded"]),
            turn_order=_strings(raw["turn_order"]),
            active_player_id=_optional_string(raw["active_player_id"]),
            finish_proposed_by=_optional_string(raw["finish_proposed_by"]),
            answers=tuple(answers),
            result=result,
        )
        check_invariants(state)
        return state
    except (ValueError, TypeError, KeyError, RecursionError, ValidationError):
        raise GameError("INVALID_SAVED_STATE") from None
