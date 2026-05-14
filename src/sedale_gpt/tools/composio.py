"""Composio-backed side-effect tools (privileged route only).

These reach into Sedale's connected Gmail and Attio accounts. The
Authorization-bearer middleware on the privileged route is the only thing
between the open internet and these calls — that's a deliberate choice;
treat the privileged token like an API key.

We talk to Composio via its Python SDK. Imports are lazy so the open
route doesn't pay the cost when Composio isn't configured.
"""
from __future__ import annotations

import os
from typing import Any


DRAFT_EMAIL_SPEC: dict[str, Any] = {
    "name": "draft_email",
    "description": (
        "Create a draft email in Sedale's Gmail. Never sends. Use this "
        "to stage a follow-up to a funder, a program officer outreach, "
        "or a meeting recap — the draft sits in the Drafts folder for "
        "Sedale to review and send."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "to": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "cc": {"type": "array", "items": {"type": "string"}},
            "subject": {"type": "string"},
            "body_markdown": {"type": "string"},
        },
        "required": ["to", "subject", "body_markdown"],
    },
}

CREATE_ATTIO_NOTE_SPEC: dict[str, Any] = {
    "name": "create_attio_note",
    "description": (
        "Create a note in Attio attached to a company record. Use to "
        "save a capital stack memo or a funder briefing onto the right "
        "company in Sedale's CRM."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "company_id_or_domain": {"type": "string"},
            "title": {"type": "string"},
            "content_markdown": {"type": "string"},
        },
        "required": ["company_id_or_domain", "title", "content_markdown"],
    },
}

CREATE_ATTIO_DEAL_SPEC: dict[str, Any] = {
    "name": "create_attio_deal",
    "description": (
        "Create a deal record in Attio for a specific funding pursuit. "
        "Link it to the company and tag with the program name."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "company_id_or_domain": {"type": "string"},
            "deal_name": {"type": "string"},
            "amount_usd": {"type": "number"},
            "stage": {"type": "string", "description": "e.g., 'qualified', 'pursuing', 'submitted'"},
            "program": {"type": "string", "description": "e.g., 'DOE LPO Title 17 ICE'"},
        },
        "required": ["company_id_or_domain", "deal_name"],
    },
}


SPECS = [DRAFT_EMAIL_SPEC, CREATE_ATTIO_NOTE_SPEC, CREATE_ATTIO_DEAL_SPEC]


def _composio_configured() -> bool:
    return bool(os.environ.get("COMPOSIO_API_KEY"))


async def _execute(action: str, params: dict[str, Any]) -> dict[str, Any]:
    """Best-effort Composio invocation.

    Composio's Python SDK surface has changed across versions; we keep this
    indirection so the import is lazy and the failure mode is a clean JSON
    error rather than an import-time crash on the open route.
    """
    if not _composio_configured():
        return {"error": "Composio is not configured on this deployment."}
    try:
        from composio import Composio  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return {"error": f"composio SDK import failed: {exc!r}"}

    user_id = os.environ.get("COMPOSIO_USER_ID", "sedale@opengrants.io")
    try:
        composio = Composio()
        # SDK shape: composio.actions.execute(action=..., params=..., entity_id=...)
        result = composio.actions.execute(
            action=action,
            params=params,
            entity_id=user_id,
        )
        return {"result": result}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"composio execute failed for {action}: {exc!r}"}


async def draft_email(*, to: list[str], subject: str, body_markdown: str,
                      cc: list[str] | None = None) -> dict[str, Any]:
    params = {
        "recipient_email": ",".join(to),
        "subject": subject,
        "body": body_markdown,
        "is_html": False,
    }
    if cc:
        params["cc"] = ",".join(cc)
    return await _execute("GMAIL_CREATE_EMAIL_DRAFT", params)


async def create_attio_note(*, company_id_or_domain: str, title: str,
                            content_markdown: str) -> dict[str, Any]:
    return await _execute(
        "ATTIO_CREATE_NOTE",
        {
            "parent_object": "companies",
            "parent_record_id": company_id_or_domain,
            "title": title,
            "content": content_markdown,
            "format": "markdown",
        },
    )


async def create_attio_deal(*, company_id_or_domain: str, deal_name: str,
                            amount_usd: float | None = None,
                            stage: str | None = None,
                            program: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "name": deal_name,
        "company": company_id_or_domain,
    }
    if amount_usd is not None:
        params["value"] = amount_usd
    if stage:
        params["stage"] = stage
    if program:
        params["program"] = program
    return await _execute("ATTIO_CREATE_DEAL", params)
