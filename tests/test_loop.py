"""Tests for the operational loop module.

Tests cover: QC checks, re-audit scheduling, retry logic, progress tracking,
and orchestrator integration.

Database-backed tests run against the real PostgreSQL database via the shared
``db_conn`` fixture from ``tests/conftest.py``.  Non-database mocks (retry,
audit) are kept as-is.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from agency_audit.db import close_pool, get_pool

# ──────────────────────────────────────────────────────────────────────
# Test data helpers
# ──────────────────────────────────────────────────────────────────────

TEST_URL_PREFIX = "https://test-loop.example.com/"


async def _seed_website(url_suffix: str = "001", **overrides) -> dict:
    """Insert a test website linked to the first BG city and return its row.

    Uses the pool so the row is committed and visible to functions that call
    ``get_pool()`` internally.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        url = f"{TEST_URL_PREFIX}{url_suffix}"
        defaults = {
            "url": url,
            "score": 0,
            "audit_status": "pending",
            "audit_attempts": 0,
        }
        defaults.update(overrides)
        defaults["url"] = url  # URL always has the prefix

        website_id = await conn.fetchval(
            """INSERT INTO websites (url, label, score, audit_status,
                                     audit_attempts, audit_last_error,
                                     needs_review, review_reason,
                                     last_audited_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
               ON CONFLICT (url) DO UPDATE
               SET score = $3, audit_status = $4,
                   audit_attempts = $5, audit_last_error = $6,
                   needs_review = $7, review_reason = $8,
                   last_audited_at = $9
               RETURNING id""",
            defaults["url"],
            defaults.get("label", f"Test Agency {url_suffix}"),
            defaults["score"],
            defaults["audit_status"],
            defaults["audit_attempts"],
            defaults.get("audit_last_error"),
            defaults.get("needs_review", False),
            defaults.get("review_reason"),
            defaults.get("last_audited_at"),
        )

        # Link to the first BG city (Sofia, id=1 from seed fixtures)
        city_id = await conn.fetchval(
            "SELECT id FROM cities WHERE country = 'BG' ORDER BY id LIMIT 1"
        )
        await conn.execute(
            """INSERT INTO website_cities (website_id, city_id)
               VALUES ($1, $2)
               ON CONFLICT (website_id, city_id) DO NOTHING""",
            website_id,
            city_id,
        )

        return {"id": website_id, "url": url, "city_id": city_id}


