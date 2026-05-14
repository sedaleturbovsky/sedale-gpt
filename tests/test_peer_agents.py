"""Hermetic tests for peer_agents — no network."""
from __future__ import annotations

import asyncio

import pytest

from sedale_gpt.tools import peer_agents


# ----------------------------------------------------------------------
# Registry

def test_registry_loads_and_validates_seed_peer():
    reg = peer_agents._load_registry()
    assert "peers" in reg
    cic = next((p for p in reg["peers"] if p["id"] == "cic-project-agent"), None)
    assert cic is not None, "seed peer cic-project-agent must be present"
    assert cic["protocol_version"] == "0.2.5"
    assert cic["auth"]["header"] == "X-API-Key"
    assert cic["auth"]["secret_env"] == "PEER_API_KEY_CIC_PROJECT_AGENT"
    assert cic["rpc_url"].startswith("https://")
    assert isinstance(cic.get("good_for"), list) and len(cic["good_for"]) >= 1
    assert isinstance(cic.get("do_not_ask"), list) and len(cic["do_not_ask"]) >= 1


def test_peer_summary_text_includes_seed_peer_id():
    s = peer_agents.peer_summary_text()
    assert "cic-project-agent" in s
    assert "good_for" in s
    assert "do_not_ask" in s
    assert "Available peers" in s


# ----------------------------------------------------------------------
# Method resolver

@pytest.mark.parametrize("version, expected", [
    ("0.2.5", "message/send"),
    ("0.3", "message/send"),
    ("0", "message/send"),
    ("1.0", "SendMessage"),
    ("1.0.0", "SendMessage"),
    ("1.1", "SendMessage"),
    ("2.0", "SendMessage"),
    ("", "message/send"),
    ("garbage", "message/send"),
])
def test_resolve_method(version, expected):
    assert peer_agents._resolve_method(version) == expected


# ----------------------------------------------------------------------
# Params builder — version-aware shape

def test_build_params_v0_uses_spec_shape():
    p = peer_agents._build_params("hello", "0.2.5")["message"]
    assert p["role"] == "user"
    assert p["parts"] == [{"kind": "text", "text": "hello"}]
    assert "messageId" in p


def test_build_params_v1_uses_proto_shape():
    p = peer_agents._build_params("hello", "1.0")["message"]
    assert p["role"] == "ROLE_USER"
    assert p["parts"] == [{"text": "hello"}]
    assert "messageId" in p


# ----------------------------------------------------------------------
# Headers

def test_build_headers_v1_includes_a2a_version_header(monkeypatch):
    peer = {"protocol_version": "1.0", "auth": None}
    headers, err = peer_agents._build_headers(peer)
    assert err is None
    assert headers["A2A-Version"] == "1.0"


def test_build_headers_v0_omits_a2a_version_header():
    peer = {"protocol_version": "0.2.5"}
    headers, err = peer_agents._build_headers(peer)
    assert err is None
    assert "A2A-Version" not in headers


def test_build_headers_api_key_resolved_from_env(monkeypatch):
    monkeypatch.setenv("PEER_API_KEY_CIC_PROJECT_AGENT", "k-abc123")
    peer = {
        "protocol_version": "0.2.5",
        "auth": {"type": "api_key", "header": "X-API-Key", "secret_env": "PEER_API_KEY_CIC_PROJECT_AGENT"},
    }
    headers, err = peer_agents._build_headers(peer)
    assert err is None
    assert headers["X-API-Key"] == "k-abc123"


def test_build_headers_missing_secret_returns_error(monkeypatch):
    monkeypatch.delenv("PEER_API_KEY_CIC_PROJECT_AGENT", raising=False)
    peer = {
        "protocol_version": "0.2.5",
        "auth": {"type": "api_key", "header": "X-API-Key", "secret_env": "PEER_API_KEY_CIC_PROJECT_AGENT"},
    }
    headers, err = peer_agents._build_headers(peer)
    assert err is not None and "PEER_API_KEY_CIC_PROJECT_AGENT" in err


# ----------------------------------------------------------------------
# Tool entry-point: error paths (no network)

def test_unknown_peer_id_returns_error():
    result = asyncio.run(peer_agents.consult(peer_id="not-a-real-peer", question="hi"))
    assert "error" in result
    assert "unknown peer" in result["error"].lower()


def test_budget_exhaustion(monkeypatch):
    task_id = "test-budget-task"
    peer_agents.reset_budget(task_id)
    monkeypatch.delenv("PEER_API_KEY_CIC_PROJECT_AGENT", raising=False)
    # First two calls reach the auth check (env missing -> err); third trips budget.
    r1 = asyncio.run(peer_agents.consult(peer_id="cic-project-agent", question="q", task_id=task_id))
    r2 = asyncio.run(peer_agents.consult(peer_id="cic-project-agent", question="q", task_id=task_id))
    r3 = asyncio.run(peer_agents.consult(peer_id="cic-project-agent", question="q", task_id=task_id))
    assert "error" in r3
    assert "budget" in r3["error"].lower()
    peer_agents.reset_budget(task_id)


# ----------------------------------------------------------------------
# Response extractor

def test_extract_answer_v0_direct_message():
    body = {"jsonrpc": "2.0", "id": "x", "result": {
        "kind": "message", "role": "agent",
        "parts": [{"kind": "text", "text": "Yes, we have three solar projects."}],
    }}
    answer, sources = peer_agents._extract_answer_text(body)
    assert "three solar projects" in answer
    assert sources == []


def test_extract_answer_v1_task_artifacts():
    body = {"jsonrpc": "2.0", "id": "x", "result": {"task": {
        "artifacts": [{"name": "answer", "parts": [{"text": "the answer"}]}],
        "status": {"state": "TASK_STATE_COMPLETED"},
    }}}
    answer, _ = peer_agents._extract_answer_text(body)
    assert "the answer" in answer


def test_extract_answer_empty_falls_back_to_json_dump():
    body = {"jsonrpc": "2.0", "id": "x", "result": {"unrecognized": "shape"}}
    answer, _ = peer_agents._extract_answer_text(body)
    assert "unrecognized" in answer
