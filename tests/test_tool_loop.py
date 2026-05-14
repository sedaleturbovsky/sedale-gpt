"""Tool registry dispatcher tests — no Anthropic, no network."""
from __future__ import annotations

import pytest

from sedale_gpt.tools.registry import RouteVariant, build_registry


def test_specs_open_excludes_composio():
    registry = build_registry()
    names = {spec["name"] for spec in registry.specs_for(RouteVariant.OPEN)}
    assert "exa_search" in names
    assert "exa_get_contents" in names
    assert "web_fetch" in names
    assert "draft_email" not in names
    assert "create_attio_note" not in names
    assert "create_attio_deal" not in names


def test_specs_privileged_includes_composio():
    registry = build_registry()
    names = {spec["name"] for spec in registry.specs_for(RouteVariant.PRIVILEGED)}
    assert {"exa_search", "exa_get_contents", "web_fetch",
            "draft_email", "create_attio_note", "create_attio_deal"}.issubset(names)


@pytest.mark.asyncio
async def test_open_route_blocks_composio_tool_call():
    registry = build_registry()
    try:
        results = await registry.run_tool_uses(
            [
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "draft_email",
                    "input": {"to": ["x@y.z"], "subject": "no", "body_markdown": "no"},
                },
            ],
            variant=RouteVariant.OPEN,
            task_id="t",
        )
    finally:
        await registry.aclose()
    assert len(results) == 1
    payload = results[0]["content"][0]["text"]
    assert "not available on this route" in payload


@pytest.mark.asyncio
async def test_unknown_tool_returns_error():
    registry = build_registry()
    try:
        results = await registry.run_tool_uses(
            [
                {"type": "tool_use", "id": "tu_2", "name": "nope", "input": {}},
            ],
            variant=RouteVariant.PRIVILEGED,
            task_id="t",
        )
    finally:
        await registry.aclose()
    # nope is unknown — blocked at allowlist check before dispatch
    assert "not available on this route" in results[0]["content"][0]["text"]
