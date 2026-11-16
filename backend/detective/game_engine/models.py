"""Immutable domain state. All identifiers are server-resolved membership identifiers."""

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from detective.scenarios.validation import load_case, validate_case


class Phase(StrEnum):
    LOBBY = "LOBBY"
    INTRO = "INTRO"
    PLAYING = "PLAYING"
    DISCUSSION = "DISCUSSION"
    ANSWERING = "ANSWERING"
    REVIEW = "REVIEW"
    RESULTS = "RESULTS"
    FINISHED = "FINISHED"


class PlayMode(StrEnum):
    REVEAL = "reveal"
    DISCARD = "discard"


class GameError(ValueError):
    """Stable, non-secret error code; no card contents in errors."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class Scenario:
    # An immutable copy of the existing manifest contract, not a second scenario schema.
    manifest_json: str = field(repr=False)

    def __post_init__(self) -> None:
        load_case(self.manifest_json.encode("utf-8"))

    @classmethod
    def from_manifest(cls, manifest: dict[str, Any]) -> "Scenario":
        validate_case(manifest)
        return cls(json.dumps(manifest, ensure_ascii=False, allow_nan=False))

    def manifest(self) -> dict[str, Any]:
        manifest: dict[str, Any] = json.loads(self.manifest_json)
        return manifest


@dataclass(frozen=True)
class Player:
    id: str
    nickname: str


@dataclass(frozen=True)
class Position:
    x: float
    y: float


@dataclass(frozen=True)
class PublishedCard:
    card_id: str
    position: Position


@dataclass(frozen=True, repr=False)
class Hand:
    player_id: str
    card_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Answer:
    question_id: str
    option_id: str
    reviewed_correct: bool | None = None


@dataclass(frozen=True)
class Result:
    correct_answers: int
    irrelevant_revealed: int
    discarded: int
    missing_discards: int
    answer_points: int
    irrelevant_penalty: int
    missing_discard_penalty: int
    score: int


@dataclass(frozen=True, repr=False)
class GameState:
    game_id: str
    scenario: Scenario
    players: tuple[Player, ...]
    host_id: str
    phase: Phase = Phase.LOBBY
    revision: int = 0
    hands: tuple[Hand, ...] = ()
    deck: tuple[str, ...] = ()
    published: tuple[PublishedCard, ...] = ()
    discarded: tuple[str, ...] = ()
    turn_order: tuple[str, ...] = ()
    active_player_id: str | None = None
    finish_proposed_by: str | None = None
    answers: tuple[Answer, ...] = ()
    result: Result | None = None

    def hand_for(self, player_id: str) -> tuple[str, ...]:
        for hand in self.hands:
            if hand.player_id == player_id:
                return hand.card_ids
        raise GameError("NOT_A_MEMBER")


@dataclass(frozen=True)
class StateChanged:
    """Safe invalidation event: no card IDs, drawn cards or key material."""

    revision: int
    phase: Phase


@dataclass(frozen=True, repr=False)
class Transition:
    state: GameState
    events: tuple[StateChanged, ...]
