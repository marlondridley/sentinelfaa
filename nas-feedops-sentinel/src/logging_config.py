from __future__ import annotations

import logging
import sys
import uuid
from contextvars import ContextVar, Token

from pythonjsonlogger import jsonlogger

CORRELATION_HEADER = "x-correlation-id"

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
    formatter = jsonlogger.JsonFormatter(
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


def bind_correlation_id(cid: str | None) -> str:
    """Bind an inbound id (or mint one) and return the active value."""
    value = (cid or "").strip() or new_correlation_id()
    _correlation_id.set(value)
    return value


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
