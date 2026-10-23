"""The single gate every upstream HTTP request must pass through.

Compliance core of the project (PRD section 6):
- every pair of adjacent request starts is spaced by a freshly drawn random
  interval within [global_min, global_max] seconds;
- "parse" requests additionally respect their own [parse_min, parse_max] lane;
- timing state lives in SQLite, so a restart never resets the clock;
- one serialized gate means no tool, job or retry can ever burst.
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable

from app.config import Settings
from app.store import Store

LAST_REQUEST_KEY = "last_request_at"
LAST_PARSE_KEY = "last_parse_at"


class RequestScheduler:
    """Serializes upstream requests and enforces randomized spacing."""

    def __init__(
        self,
        store: Store,
        settings: Settings,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[float, float], float] = random.uniform,
    ) -> None:
        # now/sleep/rng are injected so tests can run on a simulated clock.
        self._store = store
        self._settings = settings
        self._now = now
        self._sleep = sleep
        self._rng = rng
        self._gate = asyncio.Lock()

    async def acquire(self, kind: str = "standard") -> None:
        """Wait until this request may start; call right before sending."""
        async with self._gate:
            now = self._now()
            gap = self._rng(
                self._settings.global_interval_min, self._settings.global_interval_max
            )
            earliest = self._earliest(LAST_REQUEST_KEY, gap, now)
            if kind == "parse":
                parse_gap = self._rng(
                    self._settings.parse_interval_min, self._settings.parse_interval_max
                )
                earliest = max(earliest, self._earliest(LAST_PARSE_KEY, parse_gap, now))
            if earliest > now:
                await self._sleep(earliest - now)
            # Record the actual send moment, not the planned one.
            started = self._now()
            self._store.set_time(LAST_REQUEST_KEY, started)
            if kind == "parse":
                self._store.set_time(LAST_PARSE_KEY, started)

    def _earliest(self, key: str, gap: float, now: float) -> float:
        last = self._store.get_time(key)
        return last + gap if last is not None else now
