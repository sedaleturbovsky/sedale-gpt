"""AgentCard sanity checks — no network."""
from __future__ import annotations

import pytest

from sedale_gpt.agent_card import build_card_set


def test_public_card_basic_shape():
    cards = build_card_set(public_url="https://sedale-gpt.fly.dev")
    public = cards.public
    assert public.name == "Sedale GPT"
    assert "1.1.0" == public.version
    assert "text/plain" in public.default_input_modes
    assert "application/json" in public.default_input_modes
    assert "text/markdown" in public.default_output_modes
    assert public.capabilities.streaming is True
    assert public.capabilities.push_notifications is False
    assert len(public.skills) == 1
    skill = public.skills[0]
    assert skill.id == "capital_formation_advisory"
    assert any("DOE LPO" in ex for ex in skill.examples)


def test_privileged_card_differs():
    cards = build_card_set(public_url="https://sedale-gpt.fly.dev")
    assert cards.privileged.name == "Sedale GPT (privileged)"
    public_url = cards.public.supported_interfaces[0].url
    priv_url = cards.privileged.supported_interfaces[0].url
    assert public_url == "https://sedale-gpt.fly.dev/a2a"
    assert priv_url == "https://sedale-gpt.fly.dev/a2a/privileged"
    assert "Bearer" in cards.privileged.description


def test_card_url_normalization():
    cards = build_card_set(public_url="https://example.test/")
    assert cards.public.supported_interfaces[0].url == "https://example.test/a2a"
