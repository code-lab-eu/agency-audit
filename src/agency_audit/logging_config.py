"""Structured logging configuration for agency-audit.

Supports two formats controlled by ``AGENCY_AUDIT_LOG_FORMAT``:

* ``json`` (default) — JSON lines via python-json-logger, suitable for
  log aggregators (ELK, Loki, Datadog, etc.).
* ``console`` — human-readable coloured output via Rich.

Set ``AGENCY_AUDIT_LOG_LEVEL`` to control the minimum severity (default ``INFO``).
"""

from __future__ import annotations

import logging
import sys

from agency_audit.config import settings


def _make_json_handler() -> logging.Handler:
    """Create a stdout handler with a JSON-line formatter."""
    try:
        from pythonjsonlogger.json import JsonFormatter  # type: ignore[import-untyped]
    except ImportError:
        # Fall back to plain text — the user still gets structured output
        # instead of a crash.
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        handler: logging.Handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(fmt)
        return handler

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            json_ensure_ascii=False,
        )
    )
    return handler


def _make_console_handler() -> logging.Handler:
    """Create a Rich-powered coloured console handler."""
    try:
        from rich.logging import RichHandler

        handler: logging.Handler = RichHandler(rich_tracebacks=True, markup=True)
        handler.setFormatter(logging.Formatter("%(message)s", datefmt="[%X]"))
        return handler
    except ImportError:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        return handler


def setup_logging() -> None:
    """Configure the root logger for the application.

    Reads ``log_level`` and ``log_format`` from ``agency_audit.config.settings``.
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    # Remove any previously-attached handlers (idempotent)
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = _make_json_handler() if settings.log_format == "json" else _make_console_handler()

    handler.setLevel(level)
    root.addHandler(handler)

    # Quiet down noisy third-party loggers
    for noisy in ("uvicorn.access", "httpx", "playwright", "asyncpg"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
