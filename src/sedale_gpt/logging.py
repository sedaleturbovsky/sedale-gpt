"""JSON-line structured logging to stdout. Fly's log shipper picks it up."""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any


_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        payload: dict[str, Any] = {
            "ts": time.time(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # any extras passed via logger.info("...", extra={...})
        for k, v in record.__dict__.items():
            if k in {
                "args", "asctime", "created", "exc_info", "exc_text", "filename",
                "funcName", "levelname", "levelno", "lineno", "module", "msecs",
                "message", "msg", "name", "pathname", "process", "processName",
                "relativeCreated", "stack_info", "thread", "threadName",
            }:
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure() -> None:
    root = logging.getLogger()
    if getattr(root, "_sedale_configured", False):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root.handlers[:] = [handler]
    root.setLevel(_LEVEL)
    root._sedale_configured = True  # type: ignore[attr-defined]


def get_logger(name: str) -> logging.Logger:
    configure()
    return logging.getLogger(name)
