"""One bounded writer per socket. Queued data never contains private snapshots."""

import asyncio
import json
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import Any
from uuid import UUID

from starlette.websockets import WebSocket, WebSocketDisconnect

from .protocol import Limits


class Budget:
    def __init__(self, maximum: int):
        self.maximum = maximum
        self.hits: deque[float] = deque()

    def allow(self) -> bool:
        now = monotonic()
        while self.hits and self.hits[0] <= now - 1:
            self.hits.popleft()
        if len(self.hits) >= self.maximum:
            return False
        self.hits.append(now)
        return True


@dataclass(eq=False, repr=False)
class Connection:
    socket: WebSocket
    member_id: UUID
    session_id: UUID
    expires_at: datetime
    notified_revision: int
    limits: Limits
    queue: asyncio.Queue[tuple[str, int]] = field(init=False)
    queued_bytes: int = 0
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    close_code: int = 1000
    last_seen: float = field(default_factory=monotonic)
    budget: Budget = field(init=False)
    cursor_budget: Budget = field(init=False)
    preview_budget: Budget = field(init=False)

    def __post_init__(self) -> None:
        self.queue = asyncio.Queue(maxsize=self.limits.queue_messages)
        self.budget = Budget(self.limits.messages_per_second)
        self.cursor_budget = Budget(self.limits.cursors_per_second)
        self.preview_budget = Budget(self.limits.previews_per_second)

    def stop(self, code: int) -> None:
        if not self.stopped.is_set():
            self.close_code = code
            self.stopped.set()

    def expired(self) -> bool:
        return self.expires_at <= datetime.now(UTC)

    def offer(self, value: dict[str, Any]) -> bool:
        if self.expired():
            self.stop(4401)
        if self.stopped.is_set():
            return False
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        size = len(data.encode())
        if (
            size > self.limits.frame_bytes
            or self.queue.full()
            or self.queued_bytes + size > self.limits.queue_bytes
        ):
            self.stop(1013)
            return False
        self.queued_bytes += size
        self.queue.put_nowait((data, size))
        return True

    async def writer(self) -> None:
        stop_task = asyncio.create_task(self.stopped.wait())
        get_task: asyncio.Task[tuple[str, int]] | None = None
        try:
            while not self.stopped.is_set():
                get_task = asyncio.create_task(self.queue.get())
                done, _ = await asyncio.wait(
                    (get_task, stop_task), return_when=asyncio.FIRST_COMPLETED
                )
                if stop_task in done:
                    break
                data, size = get_task.result()
                get_task = None
                self.queued_bytes -= size
                if self.expired():
                    self.stop(4401)
                    break
                try:
                    await asyncio.wait_for(self.socket.send_text(data), self.limits.send_timeout)
                except TimeoutError:
                    self.stop(1013)
        except (WebSocketDisconnect, RuntimeError, OSError):
            self.stop(1001)
        finally:
            for task in (stop_task, get_task):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(t for t in (stop_task, get_task) if t is not None), return_exceptions=True
            )
            try:
                await asyncio.wait_for(self.socket.close(self.close_code), 1)
            except (WebSocketDisconnect, RuntimeError, OSError, TimeoutError):
                pass
            self.stopped.set()
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queued_bytes = 0
