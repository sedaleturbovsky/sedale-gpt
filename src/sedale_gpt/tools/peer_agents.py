"""consult_agent — outbound A2A client tool.

Sedale GPT is also an A2A *server*. This tool makes it an A2A *client* of
allowlisted peer agents, so the model can ask other agents for things only
they know (named entities, real portfolios, judgment calls).

The registry at `sedale_gpt/peer_registry.json` is the allowlist; no peer
URL is callable unless it's registered. Each registry entry carries the
peer's `protocol_version`, auth scheme, and a `good_for` / `do_not_ask`
hint set the model reads from the system prompt.

Two protocol paths are supported because the A2A spec has moved:

- v0.x peers: method name `message/send`, role string `"user"`, parts
  carry an explicit `"kind": "text"` discriminator.
- v1.0+ peers: method name `SendMessage` (gRPC-style PascalCase), role
  enum-string `"ROLE_USER"`, parts use protobuf oneof (no `kind` field),
  and the request needs an `A2A-Version: 1.0` HTTP header.

Per-task budget is enforced via a module-level counter dict keyed by
`task_id` (max 2 consults per task by default).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from importlib import resources
from typing import Any

import httpx

from ..logging import get_logger


log = get_logger("sedale_gpt.peer_agents")


MAX_CONSULTS_PER_TASK = 2
DEFAULT_TIMEOUT_SECONDS = 60


SPEC: dict[str, Any] = {
    "name": "consult_agent",
    "description": (
        "Ask a question of an allowlisted peer A2A agent. Use ONLY for "
        "judgment calls, named entities, or proprietary context the peer "
        "has and the public web does not. Read the peer's `good_for` and "
        "`do_not_ask` lists (provided in the 'Available peers' summary in "
        "the user message) before calling. Frame the question as a brief: "
        "include the project context the peer needs to give a specific "
        "answer rather than a generic one. At most 2 consults per task — "
        "spend them on the highest-leverage questions."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "peer_id": {
                "type": "string",
                "description": (
                    "Id of an allowlisted peer. Must match one of the ids "
                    "in the Available peers summary."
                ),
            },
            "question": {
                "type": "string",
                "description": (
                    "Specific, framed question. Include enough project "
                    "context for the peer to answer concretely."
                ),
            },
            "project_context": {
                "type": "string",
                "description": (
                    "1-2 sentence project anchor (sector, geography, "
                    "scale, sponsor type). Optional but strongly preferred."
                ),
            },
        },
        "required": ["peer_id", "question"],
    },
}

SPECS = [SPEC]


# ----------------------------------------------------------------------
# Registry

_REGISTRY_CACHE: dict[str, Any] | None = None


def _load_registry() -> dict[str, Any]:
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        text = resources.files("sedale_gpt").joinpath("peer_registry.json").read_text(encoding="utf-8")
        _REGISTRY_CACHE = json.loads(text)
    return _REGISTRY_CACHE


def _find_peer(peer_id: str) -> dict[str, Any] | None:
    for p in _load_registry().get("peers", []):
        if p.get("id") == peer_id:
            return p
    return None


def peer_summary_text() -> str:
    """Markdown summary of available peers, injected into every user message."""
    peers = _load_registry().get("peers", [])
    if not peers:
        return ""
    lines = ["## Available peers (consult_agent tool)\n"]
    for p in peers:
        lines.append(f"- **id**: `{p['id']}` — {p['name']} (operator: {p.get('operator', 'unknown')})")
        lines.append(f"  - skill: {p.get('skill_summary', '')}")
        if p.get("good_for"):
            lines.append(f"  - good_for: {'; '.join(p['good_for'])}")
        if p.get("do_not_ask"):
            lines.append(f"  - do_not_ask: {'; '.join(p['do_not_ask'])}")
        if p.get("protocol_version"):
            lines.append(f"  - protocol_version: {p['protocol_version']}")
    lines.append(
        "\nCall with `consult_agent(peer_id=..., question=..., project_context=...)`. "
        "At most 2 consults per task. Recommend-only for any agent NOT in this list."
    )
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Resolvers (pure functions, hermetic-testable)

def _resolve_method(protocol_version: str) -> str:
    """Pick the JSON-RPC method name based on the peer's protocol version."""
    if not protocol_version:
        return "message/send"
    major = str(protocol_version).split(".", 1)[0]
    try:
        return "SendMessage" if int(major) >= 1 else "message/send"
    except ValueError:
        return "message/send"


