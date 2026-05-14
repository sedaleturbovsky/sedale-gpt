"""Cost and concurrency guardrails for the open A2A endpoint.

The endpoint is fully open by design, so the guardrails matter:

- Concurrency semaphore caps simultaneous in-flight tasks.
- Daily token ceiling caps Anthropic spend per UTC day; on trip, new tasks
  are rejected with a clear status until the counter resets.
- (Per-IP rate limit lives in the Starlette middleware, not here.)

Counters are process-local. Multi-instance deployment will need a shared
counter (Redis, Fly KV). v1 ships single-instance.
"""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass
class DailyCeiling:
    input_token_ceiling: int
    output_token_ceiling: int
    _day_started_at_utc: float = field(default_factory=lambda: time.time())
    _input_tokens: int = 0
    _output_tokens: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @classmethod
    def from_env(cls) -> "DailyCeiling":
        return cls(
            input_token_ceiling=int(os.environ.get("DAILY_INPUT_TOKEN_CEILING", 20_000_000)),
            output_token_ceiling=int(os.environ.get("DAILY_OUTPUT_TOKEN_CEILING", 2_000_000)),
        )

    def _maybe_reset(self) -> None:
        now = time.time()
        if now - self._day_started_at_utc >= 86400:
            self._day_started_at_utc = now
            self._input_tokens = 0
            self._output_tokens = 0

    async def check(self) -> tuple[bool, str]:
        async with self._lock:
            self._maybe_reset()
            if self._input_tokens >= self.input_token_ceiling:
                return False, "daily input token ceiling reached"
            if self._output_tokens >= self.output_token_ceiling:
                return False, "daily output token ceiling reached"
            return True, ""

    async def record(self, *, input_tokens: int, output_tokens: int) -> None:
        async with self._lock:
            self._maybe_reset()
            self._input_tokens += input_tokens
            self._output_tokens += output_tokens

    def snapshot(self) -> dict[str, int]:
        return {
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "input_ceiling": self.input_token_ceiling,
            "output_ceiling": self.output_token_ceiling,
        }


class ConcurrencyGate:
    def __init__(self, limit: int) -> None:
        self._sem = asyncio.Semaphore(limit)
        self.limit = limit

    @classmethod
    def from_env(cls) -> "ConcurrencyGate":
        return cls(limit=int(os.environ.get("MAX_CONCURRENT_TASKS", 3)))

    @asynccontextmanager
    async def acquire(self):
        await self._sem.acquire()
        try:
            yield
        finally:
            self._sem.release()
