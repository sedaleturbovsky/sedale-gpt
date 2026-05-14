"""CI canary: importing app.py must succeed end-to-end.

A previous deploy crash-looped on Fly because `app.py` imports
`from a2a.server.routes import create_jsonrpc_routes`, which transitively
imports `sse_starlette` — a dep that was missing from our pyproject.toml.
No prior test imported `app.py`, so CI was happy while production crashed.

This test does only one thing: import `build_app` and assert it's callable.
If any of app.py's imports break, this test fails on PR instead of on Fly.
"""
from __future__ import annotations


def test_build_app_imports() -> None:
    from sedale_gpt.app import build_app

    assert callable(build_app)


def test_build_app_constructs_without_anthropic_key(monkeypatch) -> None:
    """build_app() shouldn't crash if ANTHROPIC_API_KEY is unset.

    The Anthropic client tolerates a missing key at construction; the call
    fails later. We just want a clean startup so Fly health checks pass.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("PUBLIC_URL", "https://example.test")

    from sedale_gpt.app import build_app

    app = build_app()
    assert app is not None