def _build_params(question_text: str, protocol_version: str) -> dict[str, Any]:
    """Build the JSON-RPC params block for SendMessage / message/send."""
    message_id = str(uuid.uuid4())
    major = str(protocol_version).split(".", 1)[0] if protocol_version else "0"
    is_v1 = major.isdigit() and int(major) >= 1
    if is_v1:
        return {
            "message": {
                "messageId": message_id,
                "role": "ROLE_USER",
                "parts": [{"text": question_text}],
            }
        }
    # v0.x spec shape — parts carry a kind discriminator, role is lowercase string
    return {
        "message": {
            "messageId": message_id,
            "role": "user",
            "kind": "message",
            "parts": [{"kind": "text", "text": question_text}],
        }
    }


def _build_headers(peer: dict[str, Any]) -> tuple[dict[str, str], str | None]:
    """Build outbound HTTP headers including auth. Returns (headers, error_or_none)."""
    headers: dict[str, str] = {
        "content-type": "application/json",
        "accept": "application/json",
    }
    pv = peer.get("protocol_version", "")
    major = str(pv).split(".", 1)[0] if pv else "0"
    is_v1 = major.isdigit() and int(major) >= 1
    if is_v1:
        headers["A2A-Version"] = "1.0"

    auth = peer.get("auth") or {}
    if auth and auth.get("type") == "api_key":
        secret_env = auth.get("secret_env")
        header_name = auth.get("header")
        if not (secret_env and header_name):
            return headers, "registry entry has incomplete auth block"
        secret = os.environ.get(secret_env)
        if not secret:
            return headers, f"missing env var {secret_env} (peer auth)"
        headers[header_name] = secret
    return headers, None


# ----------------------------------------------------------------------
# Budget — per-task counter

_BUDGETS: dict[str, int] = {}


def _consume_budget(task_id: str | None) -> bool:
    if not task_id:
        return True  # unscoped call (tests) — don't gate
    used = _BUDGETS.get(task_id, 0)
    if used >= MAX_CONSULTS_PER_TASK:
        return False
    _BUDGETS[task_id] = used + 1
    return True


def reset_budget(task_id: str) -> None:
    _BUDGETS.pop(task_id, None)


# ----------------------------------------------------------------------
# Response extraction (peer answers come back in various shapes)

def _extract_answer_text(rpc_response: dict[str, Any]) -> tuple[str, list[str]]:
    """Pull the textual answer out of a JSON-RPC SendMessage / message/send response.

    Returns (answer_text, source_urls_if_any). Handles three shapes:

    - v1: result.task.artifacts[*].parts[*].text OR result.msg.parts[*].text
    - v0.x spec: result.parts[*].text  (Message returned directly)
    - generic fallback: stringify the result for the model to triage
    """
    result = rpc_response.get("result", {})
    if not isinstance(result, dict):
        return (json.dumps(rpc_response, default=str)[:4000], [])

    texts: list[str] = []
    sources: list[str] = []

    # v1 task wrapper
    task = result.get("task") if isinstance(result.get("task"), dict) else None
    if task:
        for art in task.get("artifacts", []) or []:
            for p in art.get("parts", []) or []:
                if isinstance(p, dict) and "text" in p:
                    texts.append(p["text"])
        # Sometimes the task's status.message carries the response
        st = task.get("status", {})
        if isinstance(st, dict):
            msg = st.get("message", {})
            for p in (msg.get("parts", []) if isinstance(msg, dict) else []):
                if isinstance(p, dict) and "text" in p:
                    texts.append(p["text"])

    # v0.x direct-Message return (the spec's send returns the message)
    for p in result.get("parts", []) or []:
        if isinstance(p, dict) and "text" in p:
            texts.append(p["text"])

    # v0.x message wrapper
    msg = result.get("message") if isinstance(result.get("message"), dict) else None
    if msg:
        for p in msg.get("parts", []) or []:
            if isinstance(p, dict) and "text" in p:
                texts.append(p["text"])

    if not texts:
        # Last resort: return a truncated JSON dump so the model can triage
        return (json.dumps(result, default=str)[:4000], [])

    joined = "\n\n".join(t for t in texts if t)
    return joined.strip(), sources