@pytest.fixture(autouse=True)
async def _cleanup_loop_data():
    """Ensure the DB pool is fresh and test data is cleaned up.

    Runs before *and* after every test in this module so pool-committed
    writes from one test never leak into the next.

    Uses the pool for cleanup operations so DELETEs are committed —
    operations on ``db_conn`` would be rolled back by the conftest
    fixture, making cleanup invisible to the next test's pool.
    """
    # Close any previously-opened pool so the module-level pool singleton
    # reconnects fresh for this test (important when AGENCY_AUDIT_PG_* env
    # vars were set after a previous pool was created).
    await close_pool()

    # Pre-test cleanup via pool (committed, visible to function under test)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM website_cities WHERE website_id IN "
            "(SELECT id FROM websites WHERE url LIKE $1)",
            TEST_URL_PREFIX + "%",
        )
        await conn.execute(
            "DELETE FROM audit_log WHERE error LIKE 'test%' OR summary::text LIKE '%test-loop%'"
        )
        await conn.execute("DELETE FROM websites WHERE url LIKE $1", TEST_URL_PREFIX + "%")

    yield

    # Post-test cleanup via pool (committed)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM website_cities WHERE website_id IN "
            "(SELECT id FROM websites WHERE url LIKE $1)",
            TEST_URL_PREFIX + "%",
        )
        await conn.execute(
            "DELETE FROM audit_log WHERE error LIKE 'test%' OR summary::text LIKE '%test-loop%'"
        )
        await conn.execute("DELETE FROM websites WHERE url LIKE $1", TEST_URL_PREFIX + "%")
    await close_pool()


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

    async def test_mark_failed_website_updates_status(self, db_conn: asyncpg.Connection):
        """mark_failed_website should update website status to 'failed' in real DB."""
        from agency_audit.loop.retry import mark_failed_website

        ws = await _seed_website("mark-fail", audit_attempts=1, audit_last_error=None)

        await mark_failed_website(ws["id"], "test error message")

        row = await db_conn.fetchrow(
            "SELECT audit_status, audit_last_error, audit_attempts FROM websites WHERE id = $1",
            ws["id"],
        )
        assert row["audit_status"] == "failed"
        assert row["audit_last_error"] == "test error message"
        # audit_attempts was 1, should now be 2 (incremented)
        assert row["audit_attempts"] == 2

    async def test_mark_failed_website_audit_log_joins_cities(self, db_conn: asyncpg.Connection):
        """audit_log INSERT must resolve country via cities JOIN, not website_cities."""
        from agency_audit.loop.retry import mark_failed_website

        ws = await _seed_website("audit-log-join")

        await mark_failed_website(ws["id"], "test network timeout")

        # The audit_log row should have country resolved via JOIN through cities
        log_row = await db_conn.fetchrow(
            "SELECT country, run_type, error FROM audit_log "
            "WHERE error = $1 ORDER BY id DESC LIMIT 1",
            "test network timeout",
        )
        assert log_row is not None, "Expected an audit_log row to be inserted"
        assert log_row["country"] == "BG", (
            f"Country should be 'BG' (resolved via cities JOIN), got: {log_row['country']}"
        )
        assert log_row["run_type"] == "audit"


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

    async def test_flag_suspicious_scores_empty(self, db_conn: asyncpg.Connection):
        """flag_suspicious_scores should handle empty database gracefully."""
        from agency_audit.loop.qc import flag_suspicious_scores

        findings = await flag_suspicious_scores()
        assert findings == []

    async def test_flag_suspicious_scores_found(self, db_conn: asyncpg.Connection):
        """flag_suspicious_scores should detect scores of 0 and 100."""
        from agency_audit.loop.qc import flag_suspicious_scores

        # Seed two websites with suspicious scores
        ws_zero = await _seed_website("zero", score=0, audit_status="audited")
        ws_hundred = await _seed_website("hundred", score=100, audit_status="audited")

        findings = await flag_suspicious_scores()

        assert len(findings) == 2
        finding_ids = {f.website_id for f in findings}
        assert ws_zero["id"] in finding_ids
        assert ws_hundred["id"] in finding_ids

        # Verify DB was updated
        zero_row = await db_conn.fetchrow(
            "SELECT needs_review, review_reason, qc_checks FROM websites WHERE id = $1",
            ws_zero["id"],
        )
        assert zero_row["needs_review"] is True
        assert "score 0" in zero_row["review_reason"].lower()

        hundred_row = await db_conn.fetchrow(
            "SELECT needs_review, review_reason, qc_checks FROM websites WHERE id = $1",
            ws_hundred["id"],
        )
        assert hundred_row["needs_review"] is True
        assert "score 100" in hundred_row["review_reason"].lower()

    async def test_detect_duplicates_empty(self, db_conn: asyncpg.Connection):
        """detect_duplicates should handle empty results."""
        from agency_audit.loop.qc import detect_duplicates

        findings = await detect_duplicates()
        assert findings == []

    async def test_detect_duplicates_found(self, db_conn: asyncpg.Connection):
        """detect_duplicates should flag a domain appearing in multiple cities."""
        from agency_audit.loop.qc import detect_duplicates

        # Create one website linked to two different BG cities (Sofia + Plovdiv)
        pool = await get_pool()
        async with pool.acquire() as conn:
            url = f"{TEST_URL_PREFIX}duplicate-agency"
            wid = await conn.fetchval(
                """INSERT INTO websites (url, label, audit_status)
                   VALUES ($1, 'Dup Agency', 'audited')
                   ON CONFLICT (url) DO UPDATE SET audit_status = 'audited'
                   RETURNING id""",
                url,
            )
            # Link to Sofia (city_id=1) and Plovdiv (city_id=2 from seed)
            await conn.execute(
                """INSERT INTO website_cities (website_id, city_id)
                   VALUES ($1, 1), ($1, 2)
                   ON CONFLICT (website_id, city_id) DO NOTHING""",
                wid,
            )

        findings = await detect_duplicates()

        assert len(findings) >= 1
        # At least one finding for our duplicate-domain website
        duplicate_findings = [f for f in findings if f.url == url]
        assert len(duplicate_findings) == 1, f"Expected finding for {url}"
        assert duplicate_findings[0].severity == "info"
        assert "2 cities" in duplicate_findings[0].reason

        # Verify DB was updated with needs_review
        row = await db_conn.fetchrow(
            "SELECT needs_review, review_reason FROM websites WHERE id = $1", wid
        )
        assert row["needs_review"] is True

    async def test_detect_duplicates_skips_single_city(self, db_conn: asyncpg.Connection):
        """A website in only one city should NOT be flagged as duplicate."""
        from agency_audit.loop.qc import detect_duplicates

        # Single-city website (only Sofia)
        await _seed_website("single-city", audit_status="audited")

        findings = await detect_duplicates()

        # None of the findings should be for a single-city website
        single_city_findings = [f for f in findings if f.url == f"{TEST_URL_PREFIX}single-city"]
        assert len(single_city_findings) == 0


