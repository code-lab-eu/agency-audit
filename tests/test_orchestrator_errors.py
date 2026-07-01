"""Additional tests for orchestrator error paths. Push coverage further."""

from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import pytest

from agency_audit.config import settings
from agency_audit.db import close_pool


@pytest.fixture(autouse=True)
async def _cleanup_pool():
    """Ensure the module-level pool is closed after every test.

    get_pool() is a module-level singleton; when one test creates a pool on its
    event loop, that pool becomes stale as soon as the test's loop closes.
    Closing it here guarantees the next test always gets a fresh pool.
    """
    yield
    await close_pool()


class TestOrchestratorErrorPaths:
    """Test error-handling paths in run_country for each phase.

    These are error-injection tests: they mock phase functions to raise
    and verify the orchestrator catches and records the errors.  get_pool
    is mocked here because log_full_loop_run uses it; the mock is genuine
    error-path scaffolding, not a substitute for SQL correctness checks.
    """

    @pytest.mark.asyncio
    async def test_discovery_phase_error(self):
        """Error in discovery phase should be caught and recorded."""
        from agency_audit.loop.orchestrator import run_country

        with (
            patch(
                "agency_audit.loop.orchestrator.get_pool"
            ) as mock_get_pool,  # db-mock-check: ignore
            patch("agency_audit.loop.orchestrator.log_full_loop_run") as mock_log,
            patch("agency_audit.loop.orchestrator.DiscoveryPipeline") as mock_dp_cls,
        ):
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool
            mock_log.return_value = 1

            # Make DiscoveryPipeline().run_for_countries raise
            mock_dp = MagicMock()
            mock_dp.run_for_countries = AsyncMock(side_effect=RuntimeError("maps api error"))
            mock_dp.close = AsyncMock()
            mock_dp_cls.return_value = mock_dp

            result = await run_country(
                "bg",
                skip_discovery=False,
                skip_audit=True,
                skip_qc=True,
                skip_reaudit=True,
            )
            assert result["country"] == "BG"
            assert "discovery" in result["phases"]
            assert "error" in result["phases"]["discovery"]
            assert any("discovery" in err for err in result["errors"])
            mock_dp_cls.assert_called_once()

    @pytest.mark.asyncio
    async def test_audit_phase_error(self):
        """Error in audit phase should be caught and recorded."""
        from agency_audit.loop.orchestrator import run_country

        with (
            patch(
                "agency_audit.loop.orchestrator.get_pool"
            ) as mock_get_pool,  # db-mock-check: ignore
            patch("agency_audit.loop.orchestrator.log_full_loop_run") as mock_log,
            patch("agency_audit.loop.orchestrator._audit_country_websites") as mock_audit,
        ):
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool
            mock_log.return_value = 1
            mock_audit.side_effect = RuntimeError("audit db error")

            result = await run_country(
                "bg",
                skip_discovery=True,
                skip_audit=False,
                skip_qc=True,
                skip_reaudit=True,
            )
            assert "audit" in result["phases"]
            assert "error" in result["phases"]["audit"]
            assert any("audit" in err for err in result["errors"])

    @pytest.mark.asyncio
    async def test_qc_phase_error(self):
        """Error in QC phase should be caught and recorded."""
        from agency_audit.loop.orchestrator import run_country

        with (
            patch(
                "agency_audit.loop.orchestrator.get_pool"
            ) as mock_get_pool,  # db-mock-check: ignore
            patch("agency_audit.loop.orchestrator.log_full_loop_run") as mock_log,
            patch("agency_audit.loop.orchestrator.run_qc_checks") as mock_qc,
        ):
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool
            mock_log.return_value = 1
            mock_qc.side_effect = RuntimeError("qc db error")

            result = await run_country(
                "bg",
                skip_discovery=True,
                skip_audit=True,
                skip_qc=False,
                skip_reaudit=True,
            )
            assert "qc" in result["phases"]
            assert "error" in result["phases"]["qc"]
            assert any("qc" in err for err in result["errors"])

    @pytest.mark.asyncio
    async def test_reaudit_phase_error(self):
        """Error in reaudit phase should be caught and recorded."""
        from agency_audit.loop.orchestrator import run_country

        with (
            patch(
                "agency_audit.loop.orchestrator.get_pool"
            ) as mock_get_pool,  # db-mock-check: ignore
            patch("agency_audit.loop.orchestrator.log_full_loop_run") as mock_log,
            patch("agency_audit.loop.orchestrator.schedule_reaudits") as mock_reaudit,
        ):
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool
            mock_log.return_value = 1
            mock_reaudit.side_effect = RuntimeError("reaudit db error")

            result = await run_country(
                "bg",
                skip_discovery=True,
                skip_audit=True,
                skip_qc=True,
                skip_reaudit=False,
            )
            assert "reaudit" in result["phases"]
            assert "error" in result["phases"]["reaudit"]
            assert any("reaudit" in err for err in result["errors"])


