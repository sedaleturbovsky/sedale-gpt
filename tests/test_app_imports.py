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


def test_sdk_helper_signatures_we_depend_on() -> None:
    """Canary: a2a-sdk helper signatures the executor calls.

    A live A2A probe surfaced a TypeError because new_data_artifact's
    current signature is (name, data, media_type, description, artifact_id)
    — no metadata= kwarg. If the SDK shifts these, the executor's last
    two artifact-emit calls break only at the end of a successful LLM run,
    which is the most expensive place to discover a bug. Call them here
    with the exact kwargs the executor uses; success = canary green.
    """
    from a2a.helpers import new_data_artifact, new_text_artifact

    memo = new_text_artifact(name="capital_stack_memo", text="hello")
    assert memo is not None

    stack = new_data_artifact(
        name="capital_stack_structured",
        data={"schema_version": "1.0.0", "tranches": []},
        media_type="application/json",
        description="ok",
    )
    assert stack is not None


def test_agent_card_serializes_to_json() -> None:
    """AgentCard is a protobuf Message; _dump must produce a JSON-ready dict.

    Bug-history canary: a Pydantic-only _dump implementation crash-looped
    Fly because all of model_dump / dict / model_dump_json / json returned
    None on the protobuf object and the function fell through to TypeError.
    """
    import json as _json

    from sedale_gpt.agent_card import build_card_set
    from sedale_gpt.app import _dump

    cards = build_card_set(public_url="https://example.test")
    for card in (cards.public, cards.privileged):
        payload = _dump(card)
        assert isinstance(payload, dict)
        # Must round-trip cleanly through the stdlib JSON encoder.
        _json.dumps(payload)
        # A2A spec is camelCase — confirm we didn't accidentally emit snake_case.
        assert "defaultInputModes" in payload
        assert "default_input_modes" not in payload
        assert payload["name"].startswith("Sedale GPT")
        assert payload["skills"][0]["id"] == "capital_formation_advisory"