# ──────────────────────────────────────────────────────────────────────
# Re-audit tests
# ──────────────────────────────────────────────────────────────────────


class TestReaudit:
    """Tests for re-audit scheduling."""

    async def test_get_reaudit_queue_empty(self, db_conn: asyncpg.Connection):
        """get_reaudit_queue should return empty when no overdue websites."""
        from agency_audit.loop.reaudit import get_reaudit_queue

        queue = await get_reaudit_queue()
        assert queue == []

    async def test_schedule_reaudits_empty(self, db_conn: asyncpg.Connection):
        """schedule_reaudits should return zero when nothing to queue."""
        from agency_audit.loop.reaudit import schedule_reaudits

        result = await schedule_reaudits()
        assert result["queued"] == 0

    async def test_schedule_reaudits_queues_overdue(self, db_conn: asyncpg.Connection):
        """schedule_reaudits should queue a website audited long ago."""
        from datetime import UTC, datetime, timedelta

        from agency_audit.loop.reaudit import schedule_reaudits

        # Seed a website that was audited 45 days ago
        old_date = datetime.now(UTC) - timedelta(days=45)
        ws = await _seed_website(
            "overdue",
            audit_status="audited",
            score=75,
            last_audited_at=old_date,
        )

        result = await schedule_reaudits(interval_days=30, limit=10, country="BG")

        assert result["queued"] == 1

        # Verify the website was reset to pending with audit_attempts=0
        row = await db_conn.fetchrow(
            "SELECT audit_status, audit_attempts, last_audited_at FROM websites WHERE id = $1",
            ws["id"],
        )
        assert row["audit_status"] == "pending"
        assert row["audit_attempts"] == 0
        assert row["last_audited_at"] is None

    async def test_reaudit_scheduling_resets_attempts_to_zero(self, db_conn: asyncpg.Connection):
        """Re-audit scheduling should reset audit_attempts to 0, not increment."""
        from datetime import UTC, datetime, timedelta

        from agency_audit.loop.reaudit import schedule_reaudits

        # Website with some prior failed attempts, audited 45 days ago
        old_date = datetime.now(UTC) - timedelta(days=45)
        ws = await _seed_website(
            "reset-attempts",
            audit_status="audited",
            score=75,
            audit_attempts=2,
            last_audited_at=old_date,
        )

        result = await schedule_reaudits(interval_days=30, limit=10, country="BG")

        assert result["queued"] == 1

        row = await db_conn.fetchrow(
            "SELECT audit_status, audit_attempts FROM websites WHERE id = $1",
            ws["id"],
        )
        assert row["audit_status"] == "pending"
        assert row["audit_attempts"] == 0, "re-audit scheduling should reset audit_attempts to 0"


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

    async def test_get_progress_empty_db(self, db_conn: asyncpg.Connection):
        """get_progress should return a well-structured result.

        The database is pre-seeded with 44 countries and 20 cities from
        fixtures, so city counts are non-zero.  We assert on the structure
        and on counters that start at zero (websites).
        """
        from agency_audit.loop.tracking import get_progress

        data = await get_progress()

        assert "overview" in data
        assert "per_country" in data
        assert "recent_runs" in data

        overview = data["overview"]
        assert overview["countries"] > 0  # pre-seeded
        assert overview["cities_total"] > 0  # pre-seeded
        assert overview["websites_total"] == 0  # no websites seeded
        assert overview["websites_audited"] == 0
        assert overview["websites_failed"] == 0

    async def test_log_discovery_run_inserts_row(self, db_conn: asyncpg.Connection):
        """log_discovery_run should insert an audit_log row with correct values."""
        from agency_audit.loop.tracking import log_discovery_run

        log_id = await log_discovery_run(
            country="BG",
            cities_processed=5,
            agencies_found=12,
            duration_seconds=3.5,
        )

        row = await db_conn.fetchrow(
            "SELECT country, run_type, items_processed, items_succeeded, "
            "duration_seconds, summary FROM audit_log WHERE id = $1",
            log_id,
        )
        assert row["country"] == "BG"
        assert row["run_type"] == "discovery"
        assert row["items_processed"] == 5
        assert row["items_succeeded"] == 12
        assert float(row["duration_seconds"]) == 3.5


