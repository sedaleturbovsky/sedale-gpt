"""Cost guardrail tests — process-local, no network."""
from __future__ import annotations

import asyncio

import pytest

from sedale_gpt.guards import ConcurrencyGate, DailyCeiling


@pytest.mark.asyncio
async def test_daily_ceiling_trips():
    ceiling = DailyCeiling(input_token_ceiling=100, output_token_ceiling=100)
    ok, _ = await ceiling.check()
    assert ok is True
    await ceiling.record(input_tokens=150, output_tokens=0)
    ok, reason = await ceiling.check()
    assert ok is False
    assert "input" in reason


@pytest.mark.asyncio
async def test_concurrency_gate_serializes():
    gate = ConcurrencyGate(limit=1)
    order = []

    async def worker(label: str, delay: float):
        async with gate.acquire():
            order.append(f"{label}-enter")
            await asyncio.sleep(delay)
            order.append(f"{label}-exit")

    await asyncio.gather(worker("a", 0.05), worker("b", 0.01))
    assert order in (
        ["a-enter", "a-exit", "b-enter", "b-exit"],
        ["b-enter", "b-exit", "a-enter", "a-exit"],
    )
