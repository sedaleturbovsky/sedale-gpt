"""Entrypoint — uvicorn bound to 0.0.0.0:$PORT (Fly convention)."""
from __future__ import annotations

import os

import uvicorn

from .app import build_app


def main() -> None:
    app = build_app()
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_config=None,  # we manage logging via sedale_gpt.logging
        access_log=False,
    )


if __name__ == "__main__":
    main()
