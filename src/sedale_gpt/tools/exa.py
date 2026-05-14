"""EXA search and contents tools.

Exposed to the Anthropic Messages API as tool specs. The runner calls the
EXA HTTP API directly so we don't need the exa-py SDK at runtime — keeps
the import surface small and deployment fast.
"""
from __future__ import annotations

import os
from typing import Any

import httpx


_EXA_BASE = "https://api.exa.ai"


SEARCH_SPEC: dict[str, Any] = {
    "name": "exa_search",
    "description": (
        "Semantic web search via Exa. Use for funder priorities, recent "
        "policy changes, named programs, peer-project precedent. Returns "
        "title, url, snippet, and an Exa-assigned relevance score. Use "
        "the returned urls with exa_get_contents or web_fetch when you "
        "need the full page."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural-language search query."},
            "num_results": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
            "include_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Restrict results to these domains (e.g., ['energy.gov', 'irs.gov']).",
            },
            "start_published_date": {
                "type": "string",
                "description": "ISO-8601 date floor for published-at (e.g., '2024-01-01').",
            },
        },
        "required": ["query"],
    },
}

CONTENTS_SPEC: dict[str, Any] = {
    "name": "exa_get_contents",
    "description": (
        "Fetch the cleaned, parsed text of one or more URLs via Exa's "
        "contents endpoint. Prefer this over web_fetch for pages where "
        "you want clean prose without nav/footer clutter."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 5,
            },
        },
        "required": ["urls"],
    },
}


SPECS = [SEARCH_SPEC, CONTENTS_SPEC]


async def search(
    *,
    query: str,
    num_results: int = 8,
    include_domains: list[str] | None = None,
    start_published_date: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    api_key = os.environ.get("EXA_API_KEY")
    if not api_key:
        return {"error": "EXA_API_KEY not configured"}
    payload: dict[str, Any] = {
        "query": query,
        "numResults": num_results,
        "type": "auto",
    }
    if include_domains:
        payload["includeDomains"] = include_domains
    if start_published_date:
        payload["startPublishedDate"] = start_published_date

    own_client = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    try:
        resp = await client.post(
            f"{_EXA_BASE}/search",
            headers={"x-api-key": api_key, "content-type": "application/json"},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        results = [
            {
                "title": r.get("title"),
                "url": r.get("url"),
                "snippet": r.get("text") or r.get("snippet"),
                "score": r.get("score"),
                "published_date": r.get("publishedDate"),
            }
            for r in data.get("results", [])
        ]
        return {"results": results}
    finally:
        if own_client:
            await client.aclose()


async def get_contents(
    *,
    urls: list[str],
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    api_key = os.environ.get("EXA_API_KEY")
    if not api_key:
        return {"error": "EXA_API_KEY not configured"}

    own_client = client is None
    client = client or httpx.AsyncClient(timeout=60.0)
    try:
        resp = await client.post(
            f"{_EXA_BASE}/contents",
            headers={"x-api-key": api_key, "content-type": "application/json"},
            json={"urls": urls, "text": True},
        )
        resp.raise_for_status()
        data = resp.json()
        out = []
        for r in data.get("results", []):
            text = r.get("text") or ""
            # Cap each page at 20KB to keep prompts manageable.
            if len(text) > 20_000:
                text = text[:20_000] + "\n…[truncated]"
            out.append({"url": r.get("url"), "title": r.get("title"), "text": text})
        return {"results": out}
    finally:
        if own_client:
            await client.aclose()
