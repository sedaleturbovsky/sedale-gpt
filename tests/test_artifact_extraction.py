"""Verify the executor pulls the memo + JSON fences out of a model response."""
from __future__ import annotations

from types import SimpleNamespace

from sedale_gpt.agent_executor import SedaleGPTExecutor


def _fake_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


def test_extracts_memo_and_json():
    # Avoid running __init__ — just call the bound method.
    extract = SedaleGPTExecutor._extract_artifacts.__get__(SedaleGPTExecutor.__new__(SedaleGPTExecutor))
    txt = """Here's the quick read.

```memo
# Capital stack for ACME Solar

Senior debt + 45Y PTC + sponsor equity. Not investment advice.
```

```capital_stack_json
{"schema_version": "1.0.0", "tranches": [{"category": "senior_debt", "amount_usd": 100000000}]}
```
"""
    memo, stack, prose = extract(_fake_response(txt))
    assert "ACME Solar" in memo
    assert stack["tranches"][0]["category"] == "senior_debt"
    assert "quick read" in prose


def test_no_fences_returns_full_text_as_memo():
    extract = SedaleGPTExecutor._extract_artifacts.__get__(SedaleGPTExecutor.__new__(SedaleGPTExecutor))
    memo, stack, prose = extract(_fake_response("just prose, no fences."))
    assert "just prose" in memo
    assert stack == {}
    assert prose == ""


def test_invalid_json_returns_error_payload():
    extract = SedaleGPTExecutor._extract_artifacts.__get__(SedaleGPTExecutor.__new__(SedaleGPTExecutor))
    txt = """```memo
ok
```

```capital_stack_json
{not valid json,,,}
```
"""
    memo, stack, _ = extract(_fake_response(txt))
    assert memo == "ok"
    assert "error" in stack and "invalid JSON" in stack["error"]
