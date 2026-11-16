"""Protocol v1: notifications only. Game commands and private views remain REST."""

import json
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter

VERSION = 1
SUBPROTOCOL = "detective.v1"


@dataclass(frozen=True)
class Limits:
    poll_interval: float = 0.25
    heartbeat_interval: float = 15
    heartbeat_timeout: float = 45
    send_timeout: float = 5
    database_timeout: float = 5
    input_bytes: int = 4096
    frame_bytes: int = 8192
    queue_messages: int = 64
    queue_bytes: int = 256 * 1024
    event_batch: int = 128
    member_connections: int = 4
    room_connections: int = 32
    total_connections: int = 256
    messages_per_second: int = 20
    cursors_per_second: int = 10
    previews_per_second: int = 8

    def __post_init__(self) -> None:
        if any(value <= 0 for value in vars(self).values()):
            raise ValueError("Realtime limits must be positive")
        if self.heartbeat_timeout <= self.heartbeat_interval:
            raise ValueError("Heartbeat timeout must exceed interval")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: StrictInt = Field(ge=VERSION, le=VERSION)


class Resume(Input):
    type: Literal["resume"]
    revision: StrictInt = Field(ge=0, le=2**31 - 1)


class Pong(Input):
    type: Literal["pong"]


class Ping(Input):
    type: Literal["ping"]


class Cursor(Input):
    type: Literal["cursor"]
    x: float = Field(strict=True, allow_inf_nan=False, ge=-1_000_000, le=1_000_000)
    y: float = Field(strict=True, allow_inf_nan=False, ge=-1_000_000, le=1_000_000)


class CardPreview(Input):
    type: Literal["card_preview"]
    x: float = Field(strict=True, allow_inf_nan=False, ge=-1_000_000, le=1_000_000)
    y: float = Field(strict=True, allow_inf_nan=False, ge=-1_000_000, le=1_000_000)
    card_id: str = Field(min_length=2, max_length=100)
    rotation: float = Field(strict=True, allow_inf_nan=False)
    version: StrictInt = Field(ge=0)


type ClientMessage = Resume | Pong | Ping | Cursor | CardPreview
_ADAPTER: TypeAdapter[ClientMessage] = TypeAdapter(
    Annotated[ClientMessage, Field(discriminator="type")]
)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value
    return result


def decode(data: str) -> ClientMessage:
    return _ADAPTER.validate_python(json.loads(data, object_pairs_hook=_pairs))


def message(type_: str, **fields: Any) -> dict[str, Any]:
    return {"protocol_version": VERSION, "type": type_, **fields}
