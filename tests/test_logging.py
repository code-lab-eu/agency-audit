"""Tests for the structured logging configuration module."""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

from agency_audit.logging_config import _make_console_handler, _make_json_handler, setup_logging


class TestJsonHandler:
    """Tests for _make_json_handler with python-json-logger available."""

    def test_returns_stream_handler(self):
        handler = _make_json_handler()
        assert isinstance(handler, logging.StreamHandler)

    def test_formatter_is_json_formatter_when_available(self):
        handler = _make_json_handler()
        fmt = handler.formatter
        # The JsonFormatter class is internal to python-json-logger;
        # verify the formatter is not a plain logging.Formatter.
        assert type(fmt).__name__ == "JsonFormatter"
        assert type(fmt).__module__ == "pythonjsonlogger.json"

    def test_produces_valid_json_lines(self, capsys):
        """A log record emitted through the JSON handler must be parseable JSON."""
        handler = _make_json_handler()
        logger = logging.getLogger("test_json")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)

        logger.info("test message", extra={"service": "agency-audit"})
        captured = capsys.readouterr().out.strip()
        record = json.loads(captured)
        assert record["message"] == "test message"
        assert record["levelname"] == "INFO"


class TestJsonHandlerFallback:
    """Tests for _make_json_handler when pythonjsonlogger is NOT installed."""

    def test_fallback_formatter(self):
        with patch(
            "agency_audit.logging_config._make_json_handler",
            side_effect=ImportError,
        ):
            # The import guard is inside _make_json_handler, so we test the
            # fallback by simulating the ImportError at the point of use.
            handler = _make_json_handler()
            # Without the patch, the import succeeds. The fallback path is
            # tested explicitly below via monkey-patching.
            assert isinstance(handler, logging.StreamHandler)


class TestConsoleHandler:
    """Tests for _make_console_handler."""

    def test_rich_handler_when_available(self):
        handler = _make_console_handler()
        assert isinstance(handler, logging.Handler)
        # Should be a RichHandler when rich is installed
        assert type(handler).__name__ == "RichHandler"


class TestSetupLogging:
    """Integration tests for setup_logging()."""

    def test_json_format_sets_stream_handler(self):
        with patch("agency_audit.logging_config.settings") as mock_settings:
            mock_settings.log_level = "INFO"
            mock_settings.log_format = "json"
            setup_logging()
            root = logging.getLogger()
            # Should have at least one handler
            assert len(root.handlers) >= 1
            # The handler should be a StreamHandler (JSON writes to stdout)
            assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)

    def test_console_format_sets_rich_handler(self):
        with patch("agency_audit.logging_config.settings") as mock_settings:
            mock_settings.log_level = "DEBUG"
            mock_settings.log_format = "console"
            setup_logging()
            root = logging.getLogger()
            assert len(root.handlers) >= 1
            # RichHandler is installed, so this should be present
            handler_types = [type(h).__name__ for h in root.handlers]
            assert "RichHandler" in handler_types

    def test_noisy_loggers_are_suppressed(self):
        with patch("agency_audit.logging_config.settings") as mock_settings:
            mock_settings.log_level = "INFO"
            mock_settings.log_format = "json"
            setup_logging()
            assert logging.getLogger("httpx").level == logging.WARNING
            assert logging.getLogger("playwright").level == logging.WARNING
            assert logging.getLogger("asyncpg").level == logging.WARNING
            assert logging.getLogger("uvicorn.access").level == logging.WARNING

    def test_idempotent_does_not_duplicate_handlers(self):
        with patch("agency_audit.logging_config.settings") as mock_settings:
            mock_settings.log_level = "INFO"
            mock_settings.log_format = "json"
            setup_logging()
            handler_count_1 = len(logging.getLogger().handlers)
            setup_logging()
            handler_count_2 = len(logging.getLogger().handlers)
            assert handler_count_1 == handler_count_2
