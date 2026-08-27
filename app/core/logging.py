"""Structured logging.

Every log line is JSON with a stable set of keys so an operator can grep or
ship them to a log aggregator without writing parsers. A `request_id` is bound
for the lifetime of each HTTP request via a context variable, which means a
retrieval or provider failure can be traced back to the exact user turn that
caused it.
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

import structlog

from app.core.config import settings

_request_id: ContextVar[str] = ContextVar("request_id", default="-")


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def set_request_id(value: str) -> None:
    _request_id.set(value)


def get_request_id() -> str:
    return _request_id.get()


def _inject_request_id(_logger: Any, _name: str, event: dict) -> dict:
    event["request_id"] = _request_id.get()
    return event


def configure_logging() -> None:
    """Idempotently configure structlog + stdlib logging."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)
    # uvicorn's access log duplicates our own request log line.
    logging.getLogger("uvicorn.access").disabled = True

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _inject_request_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


@contextmanager
def timed(logger: structlog.stdlib.BoundLogger, event: str, **fields: Any) -> Iterator[dict]:
    """Time a block and always emit exactly one log line describing it.

    The yielded dict can be mutated by the caller to attach results
    (row counts, model names) that are only known once the block has run.
    """
    started = time.perf_counter()
    extra: dict[str, Any] = {}
    try:
        yield extra
    except Exception as exc:
        logger.error(
            event,
            outcome="error",
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            error_type=type(exc).__name__,
            error=str(exc)[:500],
            **fields,
            **extra,
        )
        raise
    else:
        logger.info(
            event,
            outcome="ok",
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            **fields,
            **extra,
        )
