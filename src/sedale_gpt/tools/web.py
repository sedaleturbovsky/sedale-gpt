"""Raw URL fetch tool — for specific pages (a foundation's about page, a
DOE program notice, a state PUC docket, a 990) where you already have a
URL and want the cleaned text. Prefer exa_get_contents when you can.
"""
from __future__ import annotations

import re
from typing import Any

import httpx


SPEC: dict[str, Any] = {
    "name": "web_fetch",
    "description": (
        "Fetch a single URL and return cleaned text. Use when you already "
        "have a specific URL (program page, foundation strategic plan, "
        "agency notice) and need its contents. For discovery, use "
        "exa_search instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "max_chars": {
                "type": "integer",
                "minimum": 1000,
                "maximum": 40000,
                "default": 20000,
            },
        },
        "required": ["url"],
    },
}

SPECS = [SPEC]


_SCRIPT_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_NL_RE = re.compile(r"\n{3,}")


def _clean_html(html: str) -> str:
    text = _SCRIPT_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    text = (
        text.replace("&nbsp;", " ")
            .replace("&amp;", "&")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
    )
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n\n", text)
    return text.strip()


async def fetch(
    *,
    url: str,
    max_chars: int = 20_000,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    own_client = client is None
    client = client or httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        headers={
            "user-agent": "Sedale-GPT/1.0 (+https://opengrants.io) A2A-Agent"
        },
    )
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        body = resp.text
        if "html" in ctype.lower() or body.lstrip().startswith("<"):
            body = _clean_html(body)
        if len(body) > max_chars:
            body = body[:max_chars] + "\n…[truncated]"
        return {
            "url": str(resp.url),
            "status_code": resp.status_code,
            "content_type": ctype,
            "text": body,
        }
    except httpx.HTTPError as exc:
        return {"url": url, "error": str(exc)}
    finally:
        if own_client:
            await client.aclose()
