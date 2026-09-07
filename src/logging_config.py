"""
Structured JSON logging for production observability.

Logs to stdout as one JSON object per line so they ship cleanly to
Splunk, ELK, CloudWatch, or any log aggregator. Includes a correlation_id
field on every record so traces span the full request/response pipeline.
"""
from __future__ import annotations

import logging
import os
import sys
import uuid
from contextvars import ContextVar
from typing import Any

from pythonjsonlogger import jsonlogger


_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = _correlation_id.get()
        record.service = "nas-feedops-sentinel"
        return True


def configure_logging(level: str | None = None) -> None:
    """
    Configure root logger for structured JSON output. Idempotent.

    Level is taken from LOG_LEVEL env var if not passed (default INFO).
    """
    log_level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()

    root = logging.getLogger()
    # Drop any pre-existing handlers so the config is deterministic.
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        jsonlogger.JsonFormatter(
            "%(asctime)s %(name)s %(levelname)s %(correlation_id)s "
            "%(service)s %(message)s",
            rename_fields={"asctime": "timestamp", "levelname": "level"},
        )
    )
    handler.addFilter(_ContextFilter())

    root.setLevel(log_level)
    root.addHandler(handler)


def new_correlation_id() -> str:
    """Mint a fresh correlation id and bind it to the current context."""
    cid = uuid.uuid4().hex[:12]
    _correlation_id.set(cid)
    return cid


def set_correlation_id(cid: str) -> None:
    _correlation_id.set(cid)


def get_correlation_id() -> str:
    return _correlation_id.get()


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
