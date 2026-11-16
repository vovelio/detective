"""Commands carry intent, never an asserted acting player or authority."""

from dataclasses import dataclass

from .models import PlayMode, Position


@dataclass(frozen=True)
class StartGame:
    pass


@dataclass(frozen=True)
class BeginPlay:
    pass


@dataclass(frozen=True)
class PlayCard:
    card_id: str
    mode: PlayMode
    position: Position | None = None
    confirmed: bool = True


@dataclass(frozen=True)
class ProposeFinish:
    pass


@dataclass(frozen=True)
class ConfirmFinish:
    pass


@dataclass(frozen=True)
class SelectAnswer:
    question_id: str
    option_id: str


@dataclass(frozen=True)
class SubmitAnswers:
    pass


@dataclass(frozen=True)
class ReviewAnswer:
    question_id: str
    correct: bool


@dataclass(frozen=True)
class FinishGame:
    pass


type Command = (
    StartGame
    | BeginPlay
    | PlayCard
    | ProposeFinish
    | ConfirmFinish
    | SelectAnswer
    | SubmitAnswers
    | ReviewAnswer
    | FinishGame
)
