from __future__ import annotations


class Backoff:
    """Exponentielles Backoff ohne Jitter (deterministisch testbar)."""

    def __init__(self, initial: float = 1.0, maximum: float = 60.0, factor: float = 2.0) -> None:
        self._initial, self._max, self._factor = initial, maximum, factor
        self._next = initial

    def next_delay(self) -> float:
        delay = self._next
        self._next = min(self._next * self._factor, self._max)
        return delay

    def reset(self) -> None:
        self._next = self._initial
