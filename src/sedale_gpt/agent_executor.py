"""AgentExecutor — the Anthropic tool-use loop wrapped in A2A events.

Shape follows a2a-samples/.../helloworld/agent_executor.py. We own the
loop, the streaming surface, and the cancellation; the a2a-sdk owns the
JSON-RPC envelope and the task store.

The executor is constructed once at app startup and shared across
requests; it carries per-task asyncio.Event() for cooperative cancel.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from importlib import resources
from typing import Any

from anthropic import AsyncAnthropic

from a2a.helpers import (
    new_data_artifact,
    new_task_from_user_message,
    new_text_artifact,
    new_text_message,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types.a2a_pb2 import (
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)

from .guards import ConcurrencyGate, DailyCeiling
from .logging import get_logger
from .tools import peer_agents as peer_tools
from .tools.registry import RouteVariant, ToolRegistry


log = get_logger("sedale_gpt.executor")


MODEL = "claude-opus-4-7"
MAX_OUTPUT_TOKENS = 16_000
# Opus 4.7 uses adaptive thinking + an effort knob — the older
# {"type": "enabled", "budget_tokens": N} shape returns 400.
THINKING = {"type": "adaptive"}
OUTPUT_EFFORT = "high"  # "minimal" | "low" | "medium" | "high"
MAX_TOOL_ITERATIONS = 12


_FENCE_MEMO = re.compile(r"```memo\s*\n(?P<body>.*?)```", re.DOTALL)
_FENCE_JSON = re.compile(r"```capital_stack_json\s*\n(?P<body>.*?)```", re.DOTALL)


def _load_system_prompt() -> str:
    return resources.files("sedale_gpt").joinpath("system_prompt.md").read_text(encoding="utf-8")


SYSTEM_PROMPT = _load_system_prompt()


class SedaleGPTExecutor(AgentExecutor):
    """Wraps Anthropic Messages API in A2A's event-driven shape."""

    def __init__(
        self,
        *,
        anthropic: AsyncAnthropic,
        tools: ToolRegistry,
        gate: ConcurrencyGate,
        ceiling: DailyCeiling,
        route_variant: RouteVariant,
    ) -> None:
        self.anthropic = anthropic
        self.tools = tools
        self.gate = gate
        self.ceiling = ceiling
        self.route_variant = route_variant
        self._cancellations: dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------------
    # A2A AgentExecutor surface

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or str(uuid.uuid4())
        context_id = context.context_id or str(uuid.uuid4())
        cancel_event = asyncio.Event()
        self._cancellations[task_id] = cancel_event

        started = time.time()
        log_ctx = {
            "task_id": task_id,
            "context_id": context_id,
            "route_variant": self.route_variant.value,
            "model": MODEL,
        }

        try:
            ok, reason = await self.ceiling.check()
            if not ok:
                log.warning("daily_ceiling_tripped", extra={**log_ctx, "reason": reason})
                await self._emit_status(
                    event_queue, task_id, context_id,
                    f"Daily quota exhausted: {reason}. Try again after UTC midnight.",
                    TaskState.TASK_STATE_FAILED,
                )
                return

            async with self.gate.acquire():
                task = context.current_task or new_task_from_user_message(context.message)
                await event_queue.enqueue_event(task)

                await self._emit_status(
                    event_queue, task_id, context_id,
                    "Loading project brief and scoping research…",
                    TaskState.TASK_STATE_WORKING,
                )

                messages = self._a2a_message_to_anthropic(context.message)
                tool_specs = self.tools.specs_for(self.route_variant)
                total_input = 0
                total_output = 0

                resp = None
                for iteration in range(MAX_TOOL_ITERATIONS):
                    if cancel_event.is_set():
                        await self._emit_status(
                            event_queue, task_id, context_id,
                            "Cancelled by caller.",
                            TaskState.TASK_STATE_CANCELED,
                        )
                        return

                    resp = await self.anthropic.messages.create(
                        model=MODEL,
                        max_tokens=MAX_OUTPUT_TOKENS,
                        thinking=THINKING,
                        output_config={"effort": OUTPUT_EFFORT},
                        system=SYSTEM_PROMPT,
                        tools=tool_specs,
                        messages=messages,
                    )
                    usage = getattr(resp, "usage", None)
                    if usage is not None:
                        total_input += getattr(usage, "input_tokens", 0) or 0
                        total_output += getattr(usage, "output_tokens", 0) or 0

                    messages.append({"role": "assistant", "content": resp.content})

                    if resp.stop_reason == "end_turn":
                        log.info("loop_finished", extra={
                            **log_ctx,
                            "iteration": iteration,
                            "stop_reason": resp.stop_reason,
                        })
                        break

                    tool_blocks = [b for b in resp.content if _type_of(b) == "tool_use"]
                    if not tool_blocks:
                        # No tool calls and not end_turn — stop to avoid infinite loop.
                        break

                    tool_results = await self.tools.run_tool_uses(
                        resp.content, variant=self.route_variant, task_id=task_id,
                    )
                    messages.append({"role": "user", "content": tool_results})

                    await self._emit_status(
                        event_queue, task_id, context_id,
                        f"Researching ({iteration + 1}/{MAX_TOOL_ITERATIONS}) — {len(tool_blocks)} tool call(s)…",
                        TaskState.TASK_STATE_WORKING,
                    )
                else:
                    await self._emit_status(
                        event_queue, task_id, context_id,
                        "Hit max tool iterations; returning best-effort answer.",
                        TaskState.TASK_STATE_INPUT_REQUIRED,
                    )

                await self.ceiling.record(input_tokens=total_input, output_tokens=total_output)

                if resp is None:
                    await self._emit_status(
                        event_queue, task_id, context_id,
                        "No response produced.",
                        TaskState.TASK_STATE_FAILED,
                    )
                    return

                memo_md, stack_json, prose = self._extract_artifacts(resp)

                await event_queue.enqueue_event(TaskArtifactUpdateEvent(
                    task_id=task_id, context_id=context_id,
                    artifact=new_text_artifact(name="capital_stack_memo", text=memo_md),
                ))
                await event_queue.enqueue_event(TaskArtifactUpdateEvent(
                    task_id=task_id, context_id=context_id,
                    artifact=new_data_artifact(
                        name="capital_stack_structured",
                        data=stack_json,
                        media_type="application/json",
                        description="Structured capital stack v1.0.0 (schema_version inside the data payload).",
                    ),
                ))

                final_msg = prose or "Capital stack memo and structured stack delivered."
                await self._emit_status(
                    event_queue, task_id, context_id,
                    final_msg,
                    TaskState.TASK_STATE_COMPLETED,
                )

                log.info("task_completed", extra={
                    **log_ctx,
                    "wallclock_seconds": round(time.time() - started, 2),
                    "input_tokens": total_input,
                    "output_tokens": total_output,
                    "memo_chars": len(memo_md),
                    "stack_tranches": len(stack_json.get("tranches", []) if isinstance(stack_json, dict) else []),
                })
        except Exception as exc:  # noqa: BLE001
            log.exception("task_failed", extra=log_ctx)
            await self._emit_status(
                event_queue, task_id, context_id,
                f"Unhandled error: {exc!r}",
                TaskState.TASK_STATE_FAILED,
            )
        finally:
            self._cancellations.pop(task_id, None)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        evt = self._cancellations.get(context.task_id)
        if evt is not None:
            evt.set()

    # ------------------------------------------------------------------
    # Helpers

    async def _emit_status(
        self,
        event_queue: EventQueue,
        task_id: str,
        context_id: str,
        text: str,
        state: int,
    ) -> None:
        await event_queue.enqueue_event(TaskStatusUpdateEvent(
            task_id=task_id,
            context_id=context_id,
            status=TaskStatus(state=state, message=new_text_message(text)),
        ))

    def _a2a_message_to_anthropic(self, message: Any) -> list[dict[str, Any]]:
        """Flatten an A2A Message into a single Anthropic user turn.

        A2A Message.parts may be text, JSON data, or files. For now we
        accept text and JSON; files would need a separate ingestion path.

        Prepends an "Available peers" summary so the model knows which
        peer agents it can consult on this task.
        """
        parts = getattr(message, "parts", None) or []
        text_chunks: list[str] = []

        peer_summary = peer_tools.peer_summary_text()
        if peer_summary:
            text_chunks.append(peer_summary)

        brief_chunks: list[str] = []
        for part in parts:
            kind = _type_of(part)
            if kind in ("text", "TextPart"):
                brief_chunks.append(_field(part, "text") or "")
            elif kind in ("data", "DataPart"):
                data = _field(part, "data") or {}
                brief_chunks.append("Structured project brief (JSON):\n```json\n"
                                    + json.dumps(data, indent=2, default=str)
                                    + "\n```")
            else:
                brief_chunks.append(str(part))

        if brief_chunks:
            text_chunks.append("## Project brief\n\n" + "\n\n".join(brief_chunks))
        else:
            text_chunks.append("## Project brief\n\n(no project brief provided)")

        return [{"role": "user", "content": "\n\n".join(text_chunks)}]

    def _extract_artifacts(self, response: Any) -> tuple[str, dict[str, Any], str]:
        """Parse the model's final assistant turn into (memo_md, stack_json, prose)."""
        final_text = ""
        for block in response.content:
            if _type_of(block) == "text":
                final_text += _field(block, "text") or ""

        memo_match = _FENCE_MEMO.search(final_text)
        json_match = _FENCE_JSON.search(final_text)

        memo_md = memo_match.group("body").strip() if memo_match else final_text.strip()

        stack_json: dict[str, Any]
        if json_match:
            try:
                stack_json = json.loads(json_match.group("body").strip())
            except json.JSONDecodeError as exc:
                stack_json = {
                    "error": "model emitted invalid JSON in capital_stack_json fence",
                    "raw": json_match.group("body").strip()[:4000],
                    "parse_error": str(exc),
                }
        else:
            stack_json = {}

        prose = ""
        if memo_match:
            prose = final_text[: memo_match.start()].strip()
        return memo_md, stack_json, prose


def _type_of(block: Any) -> str:
    if isinstance(block, dict):
        return block.get("type", "")
    return getattr(block, "type", "") or block.__class__.__name__


def _field(block: Any, field: str) -> Any:
    if isinstance(block, dict):
        return block.get(field)
    return getattr(block, field, None)
