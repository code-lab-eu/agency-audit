"""Supplemental tests for loop module to close coverage gaps.

Covers: QC (mark_for_manual_review, run_qc_checks, get_websites_needing_review),
reaudit (non-empty results), retry (mark_failed_*), tracking (log_* functions).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ──────────────────────────────────────────────────────────────────────
# QC — mark_for_manual_review
# ──────────────────────────────────────────────────────────────────────


class TestQCMarkForReview:
    @pytest.mark.asyncio
    async def test_mark_for_manual_review_warning(self):
        from agency_audit.loop.qc import mark_for_manual_review

        with patch("agency_audit.loop.qc.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx

            await mark_for_manual_review(42, "test reason", severity="warning")
            mock_conn.execute.assert_called_once()
            call_args = mock_conn.execute.call_args
            # Third positional arg (index 2) is the JSON string for qc_checks
            assert "manual_review" in call_args[0][2]
            # First arg (index 0) is the reason string
            assert call_args[0][1] == "test reason"

    @pytest.mark.asyncio
    async def test_mark_for_manual_review_error_severity(self):
        from agency_audit.loop.qc import mark_for_manual_review

        with patch("agency_audit.loop.qc.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx

            await mark_for_manual_review(1, "critical issue", severity="error")
            call_args = mock_conn.execute.call_args
            # The JSON parameter contains severity
            assert "error" in call_args[0][2]


# ──────────────────────────────────────────────────────────────────────
# QC — run_qc_checks
# ──────────────────────────────────────────────────────────────────────


class TestQCRunChecks:
    @pytest.mark.asyncio
    async def test_run_qc_checks_empty(self):
        from agency_audit.loop.qc import run_qc_checks

        # Mock both flag_suspicious_scores and detect_duplicates
        with (
            patch("agency_audit.loop.qc.flag_suspicious_scores") as mock_flag,
            patch("agency_audit.loop.qc.detect_duplicates") as mock_dup,
        ):
            mock_flag.return_value = []
            mock_dup.return_value = []

            summary = await run_qc_checks()
            assert summary["suspicious_scores"] == 0
            assert summary["duplicate_domains"] == 0
            assert summary["total_findings"] == 0

    @pytest.mark.asyncio
    async def test_run_qc_checks_with_findings(self):
        from agency_audit.loop.qc import QCFinding, run_qc_checks

        with (
            patch("agency_audit.loop.qc.flag_suspicious_scores") as mock_flag,
            patch("agency_audit.loop.qc.detect_duplicates") as mock_dup,
        ):
            mock_flag.return_value = [
                QCFinding(1, "https://a.com", "score 0"),
                QCFinding(2, "https://b.com", "score 100"),
            ]
            mock_dup.return_value = [
                QCFinding(3, "https://c.com", "duplicate"),
            ]

            summary = await run_qc_checks()
            assert summary["suspicious_scores"] == 2
            assert summary["duplicate_domains"] == 1
            assert summary["total_findings"] == 3


# ──────────────────────────────────────────────────────────────────────
# QC — get_websites_needing_review
# ──────────────────────────────────────────────────────────────────────


class TestQCGetWebsitesNeedingReview:
    @pytest.mark.asyncio
    async def test_empty(self):
        from agency_audit.loop.qc import get_websites_needing_review

        with patch("agency_audit.loop.qc.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.fetch = AsyncMock(return_value=[])

            result = await get_websites_needing_review()
            assert result == []

    @pytest.mark.asyncio
    async def test_with_reviews(self):
        from agency_audit.loop.qc import get_websites_needing_review

        with patch("agency_audit.loop.qc.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.fetch = AsyncMock(
                return_value=[
                    {
                        "id": 1,
                        "url": "https://example.com",
                        "label": "Test",
                        "score": 0,
                        "review_reason": "Suspicious score 0",
                        "qc_checks": '[{"check":"suspicious_score"}]',
                    }
                ]
            )

            result = await get_websites_needing_review()
            assert len(result) == 1
            assert result[0]["url"] == "https://example.com"
            assert result[0]["score"] == 0


# ──────────────────────────────────────────────────────────────────────
# Re-audit — non-empty results
# ──────────────────────────────────────────────────────────────────────


class TestReauditWithResults:
    @pytest.mark.asyncio
    async def test_get_reaudit_queue_with_results(self):
        from agency_audit.loop.reaudit import get_reaudit_queue

        with patch("agency_audit.loop.reaudit.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.fetch = AsyncMock(
                return_value=[
                    {
                        "id": 1,
                        "url": "https://old-site.com",
                        "label": "Old Agency",
                        "score": 50,
                        "last_audited_at": None,
                        "age_days": 45,
                        "country": "BG",
                    }
                ]
            )

            queue = await get_reaudit_queue(interval_days=30)
            assert len(queue) == 1
            assert queue[0]["id"] == 1
            assert queue[0]["age_days"] == 45

    @pytest.mark.asyncio
    async def test_schedule_reaudits_with_results(self):
        from agency_audit.loop.reaudit import schedule_reaudits

        with patch("agency_audit.loop.reaudit.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.fetch = AsyncMock(
                return_value=[
                    {"id": 1, "url": "https://a.com", "score": 60, "age_days": 40},
                    {"id": 2, "url": "https://b.com", "score": 70, "age_days": 50},
                ]
            )

            result = await schedule_reaudits(interval_days=30)
            assert result["queued"] == 2
            assert result["oldest_age_days"] == 50


# ──────────────────────────────────────────────────────────────────────
# Retry — mark_failed_* functions
# ──────────────────────────────────────────────────────────────────────


class TestRetryMarkFailed:
    @pytest.mark.asyncio
    async def test_mark_failed_website(self):
        from agency_audit.loop.retry import mark_failed_website

        with patch("agency_audit.loop.retry.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx

            await mark_failed_website(1, "test error")
            assert mock_conn.execute.call_count == 2  # UPDATE + INSERT

    @pytest.mark.asyncio
    async def test_mark_failed_discovery(self):
        from agency_audit.loop.retry import mark_failed_discovery

        with patch("agency_audit.loop.retry.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx

            await mark_failed_discovery(5, "discovery error")
            assert mock_conn.execute.call_count == 2  # UPDATE + INSERT

    @pytest.mark.asyncio
    async def test_mark_failed_website_type(self):
        from agency_audit.loop.retry import mark_failed

        with patch("agency_audit.loop.retry.mark_failed_website") as mock_ws:
            await mark_failed("website", 42, "error")
            mock_ws.assert_called_once_with(42, "error")

    @pytest.mark.asyncio
    async def test_mark_failed_city_type(self):
        from agency_audit.loop.retry import mark_failed

        with patch("agency_audit.loop.retry.mark_failed_discovery") as mock_city:
            await mark_failed("city", 7, "error")
            mock_city.assert_called_once_with(7, "error")

    @pytest.mark.asyncio
    async def test_mark_failed_unknown_type(self):
        from agency_audit.loop.retry import mark_failed

        with pytest.raises(ValueError, match="Unknown item_type"):
            await mark_failed("unknown", 1, "error")


# ──────────────────────────────────────────────────────────────────────
# Tracking — log_* functions
# ──────────────────────────────────────────────────────────────────────


class TestTrackingLogFunctions:
    @pytest.mark.asyncio
    async def test_log_discovery_run(self):
        from agency_audit.loop.tracking import log_discovery_run

        with patch("agency_audit.loop.tracking.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.fetchval = AsyncMock(return_value=1)

            log_id = await log_discovery_run("BG", 5, 12, 2.5)
            assert log_id == 1
            mock_conn.fetchval.assert_called_once()

    @pytest.mark.asyncio
    async def test_log_discovery_run_with_errors(self):
        from agency_audit.loop.tracking import log_discovery_run

        with patch("agency_audit.loop.tracking.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.fetchval = AsyncMock(return_value=2)

            log_id = await log_discovery_run("BG", 3, 5, 1.0, errors=["err1", "err2"])
            assert log_id == 2

    @pytest.mark.asyncio
    async def test_log_audit_run(self):
        from agency_audit.loop.tracking import log_audit_run

        with patch("agency_audit.loop.tracking.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.fetchval = AsyncMock(return_value=3)  # country lookup
            # Called twice: once for country lookup, once for INSERT
            # But we patch fetchval to return 3 the second time too

            log_id = await log_audit_run(1, 75, 1.5, success=True)
            assert log_id == 3

    @pytest.mark.asyncio
    async def test_log_audit_run_with_country(self):
        from agency_audit.loop.tracking import log_audit_run

        with patch("agency_audit.loop.tracking.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.fetchval = AsyncMock(return_value=4)

            log_id = await log_audit_run(1, 80, 2.0, country="BG", success=False, error="timeout")
            assert log_id == 4

    @pytest.mark.asyncio
    async def test_log_full_loop_run(self):
        from agency_audit.loop.tracking import log_full_loop_run

        with patch("agency_audit.loop.tracking.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.fetchval = AsyncMock(return_value=5)

            log_id = await log_full_loop_run("BG", 10, 25, 20, 18, 2, 3, 5, 30.5)
            assert log_id == 5


# ──────────────────────────────────────────────────────────────────────
# RetryConfig
# ──────────────────────────────────────────────────────────────────────


class TestRetryConfigDefaults:
    def test_defaults(self):
        from agency_audit.loop.retry import RetryConfig

        config = RetryConfig()
        assert config.max_attempts == 3
        assert config.base_delay == 2.0
        assert config.backoff_factor == 2.0
        assert config.max_delay == 60.0

    def test_default_config_instance(self):
        from agency_audit.loop.retry import DEFAULT_RETRY_CONFIG

        assert DEFAULT_RETRY_CONFIG.max_attempts == 3


# ──────────────────────────────────────────────────────────────────────
# Re-audit — helpers
# ──────────────────────────────────────────────────────────────────────


class TestReauditHelpers:
    def test_make_json(self):
        from agency_audit.loop.reaudit import _make_json

        result = _make_json({"key": "value"})
        assert '"key": "value"' in result

    def test_default_reaudit_constants(self):
        from agency_audit.loop.reaudit import DEFAULT_REAUDIT_INTERVAL_DAYS, MAX_REAUDIT_BATCH

        assert DEFAULT_REAUDIT_INTERVAL_DAYS == 30
        assert MAX_REAUDIT_BATCH == 500
