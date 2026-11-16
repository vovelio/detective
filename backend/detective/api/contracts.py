"""Strict HTTP inputs; unknown fields (including actor_id) are rejected."""

import unicodedata
from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

from detective.game_engine import PlayMode, Position
from detective.game_engine import commands as c


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Nickname(Input):
    nickname: str = Field(max_length=100)

    @field_validator("nickname")
    @classmethod
    def valid_nickname(cls, value: str) -> str:
        value = unicodedata.normalize("NFC", value.strip())
        if not 2 <= len(value) <= 20 or any(unicodedata.category(c).startswith("C") for c in value):
            raise ValueError("Invalid nickname")
        return value


class SelectCase(Input):
    case_id: str = Field(min_length=2, max_length=100, pattern=r"^[a-z][a-z0-9-]*$")


class ActionIdentity(Input):
    client_action_id: UUID
    expected_revision: StrictInt = Field(ge=0, le=2**31 - 1)


class ActionRequest(ActionIdentity):
    type: Literal[
        "board_mutation",
        "begin_play",
        "play_card",
        "propose_finish",
        "confirm_finish",
        "answer",
        "submit_answers",
        "review_answer",
        "finish_game",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)


class PlayPayload(Input):
    card_id: str = Field(min_length=2, max_length=100)
    mode: PlayMode
    x: float | None = Field(default=None, allow_inf_nan=False, strict=True)
    y: float | None = Field(default=None, allow_inf_nan=False, strict=True)
    confirmed: StrictBool  # no implicit confirmation of a discard


class AnswerPayload(Input):
    question_id: str = Field(min_length=2, max_length=100)
    option_id: str = Field(pattern=r"^[a-e]$")


class ReviewPayload(Input):
    question_id: str = Field(min_length=2, max_length=100)
    correct: StrictBool


def decode_command(action: ActionRequest) -> c.Command:
    match action.type:
        case "play_card":
            p = PlayPayload.model_validate(action.payload)
            pos = Position(p.x, p.y) if p.x is not None and p.y is not None else None
            # One coordinate alone must not silently become a valid discard.
            if (p.x is None) != (p.y is None):
                raise ValueError("Invalid position")
            return c.PlayCard(p.card_id, p.mode, pos, p.confirmed)
        case "answer":
            a = AnswerPayload.model_validate(action.payload)
            return c.SelectAnswer(a.question_id, a.option_id)
        case "review_answer":
            r = ReviewPayload.model_validate(action.payload)
            return c.ReviewAnswer(r.question_id, r.correct)
        case _:
            Input.model_validate(action.payload)
            factories: dict[str, Callable[[], c.Command]] = {
                "begin_play": c.BeginPlay,
                "propose_finish": c.ProposeFinish,
                "confirm_finish": c.ConfirmFinish,
                "submit_answers": c.SubmitAnswers,
                "finish_game": c.FinishGame,
            }
            return factories[action.type]()