class TestOrchestratorFormatSummaryEdgeCases:
    """Edge cases for _format_summary and _format_totals."""

    def test_format_summary_with_errors(self):
        from agency_audit.loop.orchestrator import _format_summary

        result = {
            "phases": {},
            "errors": ["err1", "err2"],
        }
        s = _format_summary(result)
        assert "errors:2" in s

    def test_format_summary_empty(self):
        from agency_audit.loop.orchestrator import _format_summary

        result = {
            "phases": {},
            "errors": [],
        }
        s = _format_summary(result)
        assert s == ""


# ──────────────────────────────────────────────────────────────────────
# Orchestrator happy-path phase success tests
# ──────────────────────────────────────────────────────────────────────


class TestOrchestratorHappyPaths:
    """Test the success logging paths in each phase of run_country.

    These tests mock phase functions and verify the orchestrator's
    control-flow / aggregation logic.  get_pool is mocked because the
    only real consumer is log_full_loop_run, which is also mocked here.
    """

    @pytest.mark.asyncio
    async def test_run_all_countries_default_countries(
        self,
        db_conn,
        postgres_dsn: str,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """run_all_countries without countries list fetches active countries from DB.

        Query-path test: exercises the real database via get_pool() so the
        ``SELECT iso FROM countries WHERE active = true`` query runs against
        PostgreSQL.  run_country is still mocked — we are testing the
        country-fetch path, not the full loop.

        Monkeypatches agency_audit.config.settings so get_pool() connects
        to the same isolated test database that the db_conn fixture uses.
        Assertions are dynamic — they first query the active countries via
        db_conn, then verify run_all_countries returns the same set.
        """
        from agency_audit.loop.orchestrator import run_all_countries

        # Discover what the fixture database actually contains via db_conn.
        # This makes the test independent of ambient database state and
        # works whether the fixture is a Docker container or the same
        # host as the developer's local instance.
        rows = await db_conn.fetch("SELECT iso FROM countries WHERE active = true ORDER BY iso")
        expected_isos = [r["iso"] for r in rows]

        # Point get_pool() at the fixture database so every consumer
        # (run_all_countries → get_pool → asyncpg.create_pool) hits
        # the same isolated, migrated, seeded database as db_conn.
        parsed = urlparse(postgres_dsn)
        monkeypatch.setattr(settings, "pg_host", parsed.hostname or "localhost")
        monkeypatch.setattr(settings, "pg_port", parsed.port or 5432)
        monkeypatch.setattr(settings, "pg_database", (parsed.path or "/agency_audit").lstrip("/"))
        monkeypatch.setattr(settings, "pg_user", parsed.username or "agency_audit")
        monkeypatch.setattr(settings, "pg_password", parsed.password or "")

        with patch("agency_audit.loop.orchestrator.run_country") as mock_run:
            mock_run.return_value = {
                "country": "BG",
                "phases": {},
                "errors": [],
                "duration_seconds": 0.01,
            }

            result = await run_all_countries()

            assert result["totals"]["countries_processed"] == len(expected_isos)
            assert mock_run.call_count == len(expected_isos)
            # Verify the exact set of active countries was processed
            assert set(result["results"].keys()) == set(expected_isos)

    @pytest.mark.asyncio
    async def test_discovery_phase_success(self):
        """Discovery success should log discovery run."""
        from agency_audit.loop.orchestrator import run_country

        with (
            patch("agency_audit.loop.orchestrator.get_pool"),  # db-mock-check: ignore
            patch("agency_audit.loop.orchestrator.DiscoveryPipeline") as mock_dp_cls,
            patch("agency_audit.loop.orchestrator.log_discovery_run") as mock_log,
            patch("agency_audit.loop.orchestrator.log_full_loop_run"),
        ):
            mock_dp = MagicMock()
            mock_dp.run_for_countries = AsyncMock(
                return_value={
                    "cities_processed": 5,
                    "agencies_found": 12,
                    "countries_processed": 1,
                    "results": {},
                }
            )
            mock_dp.close = AsyncMock()
            mock_dp_cls.return_value = mock_dp

            result = await run_country(
                "bg",
                skip_discovery=False,
                skip_audit=True,
                skip_qc=True,
                skip_reaudit=True,
            )
            assert result["country"] == "BG"
            assert "discovery" in result["phases"]
            assert "error" not in result["phases"]["discovery"]
            mock_log.assert_called_once()

    @pytest.mark.asyncio
    async def test_audit_phase_success(self):
        """Audit success should not log an error."""
        from agency_audit.loop.orchestrator import run_country

        with (
            patch("agency_audit.loop.orchestrator.get_pool"),  # db-mock-check: ignore
            patch("agency_audit.loop.orchestrator._audit_country_websites") as mock_audit,
            patch("agency_audit.loop.orchestrator.log_full_loop_run"),
        ):
            mock_audit.return_value = {
                "audited": 10,
                "succeeded": 8,
                "failed": 2,
            }

            result = await run_country(
                "bg",
                skip_discovery=True,
                skip_audit=False,
                skip_qc=True,
                skip_reaudit=True,
            )
            assert "audit" in result["phases"]
            assert "error" not in result["phases"]["audit"]
            assert result["phases"]["audit"]["audits_succeeded"] == 8

    @pytest.mark.asyncio
    async def test_qc_phase_success(self):
        """QC success should record findings."""
        from agency_audit.loop.orchestrator import run_country

        with (
            patch("agency_audit.loop.orchestrator.get_pool"),  # db-mock-check: ignore
            patch("agency_audit.loop.orchestrator.run_qc_checks") as mock_qc,
            patch("agency_audit.loop.orchestrator.log_full_loop_run"),
        ):
            mock_qc.return_value = {
                "suspicious_scores": 2,
                "duplicate_domains": 1,
                "total_findings": 3,
            }

            result = await run_country(
                "bg",
                skip_discovery=True,
                skip_audit=True,
                skip_qc=False,
                skip_reaudit=True,
            )
            assert "qc" in result["phases"]
            assert "error" not in result["phases"]["qc"]
            assert result["phases"]["qc"]["findings"] == 3

    @pytest.mark.asyncio
    async def test_reaudit_phase_success(self):
        """Reaudit success should record queue count."""
        from agency_audit.loop.orchestrator import run_country

        with (
            patch("agency_audit.loop.orchestrator.get_pool"),  # db-mock-check: ignore
            patch("agency_audit.loop.orchestrator.schedule_reaudits") as mock_reaudit,
            patch("agency_audit.loop.orchestrator.log_full_loop_run"),
        ):
            mock_reaudit.return_value = {
                "queued": 8,
                "oldest_age_days": 45,
            }

            result = await run_country(
                "bg",
                skip_discovery=True,
                skip_audit=True,
                skip_qc=True,
                skip_reaudit=False,
            )
            assert "reaudit" in result["phases"]
            assert "error" not in result["phases"]["reaudit"]
            assert result["phases"]["reaudit"]["queued"] == 8


# ──────────────────────────────────────────────────────────────────────
# Orchestrator: run_country with all phases skipped
# ──────────────────────────────────────────────────────────────────────


class TestOrchestratorSkipPaths:
    """run_country with all skip flags should still log and return."""

    @pytest.mark.asyncio
    async def test_run_country_all_phases_skipped(self):
        from agency_audit.loop.orchestrator import run_country

        with (
            patch(
                "agency_audit.loop.orchestrator.get_pool"
            ) as mock_get_pool,  # db-mock-check: ignore
            patch("agency_audit.loop.orchestrator.log_full_loop_run") as mock_log,
        ):
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool
            mock_log.return_value = 1

            result = await run_country(
                "bg",
                skip_discovery=True,
                skip_audit=True,
                skip_qc=True,
                skip_reaudit=True,
            )
            assert result["country"] == "BG"
            assert result["phases"] == {}
            mock_log.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_country_log_full_loop_error_handled(self):
        """When log_full_loop_run fails, it should be caught and not crash."""
        from agency_audit.loop.orchestrator import run_country

        with (
            patch(
                "agency_audit.loop.orchestrator.get_pool"
            ) as mock_get_pool,  # db-mock-check: ignore
            patch("agency_audit.loop.orchestrator.log_full_loop_run") as mock_log,
        ):
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool
            mock_log.side_effect = RuntimeError("db error")

            result = await run_country(
                "bg",
                skip_discovery=True,
                skip_audit=True,
                skip_qc=True,
                skip_reaudit=True,
            )
            assert result["country"] == "BG"

    @pytest.mark.asyncio
    async def test_run_all_countries_with_countries_list(self):
        """run_all_countries with explicit countries list, all skipped.

        When a countries list is supplied the orchestrator uses it directly
        without fetching from the database.  get_pool is still called
        (pool.acquire happens unconditionally) but the connection is
        released immediately.  Mocking get_pool here is appropriate because
        the test exercises control flow, not a SQL query path.
        """
        from agency_audit.loop.orchestrator import run_all_countries

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_ctx

        with (
            patch(
                "agency_audit.loop.orchestrator.get_pool",
                return_value=mock_pool,
            ),  # db-mock-check: ignore
            patch("agency_audit.loop.orchestrator.run_country") as mock_run,
        ):
            mock_run.return_value = {
                "country": "BG",
                "phases": {},
                "errors": [],
                "duration_seconds": 0.01,
            }

            result = await run_all_countries(countries=["BG"])
            assert "results" in result
            assert "totals" in result
            assert result["totals"]["countries_processed"] == 1
            mock_run.assert_called_once_with(
                country_iso="BG",
                max_cities=None,
                audit_concurrency=3,
                reaudit_interval_days=30,
                reaudit_limit=100,
            )

    @pytest.mark.asyncio
    async def test_run_all_countries_error_handling(self):
        """When one country errors, run_all_countries should continue and record error."""
        from agency_audit.loop.orchestrator import run_all_countries

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_ctx

        with (
            patch(
                "agency_audit.loop.orchestrator.get_pool",
                return_value=mock_pool,
            ),  # db-mock-check: ignore
            patch("agency_audit.loop.orchestrator.run_country") as mock_run,
        ):
            mock_run.side_effect = RuntimeError("boom")

            result = await run_all_countries(countries=["BG"])
            assert "BG" in result["results"]
            assert "error" in result["results"]["BG"]
            assert len(result["totals"]["errors"]) == 1

    @pytest.mark.asyncio
    async def test_audit_country_websites_empty(
        self,
        db_conn,
        postgres_dsn: str,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """_audit_country_websites reflects the actual pending-website count.

        Query-path test: exercises the real database via get_pool().  The
        test first queries the pending BG website count via db_conn, then
        asserts _audit_country_websites returns the same count.  The
        ``audit_website`` call is mocked to avoid real HTTP requests and
        prevent database writes outside the db_conn transaction.
        """
        from agency_audit.loop.orchestrator import _audit_country_websites

        # Discover the current pending BG website count via db_conn.
        pending_count = await db_conn.fetchval(
            """SELECT COUNT(*)
               FROM websites w
               JOIN website_cities wc ON wc.website_id = w.id
               JOIN cities c ON c.id = wc.city_id
               WHERE c.country = 'BG' AND w.audit_status = 'pending'
                 AND w.audit_attempts < 3"""
        )

        # Point get_pool() at the fixture database.
        parsed = urlparse(postgres_dsn)
        monkeypatch.setattr(settings, "pg_host", parsed.hostname or "localhost")
        monkeypatch.setattr(settings, "pg_port", parsed.port or 5432)
        monkeypatch.setattr(settings, "pg_database", (parsed.path or "/agency_audit").lstrip("/"))
        monkeypatch.setattr(settings, "pg_user", parsed.username or "agency_audit")
        monkeypatch.setattr(settings, "pg_password", parsed.password or "")

        # Mock audit_website so we never make real HTTP calls, regardless
        # of how many pending websites exist in the fixture database.
        mock_audit_data = MagicMock()
        mock_audit_data.score = 80
        mock_audit_data.to_dict.return_value = {"score": 80}

        with patch(
            "agency_audit.loop.orchestrator.audit_website",
            new_callable=AsyncMock,
            return_value=mock_audit_data,
        ) as mock_audit:
            result = await _audit_country_websites("BG")

        assert result["audited"] == pending_count
        # The mock audit_website always "succeeds", so all audited sites
        # should be counted as succeeded.
        assert result["succeeded"] == pending_count
        assert result["failed"] == 0
        assert mock_audit.call_count == pending_count
