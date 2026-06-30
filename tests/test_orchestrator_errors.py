"""Additional tests for orchestrator error paths. Push coverage further."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
    async def test_run_all_countries_default_countries(self):
        """run_all_countries without countries list fetches active countries from DB.

        Query-path test: exercises the real database via get_pool() so the
        ``SELECT iso FROM countries WHERE active = true`` query runs against
        PostgreSQL.  run_country is still mocked — we are testing the
        country-fetch path, not the full loop.
        """
        from agency_audit.loop.orchestrator import run_all_countries

        with patch("agency_audit.loop.orchestrator.run_country") as mock_run:
            mock_run.return_value = {
                "country": "BG",
                "phases": {},
                "errors": [],
                "duration_seconds": 0.01,
            }

            result = await run_all_countries()

            # Seeded DB has BE, BG, ES, RS as active=true
            assert result["totals"]["countries_processed"] == 4
            assert mock_run.call_count == 4
            # Verify the specific active countries were processed
            assert set(result["results"].keys()) == {"BE", "BG", "ES", "RS"}

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
    async def test_audit_country_websites_empty(self):
        """_audit_country_websites returns zeros when no pending websites.

        Query-path test: exercises the real database via get_pool().  The
        seeded test database has no websites, so the pending-websites query
        returns empty and the function returns all zeros.
        """
        from agency_audit.loop.orchestrator import _audit_country_websites

        result = await _audit_country_websites("BG")
        assert result == {"audited": 0, "succeeded": 0, "failed": 0}
