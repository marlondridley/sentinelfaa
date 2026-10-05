from __future__ import annotations

import logging
import re
import sys
import uuid
from contextvars import ContextVar, Token

from pythonjsonlogger.json import JsonFormatter

CORRELATION_HEADER = "x-correlation-id"

# What a caller may send as a correlation id. The ids this service mints are 12 hex characters; a caller's own are
# usually a UUID or a trace id, so letters, digits and . _ : - up to 64 characters take all of them. The value is
# logged, forwarded to the model gateway and, for a folder-scan ingest with no Idempotency-Key, names the Temporal
# workflow (sentinel-ingest-<id>), so anything else (control characters, spaces, a kilobyte of text) is not an id.
CORRELATION_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,64}")

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = _correlation_id.get()
        record.service = "nas-feedops-sentinel"
        return True


def configure_logging(level: str | None = None) -> None:
    """Configure root logger for structured JSON output. Idempotent."""
    try:
        from src.config import settings

        default_level = settings.LOG_LEVEL
    except Exception:  # noqa: BLE001
        default_level = "INFO"

    log_level = (level or default_level).upper()

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    formatter = JsonFormatter(
        fmt="%(asctime)s %(name)s %(levelname)s %(correlation_id)s %(service)s %(message)s",
        rename_fields={"asctime": "timestamp", "levelname": "level"},
    )
    handler.setFormatter(formatter)
    handler.addFilter(_ContextFilter())

    root.setLevel(log_level)
    root.addHandler(handler)


def new_correlation_id() -> str:
    """Mint a fresh correlation id and bind it to the current context."""
    cid = uuid.uuid4().hex[:12]
    _correlation_id.set(cid)
    return cid


def set_correlation_id(cid: str) -> Token[str]:
    """Set correlation ID and return the token for cleanup/reset."""
    return _correlation_id.set(cid)


def reset_correlation_id(token: Token[str]) -> None:
    """Reset the context variable to its previous state using a token."""
    _correlation_id.reset(token)


def get_correlation_id() -> str:
    return _correlation_id.get()


def inbound_correlation_id(raw: str | None) -> str | None:
    """
    The id a caller sent, if it is well-formed (CORRELATION_ID_PATTERN), else None: the caller mints a fresh one.

    For what arrives from outside, such as the request header. Not for bind_correlation_id in general: the pipeline
    and the activities bind a workflow key, which this service derived (an Idempotency-Key, a payload hash) and
    which is not a caller's id.
    """
    value = (raw or "").strip()
    return value if CORRELATION_ID_PATTERN.fullmatch(value) else None


def bind_correlation_id(cid: str | None) -> str:
    """Bind an inbound id (or mint one) and return the active value."""
    value = (cid or "").strip() or new_correlation_id()
    _correlation_id.set(value)
    return value


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