# ──────────────────────────────────────────────────────────────────────
# Orchestrator import / formatting tests
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

    async def test_successful_audit_resets_attempts_to_zero(self, db_conn: asyncpg.Connection):
        """On audit success, the UPDATE must set audit_attempts = 0."""
        from agency_audit.loop.orchestrator import _audit_country_websites

        # Seed a pending website with prior attempts
        ws = await _seed_website("success-reset", audit_status="pending", audit_attempts=2)

        # Mock retry to return a fake successful audit result
        class FakeAuditResult:
            score = 85

            @staticmethod
            def to_dict():
                return {"score": 85}

        with patch("agency_audit.loop.orchestrator.retry", new_callable=AsyncMock) as mock_retry:
            mock_retry.return_value = FakeAuditResult()

            result = await _audit_country_websites("BG", concurrency=1)

        assert result["succeeded"] == 1
        assert result["failed"] == 0

        row = await db_conn.fetchrow(
            "SELECT audit_status, audit_attempts, score FROM websites WHERE id = $1",
            ws["id"],
        )
        assert row["audit_status"] == "audited"
        assert row["audit_attempts"] == 0, "successful audit should reset audit_attempts to 0"
        assert row["score"] == 85

    async def test_failed_audit_increments_attempts(self, db_conn: asyncpg.Connection):
        """On audit failure, the UPDATE must increment audit_attempts."""
        from agency_audit.loop.orchestrator import _audit_country_websites

        ws = await _seed_website("fail-increment", audit_status="pending", audit_attempts=1)

        with patch("agency_audit.loop.orchestrator.retry", new_callable=AsyncMock) as mock_retry:
            mock_retry.side_effect = RuntimeError("audit failed after retries")

            result = await _audit_country_websites("BG", concurrency=1)

        assert result["failed"] == 1
        assert result["succeeded"] == 0

        row = await db_conn.fetchrow(
            "SELECT audit_status, audit_attempts, audit_last_error FROM websites WHERE id = $1",
            ws["id"],
        )
        assert row["audit_status"] == "failed"
        # Was 1, should now be 2 (incremented)
        assert row["audit_attempts"] == 2, "failed audit should increment audit_attempts"
        assert "audit failed after retries" in (row["audit_last_error"] or "")


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
