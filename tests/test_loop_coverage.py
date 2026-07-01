"""Supplemental tests for loop module to close coverage gaps.

Covers: QC (mark_for_manual_review, run_qc_checks, get_websites_needing_review),
reaudit (non-empty results), retry (mark_failed_*), tracking (log_* functions).

Each test that hits the database runs against a private, pristine database
provided by the ``fresh_db`` fixture (conftest.py): the canonical countries +
20 BG cities seed is present, the mutable tables start empty, and the database
is dropped on teardown.  Tests seed their own websites / discovery_log rows
and query actual city IDs instead of hard-coding primary keys; no manual
cleanup is needed and exact-count assertions are safe.

Non-database mocks (helper-function patches for run_qc_checks, mark_failed
routing) remain intact.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import asyncpg
import pytest

# ══════════════════════════════════════════════════════════════════════
# QC — mark_for_manual_review
# ══════════════════════════════════════════════════════════════════════


class TestQCMarkForReview:
    async def test_mark_for_manual_review_warning(self, fresh_db: asyncpg.Connection) -> None:
        from agency_audit.loop.qc import mark_for_manual_review

        website_id = await fresh_db.fetchval(
            "INSERT INTO websites (url, label) "
            "VALUES ('https://test-mr1.example.com', 'Test MR1') RETURNING id"
        )
        assert website_id is not None
        await mark_for_manual_review(website_id, "test reason", severity="warning")

        row = await fresh_db.fetchrow(
            "SELECT needs_review, review_reason, qc_checks FROM websites WHERE id = $1",
            website_id,
        )
        assert row is not None
        assert row["needs_review"] is True
        assert "test reason" in (row["review_reason"] or "")
        qc = json.loads(row["qc_checks"])
        assert any(e.get("check") == "manual_review" for e in qc)

    async def test_mark_for_manual_review_error_severity(
        self, fresh_db: asyncpg.Connection
    ) -> None:
        from agency_audit.loop.qc import mark_for_manual_review

        website_id = await fresh_db.fetchval(
            "INSERT INTO websites (url, label) "
            "VALUES ('https://test-mr2.example.com', 'Test MR2') RETURNING id"
        )
        assert website_id is not None
        await mark_for_manual_review(website_id, "critical issue", severity="error")

        row = await fresh_db.fetchrow("SELECT qc_checks FROM websites WHERE id = $1", website_id)
        assert row is not None
        qc = json.loads(row["qc_checks"])
        assert any(e.get("check") == "manual_review" and e.get("severity") == "error" for e in qc)


# ══════════════════════════════════════════════════════════════════════
# QC — run_qc_checks (mocks inner QC functions, not the database)
# ══════════════════════════════════════════════════════════════════════


class TestQCRunChecks:
    async def test_run_qc_checks_empty(self) -> None:
        from agency_audit.loop.qc import run_qc_checks

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

    async def test_run_qc_checks_with_findings(self) -> None:
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


# ══════════════════════════════════════════════════════════════════════
# QC — get_websites_needing_review
# ══════════════════════════════════════════════════════════════════════


class TestQCGetWebsitesNeedingReview:
    async def test_empty_when_no_flagged_sites(self, fresh_db: asyncpg.Connection) -> None:
        """Pristine database: no websites → empty review list."""
        from agency_audit.loop.qc import get_websites_needing_review

        result = await get_websites_needing_review()
        assert result == []

    async def test_with_reviews(self, fresh_db: asyncpg.Connection) -> None:
        from agency_audit.loop.qc import get_websites_needing_review

        # Insert one website flagged for review, one not.
        await fresh_db.execute(
            "INSERT INTO websites (url, label, score, needs_review, review_reason, qc_checks) "
            "VALUES ('https://test-review.example.com', 'Test Review', 0, true, "
            "'Suspicious score 0', '[{\"check\":\"suspicious_score\"}]'::jsonb)"
        )
        await fresh_db.execute(
            "INSERT INTO websites (url, label, score, needs_review) "
            "VALUES ('https://test-ok.example.com', 'Test OK', 85, false)"
        )

        result = await get_websites_needing_review()
        result_urls = {r["url"] for r in result}
        assert "https://test-review.example.com" in result_urls
        assert "https://test-ok.example.com" not in result_urls
        # Pristine database: exactly one flagged site.
        assert len(result) == 1
        review_row = result[0]
        assert review_row["score"] == 0


# ══════════════════════════════════════════════════════════════════════
# Re-audit — non-empty results
# ══════════════════════════════════════════════════════════════════════


class TestReauditWithResults:
    async def test_get_reaudit_queue_with_results(self, fresh_db: asyncpg.Connection) -> None:
        from agency_audit.loop.reaudit import get_reaudit_queue

        # Resolve Sofia's actual ID from the reference seed.
        sofia_id = await fresh_db.fetchval(
            "SELECT id FROM cities WHERE country = 'BG' AND slug = 'sofia'"
        )
        assert sofia_id is not None

        # Insert an audited website with an old last_audited_at date.
        website_id = await fresh_db.fetchval(
            "INSERT INTO websites (url, label, score, audit_status, "
            "needs_review, last_audited_at, audit_attempts) "
            "VALUES ('https://test-old.example.com', 'Old Agency', 50, 'audited', "
            "false, now() - INTERVAL '45 days', 0) RETURNING id"
        )
        assert website_id is not None
        await fresh_db.execute(
            "INSERT INTO website_cities (website_id, city_id) VALUES ($1, $2)",
            website_id,
            sofia_id,
        )

        queue = await get_reaudit_queue(interval_days=30)
        # Pristine database: our website is the only one in the queue.
        assert len(queue) == 1
        assert queue[0]["id"] == website_id
        assert queue[0]["age_days"] == 45

    async def test_schedule_reaudits_with_results(self, fresh_db: asyncpg.Connection) -> None:
        from agency_audit.loop.reaudit import schedule_reaudits

        # Resolve seeded BG city IDs.
        sofia_id = await fresh_db.fetchval(
            "SELECT id FROM cities WHERE country = 'BG' AND slug = 'sofia'"
        )
        plovdiv_id = await fresh_db.fetchval(
            "SELECT id FROM cities WHERE country = 'BG' AND slug = 'plovdiv'"
        )
        assert sofia_id is not None
        assert plovdiv_id is not None

        # Insert an ES city for a non-BG control — country 'ES' exists in the
        # reference seed (44 countries), but has no cities yet.
        madrid_id = await fresh_db.fetchval(
            "INSERT INTO cities (country, label, slug, population, latitude, longitude) "
            "VALUES ('ES', 'Madrid', 'madrid', 3200000, 40.4168, -3.7038) RETURNING id"
        )
        assert madrid_id is not None

        # Two BG websites — overdue, linked to seeded BG cities.
        w1 = await fresh_db.fetchval(
            "INSERT INTO websites (url, label, score, audit_status, "
            "needs_review, last_audited_at, audit_attempts) "
            "VALUES ('https://test-a.example.com', 'A', 60, 'audited', "
            "false, now() - INTERVAL '40 days', 0) RETURNING id"
        )
        assert w1 is not None
        await fresh_db.execute(
            "INSERT INTO website_cities (website_id, city_id) VALUES ($1, $2)", w1, sofia_id
        )
        w2 = await fresh_db.fetchval(
            "INSERT INTO websites (url, label, score, audit_status, "
            "needs_review, last_audited_at, audit_attempts) "
            "VALUES ('https://test-b.example.com', 'B', 70, 'audited', "
            "false, now() - INTERVAL '50 days', 0) RETURNING id"
        )
        assert w2 is not None
        await fresh_db.execute(
            "INSERT INTO website_cities (website_id, city_id) VALUES ($1, $2)", w2, plovdiv_id
        )

        # Non-BG control — overdue, linked to the ES city.
        w3 = await fresh_db.fetchval(
            "INSERT INTO websites (url, label, score, audit_status, "
            "needs_review, last_audited_at, audit_attempts) "
            "VALUES ('https://test-es.example.com', 'ES Agency', 80, 'audited', "
            "false, now() - INTERVAL '60 days', 0) RETURNING id"
        )
        assert w3 is not None
        await fresh_db.execute(
            "INSERT INTO website_cities (website_id, city_id) VALUES ($1, $2)", w3, madrid_id
        )

        result = await schedule_reaudits(interval_days=30, country="BG")
        assert result["queued"] == 2  # only BG, not ES
        assert result["oldest_age_days"] == 50

        # Verify BG websites were updated to 'pending' with reset attempts.
        for wid in (w1, w2):
            row = await fresh_db.fetchrow(
                "SELECT audit_status, audit_attempts FROM websites WHERE id = $1", wid
            )
            assert row is not None
            assert row["audit_status"] == "pending"
            assert row["audit_attempts"] == 0

        # ES website must NOT have been touched.
        row = await fresh_db.fetchrow(
            "SELECT audit_status, audit_attempts FROM websites WHERE id = $1", w3
        )
        assert row is not None
        assert row["audit_status"] == "audited"
        assert row["audit_attempts"] == 0

        # Verify an audit_log entry was created — scoped to the country + run_type
        # this test created (pristine database: exactly one such row).
        log_row = await fresh_db.fetchrow(
            "SELECT id, run_type, items_processed, summary FROM audit_log "
            "WHERE run_type = 'reaudit' AND country = 'BG'"
        )
        assert log_row is not None
        assert log_row["run_type"] == "reaudit"
        assert log_row["items_processed"] == 2


# ══════════════════════════════════════════════════════════════════════
# Retry — mark_failed_* functions
# ══════════════════════════════════════════════════════════════════════


class TestRetryMarkFailed:
    async def test_mark_failed_website(self, fresh_db: asyncpg.Connection) -> None:
        from agency_audit.loop.retry import mark_failed_website

        # Resolve Sofia's actual ID from the reference seed.
        sofia_id = await fresh_db.fetchval(
            "SELECT id FROM cities WHERE country = 'BG' AND slug = 'sofia'"
        )
        assert sofia_id is not None

        website_id = await fresh_db.fetchval(
            "INSERT INTO websites (url, label, audit_status, audit_attempts) "
            "VALUES ('https://test-fail.example.com', 'Fail', 'audited', 1) RETURNING id"
        )
        assert website_id is not None
        await fresh_db.execute(
            "INSERT INTO website_cities (website_id, city_id) VALUES ($1, $2)",
            website_id,
            sofia_id,
        )

        await mark_failed_website(website_id, "test error")

        # Check website state.
        row = await fresh_db.fetchrow(
            "SELECT audit_status, audit_last_error, audit_attempts FROM websites WHERE id = $1",
            website_id,
        )
        assert row is not None
        assert row["audit_status"] == "failed"
        assert row["audit_last_error"] == "test error"
        assert row["audit_attempts"] == 2  # was 1, incremented

        # Check audit_log entry — pristine database: exactly one 'audit' row with
        # this error.
        log_row = await fresh_db.fetchrow(
            "SELECT run_type, country, items_failed, error FROM audit_log "
            "WHERE error = 'test error'"
        )
        assert log_row is not None
        assert log_row["run_type"] == "audit"
        assert log_row["country"] == "BG"
        assert log_row["items_failed"] == 1

    async def test_mark_failed_discovery(self, fresh_db: asyncpg.Connection) -> None:
        from agency_audit.loop.retry import mark_failed_discovery

        # Resolve Sofia from the reference seed.
        sofia_id = await fresh_db.fetchval(
            "SELECT id FROM cities WHERE country = 'BG' AND slug = 'sofia'"
        )
        assert sofia_id is not None

        # Reset to pending first (seed cities start as 'pending', but be explicit).
        await fresh_db.execute(
            "UPDATE cities SET discovery_status = 'pending' WHERE id = $1", sofia_id
        )
        await mark_failed_discovery(sofia_id, "discovery error")

        row = await fresh_db.fetchrow("SELECT discovery_status FROM cities WHERE id = $1", sofia_id)
        assert row is not None
        assert row["discovery_status"] == "skipped"

        # Check discovery_log entry — pristine database: exactly one row.
        log_row = await fresh_db.fetchrow(
            "SELECT city_id, status, last_error, attempt FROM discovery_log "
            "WHERE city_id = $1 AND last_error = 'discovery error'",
            sofia_id,
        )
        assert log_row is not None
        assert log_row["status"] == "failed"
        assert log_row["attempt"] == 3

    async def test_mark_failed_website_type(self) -> None:
        from agency_audit.loop.retry import mark_failed

        with patch("agency_audit.loop.retry.mark_failed_website") as mock_ws:
            await mark_failed("website", 42, "error")
            mock_ws.assert_called_once_with(42, "error")

    async def test_mark_failed_city_type(self) -> None:
        from agency_audit.loop.retry import mark_failed

        with patch("agency_audit.loop.retry.mark_failed_discovery") as mock_city:
            await mark_failed("city", 7, "error")
            mock_city.assert_called_once_with(7, "error")

    async def test_mark_failed_unknown_type(self) -> None:
        from agency_audit.loop.retry import mark_failed

        with pytest.raises(ValueError, match="Unknown item_type"):
            await mark_failed("unknown", 1, "error")


# ══════════════════════════════════════════════════════════════════════
# Tracking — log_* functions
# ══════════════════════════════════════════════════════════════════════


class TestTrackingLogFunctions:
    async def test_log_discovery_run(self, fresh_db: asyncpg.Connection) -> None:
        from agency_audit.loop.tracking import log_discovery_run

        log_id = await log_discovery_run("BG", 5, 12, 2.5)
        assert isinstance(log_id, int)
        assert log_id > 0

        row = await fresh_db.fetchrow(
            "SELECT country, run_type, duration_seconds, items_processed, "
            "items_succeeded, items_failed, summary FROM audit_log WHERE id = $1",
            log_id,
        )
        assert row is not None
        assert row["country"] == "BG"
        assert row["run_type"] == "discovery"
        assert float(row["duration_seconds"]) == 2.5
        assert row["items_processed"] == 5
        assert row["items_succeeded"] == 12
        assert row["items_failed"] == 0
        summary = json.loads(row["summary"])
        assert summary["cities_processed"] == 5
        assert summary["agencies_found"] == 12

    async def test_log_discovery_run_with_errors(self, fresh_db: asyncpg.Connection) -> None:
        from agency_audit.loop.tracking import log_discovery_run

        log_id = await log_discovery_run("BG", 3, 5, 1.0, errors=["err1", "err2"])
        assert isinstance(log_id, int)
        assert log_id > 0

        row = await fresh_db.fetchrow(
            "SELECT items_failed, summary FROM audit_log WHERE id = $1",
            log_id,
        )
        assert row is not None
        assert row["items_failed"] == 2
        summary = json.loads(row["summary"])
        assert summary["errors"] == ["err1", "err2"]

    async def test_log_audit_run(self, fresh_db: asyncpg.Connection) -> None:
        from agency_audit.loop.tracking import log_audit_run

        sofia_id = await fresh_db.fetchval(
            "SELECT id FROM cities WHERE country = 'BG' AND slug = 'sofia'"
        )
        assert sofia_id is not None

        website_id = await fresh_db.fetchval(
            "INSERT INTO websites (url, label) "
            "VALUES ('https://test-audit.example.com', 'Test Audit') RETURNING id"
        )
        assert website_id is not None
        await fresh_db.execute(
            "INSERT INTO website_cities (website_id, city_id) VALUES ($1, $2)",
            website_id,
            sofia_id,
        )

        log_id = await log_audit_run(website_id, 75, 1.5, success=True)
        assert isinstance(log_id, int)
        assert log_id > 0

        row = await fresh_db.fetchrow(
            "SELECT country, run_type, items_succeeded, items_failed, summary "
            "FROM audit_log WHERE id = $1",
            log_id,
        )
        assert row is not None
        assert row["country"] == "BG"
        assert row["run_type"] == "audit"
        assert row["items_succeeded"] == 1
        assert row["items_failed"] == 0
        summary = json.loads(row["summary"])
        assert summary["website_id"] == website_id
        assert summary["score"] == 75

    async def test_log_audit_run_with_country(self, fresh_db: asyncpg.Connection) -> None:
        from agency_audit.loop.tracking import log_audit_run

        website_id = await fresh_db.fetchval(
            "INSERT INTO websites (url, label) "
            "VALUES ('https://test-audit2.example.com', 'Test Audit 2') RETURNING id"
        )
        assert website_id is not None

        log_id = await log_audit_run(
            website_id, 80, 2.0, country="BG", success=False, error="timeout"
        )
        assert isinstance(log_id, int)
        assert log_id > 0

        row = await fresh_db.fetchrow(
            "SELECT country, items_succeeded, items_failed, summary, error "
            "FROM audit_log WHERE id = $1",
            log_id,
        )
        assert row is not None
        assert row["country"] == "BG"
        assert row["items_succeeded"] == 0
        assert row["items_failed"] == 1
        assert row["error"] == "timeout"
        summary = json.loads(row["summary"])
        assert summary["score"] == 80

    async def test_log_full_loop_run(self, fresh_db: asyncpg.Connection) -> None:
        from agency_audit.loop.tracking import log_full_loop_run

        log_id = await log_full_loop_run("BG", 10, 25, 20, 18, 2, 3, 5, 30.5)
        assert isinstance(log_id, int)
        assert log_id > 0

        row = await fresh_db.fetchrow(
            "SELECT country, run_type, duration_seconds, items_processed, "
            "items_succeeded, items_failed, summary FROM audit_log WHERE id = $1",
            log_id,
        )
        assert row is not None
        assert row["country"] == "BG"
        assert row["run_type"] == "full_loop"
        assert float(row["duration_seconds"]) == 30.5
        assert row["items_processed"] == 30  # cities + websites
        assert row["items_succeeded"] == 43  # agencies + succeeded audits
        assert row["items_failed"] == 2
        summary = json.loads(row["summary"])
        assert summary["qc_findings"] == 3
        assert summary["reaudit_queued"] == 5


# ══════════════════════════════════════════════════════════════════════
# RetryConfig
# ══════════════════════════════════════════════════════════════════════


class TestRetryConfigDefaults:
    def test_defaults(self) -> None:
        from agency_audit.loop.retry import RetryConfig

        config = RetryConfig()
        assert config.max_attempts == 3
        assert config.base_delay == 2.0
        assert config.backoff_factor == 2.0
        assert config.max_delay == 60.0

    def test_default_config_instance(self) -> None:
        from agency_audit.loop.retry import DEFAULT_RETRY_CONFIG

        assert DEFAULT_RETRY_CONFIG.max_attempts == 3


# ══════════════════════════════════════════════════════════════════════
# Re-audit — helpers
# ══════════════════════════════════════════════════════════════════════


class TestReauditHelpers:
    def test_make_json(self) -> None:
        from agency_audit.loop.reaudit import _make_json

        result = _make_json({"key": "value"})
        assert '"key": "value"' in result

    def test_default_reaudit_constants(self) -> None:
        from agency_audit.loop.reaudit import (
            DEFAULT_REAUDIT_INTERVAL_DAYS,
            MAX_REAUDIT_BATCH,
        )

        assert DEFAULT_REAUDIT_INTERVAL_DAYS == 30
        assert MAX_REAUDIT_BATCH == 500
