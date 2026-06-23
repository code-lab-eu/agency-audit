"""Tests for the operational loop module.

Tests cover: QC checks, re-audit scheduling, retry logic, progress tracking,
and orchestrator integration.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ──────────────────────────────────────────────────────────────────────
# Retry tests
# ──────────────────────────────────────────────────────────────────────


class TestRetry:
    """Tests for retry logic with exponential backoff."""

    async def test_retry_succeeds_first_attempt(self):
        """Retry should return the result on first success."""
        from agency_audit.loop.retry import retry

        call_count = 0

        async def success_func():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await retry(success_func, max_attempts=3)
        assert result == "ok"
        assert call_count == 1

    async def test_retry_succeeds_after_failures(self):
        """Retry should succeed after transient failures."""
        from agency_audit.loop.retry import retry

        call_count = 0

        async def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("transient error")
            return "recovered"

        result = await retry(flaky_func, max_attempts=5, base_delay=0.01)
        assert result == "recovered"
        assert call_count == 3

    async def test_retry_exhausted(self):
        """Retry should raise the last exception after exhausting attempts."""
        from agency_audit.loop.retry import retry

        async def always_fails():
            raise RuntimeError("always failing")

        with pytest.raises(RuntimeError, match="always failing"):
            await retry(always_fails, max_attempts=3, base_delay=0.01)

    async def test_retry_non_retryable_exception(self):
        """Non-retryable exceptions should propagate immediately."""
        from agency_audit.loop.retry import retry

        async def raises_type_error():
            raise TypeError("not retryable")

        # TypeError is not in the default retryable set (all exceptions),
        # but our default is Exception, so it WILL retry.
        # Test with a restricted set instead.
        with pytest.raises(TypeError, match="not retryable"):
            await retry(
                raises_type_error,
                max_attempts=3,
                base_delay=0.01,
                retryable_exceptions=(ValueError,),
            )

    async def test_retry_backoff_increases(self):
        """Retry delays should increase with each attempt."""
        from agency_audit.loop.retry import retry

        call_count = 0

        async def fails_twice():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("fail")
            return "ok"

        import time

        start = time.monotonic()
        result = await retry(fails_twice, max_attempts=3, base_delay=0.05, backoff_factor=2.0)
        elapsed = time.monotonic() - start

        assert result == "ok"
        # With base_delay=0.05, backoff=2x: delays = 0.05, 0.10 = 0.15s minimum
        assert elapsed >= 0.10  # at least two delays

    @pytest.mark.asyncio
    async def test_mark_failed_website_updates_status(self):
        """mark_failed_website should update website status to 'failed'."""
        from agency_audit.loop.retry import mark_failed_website

        with patch("agency_audit.loop.retry.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx

            await mark_failed_website(42, "test error message")

            assert mock_conn.execute.call_count == 2

            # First call: UPDATE websites
            update_call = mock_conn.execute.call_args_list[0]
            update_sql = update_call.args[0]
            assert "UPDATE websites" in update_sql
            assert "audit_status = 'failed'" in update_sql
            assert update_call.args[1] == "test error message"
            assert update_call.args[2] == 42

    @pytest.mark.asyncio
    async def test_mark_failed_website_audit_log_joins_cities(self):
        """audit_log INSERT must resolve country via cities JOIN, not website_cities."""
        from agency_audit.loop.retry import mark_failed_website

        with patch("agency_audit.loop.retry.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx

            await mark_failed_website(7, "network timeout")

            assert mock_conn.execute.call_count == 2

            # Second call: INSERT INTO audit_log via SELECT … JOIN
            insert_call = mock_conn.execute.call_args_list[1]
            insert_sql = insert_call.args[0]

            # The query must JOIN through cities to get the country
            assert "JOIN cities c ON wc.city_id = c.id" in insert_sql, (
                "Expected JOIN through cities table, got: " + insert_sql
            )

            # The SELECT must reference c.country, NOT wc.country
            assert "c.country" in insert_sql, "Expected c.country (from cities), got: " + insert_sql
            assert "wc.country" not in insert_sql, (
                "website_cities has no country column — must use c.country from cities JOIN"
            )

            # Verify the parameters
            assert insert_call.args[1] == "network timeout"
            assert insert_call.args[2] == 7


# ──────────────────────────────────────────────────────────────────────
# QC tests
# ──────────────────────────────────────────────────────────────────────


class TestQC:
    """Tests for quality control checks."""

    def test_extract_domain(self):
        """_extract_domain should normalize URLs correctly."""
        from agency_audit.loop.qc import _extract_domain

        assert _extract_domain("https://www.example.com/page") == "example.com"
        assert _extract_domain("https://example.com") == "example.com"
        assert _extract_domain("http://www.example.co.uk/path") == "example.co.uk"
        assert _extract_domain("https://subdomain.example.com") == "subdomain.example.com"
        assert _extract_domain("HTTP://WWW.EXAMPLE.COM") == "example.com"

    @pytest.mark.asyncio
    async def test_flag_suspicious_scores_empty(self):
        """flag_suspicious_scores should handle empty database gracefully."""
        from agency_audit.loop.qc import flag_suspicious_scores

        # We mock the database pool
        with patch("agency_audit.loop.qc.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            # No suspicious websites
            mock_conn.fetch.return_value = []

            findings = await flag_suspicious_scores()
            assert findings == []

    @pytest.mark.asyncio
    async def test_flag_suspicious_scores_found(self):
        """flag_suspicious_scores should detect scores of 0 and 100."""
        from agency_audit.loop.qc import flag_suspicious_scores

        with patch("agency_audit.loop.qc.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.fetch.return_value = [
                {"id": 1, "url": "https://example.com", "score": 0, "audit_status": "audited"},
                {"id": 2, "url": "https://example.org", "score": 100, "audit_status": "audited"},
            ]

            findings = await flag_suspicious_scores()
            assert len(findings) == 2
            assert findings[0].website_id == 1
            assert "score 0" in findings[0].reason.lower()
            assert findings[1].website_id == 2
            assert "score 100" in findings[1].reason.lower()

    @pytest.mark.asyncio
    async def test_detect_duplicates_empty(self):
        """detect_duplicates should handle empty results."""
        from agency_audit.loop.qc import detect_duplicates

        with patch("agency_audit.loop.qc.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.fetch.return_value = []

            findings = await detect_duplicates()
            assert findings == []

# ──────────────────────────────────────────────────────────────────────
# Re-audit tests
# ──────────────────────────────────────────────────────────────────────


class TestReaudit:
    """Tests for re-audit scheduling."""

    @pytest.mark.asyncio
    async def test_get_reaudit_queue_empty(self):
        """get_reaudit_queue should return empty when no overdue websites."""
        from agency_audit.loop.reaudit import get_reaudit_queue

        with patch("agency_audit.loop.reaudit.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.fetch.return_value = []

            queue = await get_reaudit_queue()
            assert queue == []

    @pytest.mark.asyncio
    async def test_schedule_reaudits_empty(self):
        """schedule_reaudits should return zero when nothing to queue."""
        from agency_audit.loop.reaudit import schedule_reaudits

        with patch("agency_audit.loop.reaudit.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.fetch.return_value = []

            result = await schedule_reaudits()
            assert result["queued"] == 0


# ──────────────────────────────────────────────────────────────────────
# Tracking tests
# ──────────────────────────────────────────────────────────────────────


class TestTracking:
    """Tests for progress tracking."""

    def test_make_json(self):
        """_make_json should serialize Python objects to JSON."""
        from agency_audit.loop.tracking import _make_json

        result = _make_json({"foo": "bar", "num": 42})
        assert '"foo": "bar"' in result
        assert "42" in result

    def test_audit_log_entry_defaults(self):
        """AuditLogEntry should have sensible defaults."""
        from agency_audit.loop.tracking import AuditLogEntry

        entry = AuditLogEntry()
        assert entry.run_type == "full_loop"
        assert entry.items_processed == 0
        assert entry.summary == {}

    @pytest.mark.asyncio
    async def test_get_progress_empty_db(self):
        """get_progress should handle empty database."""
        from agency_audit.loop.tracking import get_progress

        with patch("agency_audit.loop.tracking.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            # Return 0 for all counts
            mock_conn.fetchval = AsyncMock(return_value=0)
            mock_conn.fetch = AsyncMock(return_value=[])

            data = await get_progress()
            assert "overview" in data
            assert data["overview"]["cities_total"] == 0
            assert data["overview"]["websites_total"] == 0


# ──────────────────────────────────────────────────────────────────────
# Orchestrator import tests
# ──────────────────────────────────────────────────────────────────────


class TestOrchestrator:
    """Tests for the main orchestrator."""

    def test_run_country_importable(self):
        """run_country should be importable."""
        from agency_audit.loop.orchestrator import run_all_countries, run_country

        assert callable(run_country)
        assert callable(run_all_countries)

    def test_format_summary(self):
        """_format_summary should produce compact strings."""
        from agency_audit.loop.orchestrator import _format_summary

        result = {
            "phases": {
                "discovery": {"cities_processed": 5, "agencies_found": 12},
                "audit": {"succeeded": 10, "failed": 2},
                "qc": {"findings": 3},
                "reaudit": {"queued": 0},
            },
            "errors": [],
        }
        s = _format_summary(result)
        assert "discovery:5c/12a" in s
        assert "audit:10✓/2✗" in s
        assert "qc:3" in s
        assert "reaudit:0q" in s

    def test_format_totals(self):
        """_format_totals should produce aggregate summary."""
        from agency_audit.loop.orchestrator import _format_totals

        totals = {
            "countries_processed": 5,
            "cities_processed": 20,
            "agencies_found": 45,
            "audits_succeeded": 40,
            "audits_failed": 5,
            "qc_findings": 8,
            "reaudit_queued": 12,
        }
        s = _format_totals(totals)
        assert "5 countries" in s
        assert "20 cities" in s
        assert "45 agencies" in s
        assert "40✓/5✗ audits" in s
        assert "8 qc" in s
        assert "12 reaudits" in s


class TestAuditAttemptsCounter:
    """Tests for audit_attempts counter behaviour.

    audit_attempts must reset to 0 on success and increment only on failure,
    so that audit_attempts < 3 filters for *consecutive* failures rather than
    total lifetime attempts.
    """

    @pytest.mark.asyncio
    async def test_successful_audit_resets_attempts_to_zero(self):
        """On audit success, the UPDATE must set audit_attempts = 0."""
        from agency_audit.loop.orchestrator import _audit_country_websites

        with patch("agency_audit.loop.orchestrator.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx

            # Return one website to audit
            mock_conn.fetch.return_value = [{"id": 1, "url": "https://example.com"}]

            # Mock retry to succeed — returns a fake audit result
            class FakeAuditResult:
                @staticmethod
                def to_dict():
                    return {"score": 85}
                score = 85

            with patch("agency_audit.loop.orchestrator.retry",
                       new_callable=AsyncMock) as mock_retry:
                mock_retry.return_value = FakeAuditResult()

                result = await _audit_country_websites("BG", concurrency=1)

            assert result["succeeded"] == 1
            assert result["failed"] == 0

            # Collect all UPDATE calls on the mock connection
            update_calls = [
                call for call in mock_conn.execute.call_args_list
                if "UPDATE websites" in str(call.args[0])
            ]
            assert len(update_calls) >= 1, "Expected at least one UPDATE call"

            success_update = str(update_calls[0].args[0])
            assert "audit_attempts = 0" in success_update, (
                "successful audit should reset audit_attempts to 0, got: "
                + success_update
            )
            assert "audit_attempts = audit_attempts + 1" not in success_update, (
                "successful audit should NOT increment audit_attempts"
            )

    @pytest.mark.asyncio
    async def test_failed_audit_increments_attempts(self):
        """On audit failure, the UPDATE must keep audit_attempts = audit_attempts + 1."""
        from agency_audit.loop.orchestrator import _audit_country_websites

        with patch("agency_audit.loop.orchestrator.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx

            mock_conn.fetch.return_value = [{"id": 1, "url": "https://example.com"}]

            # Mock retry to fail
            with patch("agency_audit.loop.orchestrator.retry",
                       new_callable=AsyncMock) as mock_retry:
                mock_retry.side_effect = RuntimeError("audit failed after retries")

                result = await _audit_country_websites("BG", concurrency=1)

            assert result["failed"] == 1
            assert result["succeeded"] == 0

            # Find the failure UPDATE
            update_calls = [
                call for call in mock_conn.execute.call_args_list
                if "UPDATE websites" in str(call.args[0])
            ]
            assert len(update_calls) >= 1

            failure_update = str(update_calls[0].args[0])
            assert "audit_attempts = audit_attempts + 1" in failure_update, (
                "failed audit should increment audit_attempts, got: "
                + failure_update
            )
            assert "audit_attempts = 0" not in failure_update, (
                "failed audit should NOT reset audit_attempts to 0"
            )

    @pytest.mark.asyncio
    async def test_reaudit_scheduling_resets_attempts_to_zero(self):
        """Re-audit scheduling should reset audit_attempts to 0, not increment."""
        from agency_audit.loop.reaudit import schedule_reaudits

        with patch("agency_audit.loop.reaudit.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx

            # Return one overdue website
            mock_conn.fetch.return_value = [
                {"id": 42, "url": "https://example.com", "score": 75,
                 "age_days": 45},
            ]

            result = await schedule_reaudits(interval_days=30, limit=10)

            assert result["queued"] == 1

            # The UPDATE that sets status back to 'pending' should reset
            # audit_attempts to 0, not increment
            update_calls = [
                call for call in mock_conn.execute.call_args_list
                if "UPDATE websites" in str(call.args[0])
                and "SET audit_status = 'pending'" in str(call.args[0])
            ]
            assert len(update_calls) == 1, (
                "Expected exactly one UPDATE websites SET audit_status='pending' call"
            )

            reaudit_update = str(update_calls[0].args[0])
            assert "audit_attempts = 0" in reaudit_update, (
                "re-audit scheduling should reset audit_attempts to 0, got: "
                + reaudit_update
            )
            assert "audit_attempts = audit_attempts + 1" not in reaudit_update, (
                "re-audit scheduling should NOT increment audit_attempts"
            )


# ──────────────────────────────────────────────────────────────────────
# CLI integration tests
# ──────────────────────────────────────────────────────────────────────


class TestCLICommands:
    """Tests that CLI commands are registered."""

    def test_run_command_registered(self):
        """'run' command should be registered."""
        from agency_audit.cli import app

        commands = [c.name for c in app.registered_commands]
        assert "run" in commands

    def test_run_all_command_registered(self):
        """'run-all' command should be registered."""
        from agency_audit.cli import app

        commands = [c.name for c in app.registered_commands]
        assert "run-all" in commands

    def test_qc_command_registered(self):
        """'qc' command should be registered."""
        from agency_audit.cli import app

        commands = [c.name for c in app.registered_commands]
        assert "qc" in commands

    def test_reaudit_command_registered(self):
        """'reaudit' command should be registered."""
        from agency_audit.cli import app

        commands = [c.name for c in app.registered_commands]
        assert "reaudit" in commands

    def test_progress_command_registered(self):
        """'progress' command should be registered."""
        from agency_audit.cli import app

        commands = [c.name for c in app.registered_commands]
        assert "progress" in commands

    def test_existing_commands_still_registered(self):
        """Existing commands should still be registered."""
        from agency_audit.cli import app

        commands = [c.name for c in app.registered_commands]
        for cmd in [
            "db-init",
            "seed-countries",
            "import-cities",
            "serve",
            "audit",
            "batch-audit",
            "stats",
            "discover",
        ]:
            assert cmd in commands, f"{cmd} should be registered"
