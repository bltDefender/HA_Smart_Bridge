from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Hashable
from typing import Protocol

log = logging.getLogger(__name__)

Callback = Callable[[], Awaitable[None]]


class Timers(Protocol):
    def schedule(self, key: Hashable, delay: float, callback: Callback) -> None:
        """Ersetzt einen bestehenden Timer mit gleichem Schlüssel."""

    def cancel(self, key: Hashable) -> bool: ...

    def cancel_all(self) -> None: ...


class AsyncioTimers:
    def __init__(self) -> None:
        self._tasks: dict[Hashable, asyncio.Task[None]] = {}

    def schedule(self, key: Hashable, delay: float, callback: Callback) -> None:
        self.cancel(key)
        self._tasks[key] = asyncio.create_task(self._run(key, delay, callback))

    async def _run(self, key: Hashable, delay: float, callback: Callback) -> None:
        await asyncio.sleep(delay)
        self._tasks.pop(key, None)  # vor dem Callback, damit dieser neu planen kann
        try:
            await callback()
        except Exception:
            log.exception("timer callback failed: %r", key)

    def cancel(self, key: Hashable) -> bool:
        task = self._tasks.pop(key, None)
        if task is None:
            return False
        task.cancel()
        return True

    def cancel_all(self) -> None:
        for key in list(self._tasks):
            self.cancel(key)