# ----------------------------------------------------------------------
# Main tool entry point

async def consult(
    *,
    peer_id: str,
    question: str,
    project_context: str | None = None,
    task_id: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    peer = _find_peer(peer_id)
    if peer is None:
        ids = [p.get("id") for p in _load_registry().get("peers", [])]
        return {
            "error": f"unknown peer id '{peer_id}'. Available: {ids}. "
                      "Use the Available peers list in the user message."
        }

    if not _consume_budget(task_id):
        return {
            "error": (
                f"peer-call budget exhausted for this task "
                f"(max {MAX_CONSULTS_PER_TASK}). Spend remaining work on "
                "EXA / web_fetch / synthesis."
            )
        }

    headers, header_err = _build_headers(peer)
    if header_err:
        return {"peer": peer_id, "error": header_err}

    method = _resolve_method(peer.get("protocol_version", ""))
    question_text = (
        f"{project_context.strip()}\n\n{question.strip()}"
        if project_context else question.strip()
    )
    params = _build_params(question_text, peer.get("protocol_version", ""))
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": method,
        "params": params,
    }
    url = peer.get("rpc_url") or peer.get("card_url", "").rsplit("/.well-known", 1)[0]
    timeout = float(peer.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)

    log.info(
        "peer_consult_start",
        extra={
            "task_id": task_id,
            "peer_id": peer_id,
            "method": method,
            "url": url,
            "question_chars": len(question_text),
        },
    )

    own_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    started = time.time()
    try:
        resp = await client.post(url, json=payload, headers=headers, timeout=timeout)
        elapsed = time.time() - started
        body_text = resp.text
        try:
            body = resp.json()
        except Exception:
            return {
                "peer": peer_id,
                "error": f"peer returned non-JSON (HTTP {resp.status_code}): {body_text[:500]}",
                "wall_clock_seconds": round(elapsed, 2),
            }

        if "error" in body:
            log.warning(
                "peer_consult_rpc_error",
                extra={"task_id": task_id, "peer_id": peer_id, "error": body["error"]},
            )
            return {
                "peer": peer_id,
                "error": f"peer returned JSON-RPC error: {body['error']}",
                "wall_clock_seconds": round(elapsed, 2),
            }

        answer, sources = _extract_answer_text(body)
        log.info(
            "peer_consult_done",
            extra={
                "task_id": task_id,
                "peer_id": peer_id,
                "wall_clock_seconds": round(elapsed, 2),
                "answer_chars": len(answer),
            },
        )
        return {
            "peer": peer_id,
            "peer_name": peer.get("name", peer_id),
            "operator": peer.get("operator"),
            "answer": answer,
            "cited_sources": sources,
            "wall_clock_seconds": round(elapsed, 2),
        }
    except httpx.TimeoutException:
        return {
            "peer": peer_id,
            "error": f"peer timed out after {timeout}s",
            "wall_clock_seconds": round(time.time() - started, 2),
        }
    except httpx.HTTPError as exc:
        return {
            "peer": peer_id,
            "error": f"peer HTTP error: {exc!s}",
            "wall_clock_seconds": round(time.time() - started, 2),
        }
    finally:
        if own_client:
            await client.aclose()
