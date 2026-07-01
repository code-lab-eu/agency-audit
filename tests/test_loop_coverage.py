"""Supplemental tests for loop module to close coverage gaps.

Covers: QC (mark_for_manual_review, run_qc_checks, get_websites_needing_review),
reaudit (non-empty results), retry (mark_failed_*), tracking (log_* functions).

Migrated from mocked database calls to the real database (shared db_conn fixture).
Non-database mocks (helper-function patches) remain intact.

All database-hitting tests accept the ``postgres_dsn`` fixture and monkeypatch
``settings`` so that seed connections, ``get_pool()``, and assertions all target
the same database — no ambient-state dependency.
"""

from __future__ import annotations

import json
from unittest.mock import patch
from urllib.parse import urlparse

import asyncpg
import pytest

from agency_audit.config import settings
from agency_audit.db import close_pool

# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


async def _seed_conn(dsn: str) -> asyncpg.Connection:
    """Return an auto-commit connection for seeding test data.

    Data inserted through this connection is immediately committed and
    visible to get_pool() connections used by the functions under test.
    Uses *dsn* (from the postgres_dsn fixture) so all connections target
    the same database.
    """
    return await asyncpg.connect(dsn=dsn)


def _point_settings_at_fixture_db(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkeypatch global settings so ``get_pool()`` connects to the fixture DB."""
    parsed = urlparse(postgres_dsn)
    monkeypatch.setattr(settings, "pg_host", parsed.hostname or "localhost")
    monkeypatch.setattr(settings, "pg_port", parsed.port or 5432)
    monkeypatch.setattr(settings, "pg_database", (parsed.path or "/agency_audit").lstrip("/"))
    monkeypatch.setattr(settings, "pg_user", parsed.username or "agency_audit")
    monkeypatch.setattr(settings, "pg_password", parsed.password or "")


@pytest.fixture(autouse=True)
async def _fresh_pool() -> None:
    """Close the shared pool after each test so the next gets a fresh one."""
    yield
    await close_pool()


# ──────────────────────────────────────────────────────────────────────
# QC — mark_for_manual_review
# ──────────────────────────────────────────────────────────────────────


class TestQCMarkForReview:
    async def test_mark_for_manual_review_warning(
        self, db_conn: asyncpg.Connection, postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agency_audit.loop.qc import mark_for_manual_review

        _point_settings_at_fixture_db(postgres_dsn, monkeypatch)

        seed = await _seed_conn(postgres_dsn)
        try:
            website_id = await seed.fetchval(
                "INSERT INTO websites (url, label) "
                "VALUES ('https://test-mr1.example.com', 'Test MR1') RETURNING id"
            )
            await mark_for_manual_review(website_id, "test reason", severity="warning")

            row = await db_conn.fetchrow(
                "SELECT needs_review, review_reason, qc_checks FROM websites WHERE id = $1",
                website_id,
            )
            assert row["needs_review"] is True
            assert "test reason" in (row["review_reason"] or "")
            qc = json.loads(row["qc_checks"])
            assert any(e.get("check") == "manual_review" for e in qc)
        finally:
            await seed.execute("DELETE FROM websites WHERE url LIKE 'https://test-mr%'")
            await seed.close()

    async def test_mark_for_manual_review_error_severity(
        self, db_conn: asyncpg.Connection, postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agency_audit.loop.qc import mark_for_manual_review

        _point_settings_at_fixture_db(postgres_dsn, monkeypatch)

        seed = await _seed_conn(postgres_dsn)
        try:
            website_id = await seed.fetchval(
                "INSERT INTO websites (url, label) "
                "VALUES ('https://test-mr2.example.com', 'Test MR2') RETURNING id"
            )
            await mark_for_manual_review(website_id, "critical issue", severity="error")

            row = await db_conn.fetchrow("SELECT qc_checks FROM websites WHERE id = $1", website_id)
            qc = json.loads(row["qc_checks"])
            assert any(
                e.get("check") == "manual_review" and e.get("severity") == "error" for e in qc
            )
        finally:
            await seed.execute("DELETE FROM websites WHERE url LIKE 'https://test-mr%'")
            await seed.close()


# ──────────────────────────────────────────────────────────────────────
# QC — run_qc_checks (mocks inner QC functions, not the database)
# ──────────────────────────────────────────────────────────────────────


class TestQCRunChecks:
    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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


# ──────────────────────────────────────────────────────────────────────
# QC — get_websites_needing_review
# ──────────────────────────────────────────────────────────────────────


class TestQCGetWebsitesNeedingReview:
    async def test_no_owned_reviews(
        self, db_conn: asyncpg.Connection, postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agency_audit.loop.qc import get_websites_needing_review

        _point_settings_at_fixture_db(postgres_dsn, monkeypatch)

        # Clean any leftovers from prior runs, then verify the test-owned
        # URL does not appear — scoped to rows this test owns, not global state.
        seed = await _seed_conn(postgres_dsn)
        try:
            await seed.execute("DELETE FROM websites WHERE url LIKE 'https://test-review%'")
        finally:
            await seed.close()

        result = await get_websites_needing_review()
        result_urls = {r["url"] for r in result}
        assert "https://test-review.example.com" not in result_urls

    async def test_with_reviews(
        self, db_conn: asyncpg.Connection, postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agency_audit.loop.qc import get_websites_needing_review

        _point_settings_at_fixture_db(postgres_dsn, monkeypatch)

        seed = await _seed_conn(postgres_dsn)
        try:
            # Insert one website flagged for review, one not
            await seed.execute(
                "INSERT INTO websites (url, label, score, needs_review, review_reason, qc_checks) "
                "VALUES ('https://test-review.example.com', 'Test Review', 0, true, "
                "'Suspicious score 0', '[{\"check\":\"suspicious_score\"}]'::jsonb)"
            )
            await seed.execute(
                "INSERT INTO websites (url, label, score, needs_review) "
                "VALUES ('https://test-ok.example.com', 'Test OK', 85, false)"
            )

            result = await get_websites_needing_review()
            result_urls = {r["url"] for r in result}
            assert "https://test-review.example.com" in result_urls
            assert "https://test-ok.example.com" not in result_urls
            # Verify the flagged row's data is intact
            review_row = next(r for r in result if r["url"] == "https://test-review.example.com")
            assert review_row["score"] == 0
        finally:
            await seed.execute("DELETE FROM websites WHERE url LIKE 'https://test-%'")
            await seed.close()


# ──────────────────────────────────────────────────────────────────────
# Re-audit — non-empty results
# ──────────────────────────────────────────────────────────────────────


class TestReauditWithResults:
    async def test_get_reaudit_queue_with_results(
        self, db_conn: asyncpg.Connection, postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agency_audit.loop.reaudit import get_reaudit_queue

        _point_settings_at_fixture_db(postgres_dsn, monkeypatch)

        seed = await _seed_conn(postgres_dsn)
        try:
            # Insert an audited website with an old last_audited_at date,
            # linked to Sofia (city id=1, country BG from seed fixtures).
            website_id = await seed.fetchval(
                "INSERT INTO websites (url, label, score, audit_status, "
                "needs_review, last_audited_at, audit_attempts) "
                "VALUES ('https://test-old.example.com', 'Old Agency', 50, 'audited', "
                "false, now() - INTERVAL '45 days', 0) RETURNING id"
            )
            await seed.execute(
                "INSERT INTO website_cities (website_id, city_id) VALUES ($1, 1)",
                website_id,
            )

            queue = await get_reaudit_queue(interval_days=30)
            # Scoped: check our website is present, not global cardinality
            queue_ids = [q["id"] for q in queue]
            assert website_id in queue_ids
            our_entry = next(q for q in queue if q["id"] == website_id)
            assert our_entry["age_days"] == 45
        finally:
            await seed.execute(
                "DELETE FROM website_cities WHERE website_id IN "
                "(SELECT id FROM websites WHERE url LIKE 'https://test-%')"
            )
            await seed.execute("DELETE FROM websites WHERE url LIKE 'https://test-%'")
            await seed.close()

    async def test_schedule_reaudits_with_results(
        self, db_conn: asyncpg.Connection, postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agency_audit.loop.reaudit import schedule_reaudits

        _point_settings_at_fixture_db(postgres_dsn, monkeypatch)

        seed = await _seed_conn(postgres_dsn)
        cleaned_log_ids: list[int] = []
        cleaned_city_id: int | None = None
        try:
            # Two BG websites — linked to Sofia (id=1) and Plovdiv (id=2)
            w1 = await seed.fetchval(
                "INSERT INTO websites (url, label, score, audit_status, "
                "needs_review, last_audited_at, audit_attempts) "
                "VALUES ('https://test-a.example.com', 'A', 60, 'audited', "
                "false, now() - INTERVAL '40 days', 0) RETURNING id"
            )
            await seed.execute(
                "INSERT INTO website_cities (website_id, city_id) VALUES ($1, 1)", w1
            )
            w2 = await seed.fetchval(
                "INSERT INTO websites (url, label, score, audit_status, "
                "needs_review, last_audited_at, audit_attempts) "
                "VALUES ('https://test-b.example.com', 'B', 70, 'audited', "
                "false, now() - INTERVAL '50 days', 0) RETURNING id"
            )
            await seed.execute(
                "INSERT INTO website_cities (website_id, city_id) VALUES ($1, 2)", w2
            )

            # Non-BG control — link to a city in Spain (ES) so the country
            # filter in schedule_reaudits(country="BG") actually excludes it.
            es_city_id = await seed.fetchval(
                "INSERT INTO cities (country, label, slug, population, latitude, longitude) "
                "VALUES ('ES', 'Madrid', 'madrid', 3223334, 40.4168, -3.7038) RETURNING id"
            )
            cleaned_city_id = es_city_id
            w3 = await seed.fetchval(
                "INSERT INTO websites (url, label, score, audit_status, "
                "needs_review, last_audited_at, audit_attempts) "
                "VALUES ('https://test-es.example.com', 'ES Agency', 80, 'audited', "
                "false, now() - INTERVAL '60 days', 0) RETURNING id"
            )
            await seed.execute(
                "INSERT INTO website_cities (website_id, city_id) VALUES ($1, $2)",
                w3,
                es_city_id,
            )

            result = await schedule_reaudits(interval_days=30, country="BG")
            assert result["queued"] == 2  # only BG, not ES
            assert result["oldest_age_days"] == 50

            # Verify BG websites were updated to 'pending' with reset attempts
            for wid in (w1, w2):
                row = await db_conn.fetchrow(
                    "SELECT audit_status, audit_attempts FROM websites WHERE id = $1",
                    wid,
                )
                assert row["audit_status"] == "pending"
                assert row["audit_attempts"] == 0

            # ES website must NOT have been touched
            row = await db_conn.fetchrow(
                "SELECT audit_status, audit_attempts FROM websites WHERE id = $1",
                w3,
            )
            assert row["audit_status"] == "audited"
            assert row["audit_attempts"] == 0

            # Verify an audit_log entry was created — capture its ID for cleanup
            log_row = await db_conn.fetchrow(
                "SELECT id, run_type, items_processed, summary FROM audit_log "
                "WHERE summary->>'queued_websites' = '2'"
            )
            assert log_row is not None
            assert log_row["run_type"] == "reaudit"
            assert log_row["items_processed"] == 2
            cleaned_log_ids.append(log_row["id"])
        finally:
            if cleaned_log_ids:
                await seed.execute("DELETE FROM audit_log WHERE id = ANY($1)", cleaned_log_ids)
            await seed.execute(
                "DELETE FROM website_cities WHERE website_id IN "
                "(SELECT id FROM websites WHERE url LIKE 'https://test-%')"
            )
            await seed.execute("DELETE FROM websites WHERE url LIKE 'https://test-%'")
            if cleaned_city_id is not None:
                await seed.execute("DELETE FROM cities WHERE id = $1", cleaned_city_id)
            await seed.close()


# ──────────────────────────────────────────────────────────────────────
# Retry — mark_failed_* functions
# ──────────────────────────────────────────────────────────────────────


class TestRetryMarkFailed:
    async def test_mark_failed_website(
        self, db_conn: asyncpg.Connection, postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agency_audit.loop.retry import mark_failed_website

        _point_settings_at_fixture_db(postgres_dsn, monkeypatch)

        seed = await _seed_conn(postgres_dsn)
        cleaned_log_ids: list[int] = []
        try:
            # Need website + website_cities link to a real city for the
            # audit_log INSERT (JOIN website_cities → cities).
            website_id = await seed.fetchval(
                "INSERT INTO websites (url, label, audit_status, audit_attempts) "
                "VALUES ('https://test-fail.example.com', 'Fail', 'audited', 1) "
                "RETURNING id"
            )
            # Link to Sofia (city id=1 from seed fixtures, country BG)
            await seed.execute(
                "INSERT INTO website_cities (website_id, city_id) VALUES ($1, 1)",
                website_id,
            )

            await mark_failed_website(website_id, "test error")

            # Check website state
            row = await db_conn.fetchrow(
                "SELECT audit_status, audit_last_error, audit_attempts FROM websites WHERE id = $1",
                website_id,
            )
            assert row["audit_status"] == "failed"
            assert row["audit_last_error"] == "test error"
            assert row["audit_attempts"] == 2  # was 1, incremented

            # Check audit_log entry — capture its ID for cleanup
            log_row = await db_conn.fetchrow(
                "SELECT id, run_type, country, items_failed, error FROM audit_log "
                "WHERE error = 'test error'"
            )
            assert log_row is not None
            assert log_row["run_type"] == "audit"
            assert log_row["country"] == "BG"
            assert log_row["items_failed"] == 1
            cleaned_log_ids.append(log_row["id"])
        finally:
            if cleaned_log_ids:
                await seed.execute("DELETE FROM audit_log WHERE id = ANY($1)", cleaned_log_ids)
            await seed.execute(
                "DELETE FROM website_cities WHERE website_id IN "
                "(SELECT id FROM websites WHERE url LIKE 'https://test-%')"
            )
            await seed.execute("DELETE FROM websites WHERE url LIKE 'https://test-%'")
            await seed.close()

    async def test_mark_failed_discovery(
        self, db_conn: asyncpg.Connection, postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agency_audit.loop.retry import mark_failed_discovery

        _point_settings_at_fixture_db(postgres_dsn, monkeypatch)

        seed = await _seed_conn(postgres_dsn)
        try:
            # Use Sofia (city id=1) — reset to pending first, then mark failed
            await seed.execute("UPDATE cities SET discovery_status = 'pending' WHERE id = 1")
            await mark_failed_discovery(1, "discovery error")

            row = await db_conn.fetchrow("SELECT discovery_status FROM cities WHERE id = 1")
            assert row["discovery_status"] == "skipped"

            # Check discovery_log entry
            log_row = await db_conn.fetchrow(
                "SELECT city_id, status, last_error, attempt FROM discovery_log "
                "WHERE city_id = 1 AND last_error = 'discovery error'"
            )
            assert log_row is not None
            assert log_row["status"] == "failed"
            assert log_row["attempt"] == 3
        finally:
            # Restore city state
            await seed.execute("UPDATE cities SET discovery_status = 'pending' WHERE id = 1")
            await seed.execute(
                "DELETE FROM discovery_log WHERE city_id = 1 AND last_error = 'discovery error'"
            )
            await seed.close()

    @pytest.mark.asyncio
    async def test_mark_failed_website_type(self) -> None:
        from agency_audit.loop.retry import mark_failed

        with patch("agency_audit.loop.retry.mark_failed_website") as mock_ws:
            await mark_failed("website", 42, "error")
            mock_ws.assert_called_once_with(42, "error")

    @pytest.mark.asyncio
    async def test_mark_failed_city_type(self) -> None:
        from agency_audit.loop.retry import mark_failed

        with patch("agency_audit.loop.retry.mark_failed_discovery") as mock_city:
            await mark_failed("city", 7, "error")
            mock_city.assert_called_once_with(7, "error")

    @pytest.mark.asyncio
    async def test_mark_failed_unknown_type(self) -> None:
        from agency_audit.loop.retry import mark_failed

        with pytest.raises(ValueError, match="Unknown item_type"):
            await mark_failed("unknown", 1, "error")


# ──────────────────────────────────────────────────────────────────────
# Tracking — log_* functions
# ──────────────────────────────────────────────────────────────────────


class TestTrackingLogFunctions:
    async def test_log_discovery_run(
        self, db_conn: asyncpg.Connection, postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agency_audit.loop.tracking import log_discovery_run

        _point_settings_at_fixture_db(postgres_dsn, monkeypatch)

        seed = await _seed_conn(postgres_dsn)
        log_id: int | None = None
        try:
            log_id = await log_discovery_run("BG", 5, 12, 2.5)
            assert isinstance(log_id, int)
            assert log_id > 0

            row = await db_conn.fetchrow(
                "SELECT country, run_type, duration_seconds, items_processed, "
                "items_succeeded, items_failed, summary FROM audit_log WHERE id = $1",
                log_id,
            )
            assert row["country"] == "BG"
            assert row["run_type"] == "discovery"
            assert float(row["duration_seconds"]) == 2.5
            assert row["items_processed"] == 5
            assert row["items_succeeded"] == 12
            assert row["items_failed"] == 0
            summary = json.loads(row["summary"])
            assert summary["cities_processed"] == 5
            assert summary["agencies_found"] == 12
        finally:
            if log_id is not None:
                await seed.execute("DELETE FROM audit_log WHERE id = $1", log_id)
            await seed.close()

    async def test_log_discovery_run_with_errors(
        self, db_conn: asyncpg.Connection, postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agency_audit.loop.tracking import log_discovery_run

        _point_settings_at_fixture_db(postgres_dsn, monkeypatch)

        seed = await _seed_conn(postgres_dsn)
        log_id: int | None = None
        try:
            log_id = await log_discovery_run("BG", 3, 5, 1.0, errors=["err1", "err2"])
            assert isinstance(log_id, int)
            assert log_id > 0

            row = await db_conn.fetchrow(
                "SELECT items_failed, summary FROM audit_log WHERE id = $1",
                log_id,
            )
            assert row["items_failed"] == 2
            summary = json.loads(row["summary"])
            assert summary["errors"] == ["err1", "err2"]
        finally:
            if log_id is not None:
                await seed.execute("DELETE FROM audit_log WHERE id = $1", log_id)
            await seed.close()

    async def test_log_audit_run(
        self, db_conn: asyncpg.Connection, postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agency_audit.loop.tracking import log_audit_run

        _point_settings_at_fixture_db(postgres_dsn, monkeypatch)

        seed = await _seed_conn(postgres_dsn)
        log_id: int | None = None
        try:
            # Need website + website_cities → city so country can be resolved
            website_id = await seed.fetchval(
                "INSERT INTO websites (url, label) "
                "VALUES ('https://test-audit.example.com', 'Test Audit') RETURNING id"
            )
            await seed.execute(
                "INSERT INTO website_cities (website_id, city_id) VALUES ($1, 1)",
                website_id,
            )

            log_id = await log_audit_run(website_id, 75, 1.5, success=True)
            assert isinstance(log_id, int)
            assert log_id > 0

            row = await db_conn.fetchrow(
                "SELECT country, run_type, items_succeeded, items_failed, summary "
                "FROM audit_log WHERE id = $1",
                log_id,
            )
            assert row["country"] == "BG"
            assert row["run_type"] == "audit"
            assert row["items_succeeded"] == 1
            assert row["items_failed"] == 0
            summary = json.loads(row["summary"])
            assert summary["website_id"] == website_id
            assert summary["score"] == 75
        finally:
            if log_id is not None:
                await seed.execute("DELETE FROM audit_log WHERE id = $1", log_id)
            await seed.execute(
                "DELETE FROM website_cities WHERE website_id IN "
                "(SELECT id FROM websites WHERE url LIKE 'https://test-%')"
            )
            await seed.execute("DELETE FROM websites WHERE url LIKE 'https://test-%'")
            await seed.close()

    async def test_log_audit_run_with_country(
        self, db_conn: asyncpg.Connection, postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agency_audit.loop.tracking import log_audit_run

        _point_settings_at_fixture_db(postgres_dsn, monkeypatch)

        seed = await _seed_conn(postgres_dsn)
        log_id: int | None = None
        try:
            website_id = await seed.fetchval(
                "INSERT INTO websites (url, label) "
                "VALUES ('https://test-audit2.example.com', 'Test Audit 2') RETURNING id"
            )
            await seed.execute(
                "INSERT INTO website_cities (website_id, city_id) VALUES ($1, 1)",
                website_id,
            )

            log_id = await log_audit_run(
                website_id, 80, 2.0, country="BG", success=False, error="timeout"
            )
            assert isinstance(log_id, int)
            assert log_id > 0

            row = await db_conn.fetchrow(
                "SELECT country, items_succeeded, items_failed, summary, error "
                "FROM audit_log WHERE id = $1",
                log_id,
            )
            assert row["country"] == "BG"
            assert row["items_succeeded"] == 0
            assert row["items_failed"] == 1
            assert row["error"] == "timeout"
            summary = json.loads(row["summary"])
            assert summary["score"] == 80
        finally:
            if log_id is not None:
                await seed.execute("DELETE FROM audit_log WHERE id = $1", log_id)
            await seed.execute(
                "DELETE FROM website_cities WHERE website_id IN "
                "(SELECT id FROM websites WHERE url LIKE 'https://test-%')"
            )
            await seed.execute("DELETE FROM websites WHERE url LIKE 'https://test-%'")
            await seed.close()

    async def test_log_full_loop_run(
        self, db_conn: asyncpg.Connection, postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agency_audit.loop.tracking import log_full_loop_run

        _point_settings_at_fixture_db(postgres_dsn, monkeypatch)

        seed = await _seed_conn(postgres_dsn)
        log_id: int | None = None
        try:
            log_id = await log_full_loop_run("BG", 10, 25, 20, 18, 2, 3, 5, 30.5)
            assert isinstance(log_id, int)
            assert log_id > 0

            row = await db_conn.fetchrow(
                "SELECT country, run_type, duration_seconds, items_processed, "
                "items_succeeded, items_failed, summary FROM audit_log WHERE id = $1",
                log_id,
            )
            assert row["country"] == "BG"
            assert row["run_type"] == "full_loop"
            assert float(row["duration_seconds"]) == 30.5
            assert row["items_processed"] == 30  # cities + websites
            assert row["items_succeeded"] == 43  # agencies + succeeded audits
            assert row["items_failed"] == 2
            summary = json.loads(row["summary"])
            assert summary["qc_findings"] == 3
            assert summary["reaudit_queued"] == 5
        finally:
            if log_id is not None:
                await seed.execute("DELETE FROM audit_log WHERE id = $1", log_id)
            await seed.close()


# ──────────────────────────────────────────────────────────────────────
# RetryConfig
# ──────────────────────────────────────────────────────────────────────


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


# ──────────────────────────────────────────────────────────────────────
# Re-audit — helpers
# ──────────────────────────────────────────────────────────────────────


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
