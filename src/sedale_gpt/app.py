"""Starlette app factory.

Mounts:
- GET /.well-known/agent-card.json        → public AgentCard
- GET /.well-known/agent-card/privileged  → privileged AgentCard
- POST /a2a                                → public JSON-RPC (advisory only)
- POST /a2a/privileged                     → bearer-gated JSON-RPC (+ side effects)
- GET /healthz                              → trivial liveness

The two JSON-RPC mounts share state (Anthropic client, ToolRegistry,
ConcurrencyGate, DailyCeiling) so the cost guardrails apply globally to
the process.
"""
from __future__ import annotations

import json
import os

from anthropic import AsyncAnthropic
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

from .agent_card import build_card_set
from .agent_executor import SedaleGPTExecutor
from .guards import ConcurrencyGate, DailyCeiling
from .logging import configure as configure_logging, get_logger
from .tools.registry import RouteVariant, build_registry


log = get_logger("sedale_gpt.app")


class _BearerAuthMiddleware(BaseHTTPMiddleware):
    """Guards the /a2a/privileged mount with a single shared bearer token."""

    def __init__(self, app, expected_token: str) -> None:
        super().__init__(app)
        self._expected = expected_token

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "missing Authorization: Bearer <token>"},
                status_code=401,
            )
        token = auth.split(" ", 1)[1].strip()
        if token != self._expected:
            return JSONResponse({"error": "invalid bearer token"}, status_code=403)
        return await call_next(request)


def _agent_card_handler(card):
    async def _handler(_request: Request) -> JSONResponse:
        # AgentCard supports model_dump (pydantic-v2) or .to_dict()/asdict()
        payload = _dump(card)
        return JSONResponse(payload)
    return _handler


def _dump(card) -> dict:
    for attr in ("model_dump", "dict"):
        fn = getattr(card, attr, None)
        if callable(fn):
            try:
                return fn(by_alias=True, exclude_none=True)
            except TypeError:
                return fn()
    # Last-resort: JSON-round-trip via Pydantic's .json/.model_dump_json
    for attr in ("model_dump_json", "json"):
        fn = getattr(card, attr, None)
        if callable(fn):
            try:
                return json.loads(fn(by_alias=True, exclude_none=True))
            except TypeError:
                return json.loads(fn())
    raise TypeError(f"AgentCard {card!r} is not serializable")


async def _healthz(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def build_app() -> Starlette:
    configure_logging()

    public_url = os.environ.get("PUBLIC_URL", "http://127.0.0.1:8080").rstrip("/")
    cards = build_card_set(public_url=public_url)

    anthropic_client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    tool_registry = build_registry()
    gate = ConcurrencyGate.from_env()
    ceiling = DailyCeiling.from_env()

    # Shared task store — both routes write into the same store so a single
    # Fly machine has a coherent view of in-flight tasks.
    task_store = InMemoryTaskStore()

    public_executor = SedaleGPTExecutor(
        anthropic=anthropic_client,
        tools=tool_registry,
        gate=gate,
        ceiling=ceiling,
        route_variant=RouteVariant.OPEN,
    )
    privileged_executor = SedaleGPTExecutor(
        anthropic=anthropic_client,
        tools=tool_registry,
        gate=gate,
        ceiling=ceiling,
        route_variant=RouteVariant.PRIVILEGED,
    )

    public_handler = DefaultRequestHandler(
        agent_executor=public_executor,
        task_store=task_store,
        agent_card=cards.public,
    )
    privileged_handler = DefaultRequestHandler(
        agent_executor=privileged_executor,
        task_store=task_store,
        agent_card=cards.privileged,
    )

    routes = [
        Route("/.well-known/agent-card.json", _agent_card_handler(cards.public), methods=["GET"]),
        Route("/.well-known/agent-card/privileged", _agent_card_handler(cards.privileged), methods=["GET"]),
        Route("/healthz", _healthz, methods=["GET"]),
        # Public A2A — advisory tools only
        *create_jsonrpc_routes(public_handler, "/a2a"),
        # Privileged A2A — gated mount adds bearer auth
        Mount(
            "/a2a/privileged",
            routes=create_jsonrpc_routes(privileged_handler, "/"),
            middleware=[
                Middleware(
                    _BearerAuthMiddleware,
                    expected_token=os.environ.get("SEDALE_GPT_PRIVILEGED_TOKEN", ""),
                ),
            ],
        ),
        # Mirror the public card on the A2A-standard well-known route too.
        *create_agent_card_routes(cards.public),
    ]

    async def _on_startup() -> None:
        log.info("sedale_gpt_started", extra={
            "public_url": public_url,
            "ceiling": ceiling.snapshot(),
            "max_concurrent_tasks": gate.limit,
        })

    async def _on_shutdown() -> None:
        await tool_registry.aclose()

    app = Starlette(
        routes=routes,
        on_startup=[_on_startup],
        on_shutdown=[_on_shutdown],
    )
    return app
