"""Tool registry — single dispatcher used by the AgentExecutor.

`specs_for(variant)` returns the tool specs to advertise to Anthropic.
`run_tool_uses(blocks, ...)` executes whatever `tool_use` blocks Claude
emitted and returns the `tool_result` blocks to append to the next turn.
"""
from __future__ import annotations

import json
from enum import Enum
from typing import Any, Iterable

import httpx

from ..logging import get_logger
from . import composio as composio_tools
from . import exa as exa_tools
from . import peer_agents as peer_tools
from . import web as web_tools


log = get_logger("sedale_gpt.tools")


class RouteVariant(str, Enum):
    OPEN = "open"
    PRIVILEGED = "privileged"


OPEN_TOOLS = {
    "exa_search": exa_tools.search,
    "exa_get_contents": exa_tools.get_contents,
    "web_fetch": web_tools.fetch,
    "consult_agent": peer_tools.consult,
}

PRIVILEGED_ONLY_TOOLS = {
    "draft_email": composio_tools.draft_email,
    "create_attio_note": composio_tools.create_attio_note,
    "create_attio_deal": composio_tools.create_attio_deal,
}

OPEN_SPECS = [*exa_tools.SPECS, *web_tools.SPECS, *peer_tools.SPECS]
PRIVILEGED_SPECS = [*OPEN_SPECS, *composio_tools.SPECS]


class ToolRegistry:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    def specs_for(self, variant: RouteVariant) -> list[dict[str, Any]]:
        return PRIVILEGED_SPECS if variant == RouteVariant.PRIVILEGED else OPEN_SPECS

    def allowed_for(self, variant: RouteVariant) -> set[str]:
        if variant == RouteVariant.PRIVILEGED:
            return set(OPEN_TOOLS) | set(PRIVILEGED_ONLY_TOOLS)
        return set(OPEN_TOOLS)

    async def run_tool_uses(
        self,
        assistant_blocks: Iterable[Any],
        *,
        variant: RouteVariant,
        task_id: str,
    ) -> list[dict[str, Any]]:
        """Execute every tool_use block in the assistant turn.

        Returns the list of tool_result blocks ready to be wrapped in a
        user-role message and appended to the conversation.
        """
        allowed = self.allowed_for(variant)
        results: list[dict[str, Any]] = []
        for block in assistant_blocks:
            block_type = _block_type(block)
            if block_type != "tool_use":
                continue
            name = _block_field(block, "name")
            tool_use_id = _block_field(block, "id")
            args = _block_field(block, "input") or {}

            if name not in allowed:
                payload = {"error": f"tool '{name}' is not available on this route."}
                log.warning("tool_blocked", extra={"task_id": task_id, "tool": name, "variant": variant.value})
            else:
                payload = await self._dispatch(name, args, task_id=task_id)

            results.append({
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": [{"type": "text", "text": json.dumps(payload, default=str)}],
            })
        return results

    async def _dispatch(self, name: str, args: dict[str, Any], *, task_id: str | None = None) -> Any:
        fn = OPEN_TOOLS.get(name) or PRIVILEGED_ONLY_TOOLS.get(name)
        if fn is None:
            return {"error": f"unknown tool '{name}'"}
        try:
            if name in {"exa_search", "exa_get_contents", "web_fetch"}:
                client = await self._http()
                return await fn(client=client, **args)
            if name == "consult_agent":
                client = await self._http()
                return await fn(client=client, task_id=task_id, **args)
            return await fn(**args)
        except TypeError as exc:
            return {"error": f"bad arguments for {name}: {exc!s}"}
        except Exception as exc:  # noqa: BLE001
            log.exception("tool_call_failed", extra={"tool": name})
            return {"error": f"{name} raised: {exc!r}"}


def _block_type(block: Any) -> str:
    if isinstance(block, dict):
        return block.get("type", "")
    return getattr(block, "type", "")


def _block_field(block: Any, field: str) -> Any:
    if isinstance(block, dict):
        return block.get(field)
    return getattr(block, field, None)


def build_registry() -> ToolRegistry:
    return ToolRegistry()
