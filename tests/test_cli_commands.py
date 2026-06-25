"""CLI command tests that execute async bodies by mocking DB dependencies.

Instead of mocking asyncio.run, we mock the DB pool functions so the real
asyncio loop can execute the async _run() function bodies.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from agency_audit.cli import app

runner = CliRunner()


def _make_pool_mock():
    """Create a mock pool that returns an async context manager connection."""
    mock_conn = AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_conn
    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_ctx
    # Also mock as a plain context manager variant
    return mock_pool, mock_conn


# ──────────────────────────────────────────────────────────────────────
# help text — every command's --help should show relevant description
# ──────────────────────────────────────────────────────────────────────


class TestHelpText:
    """Verify each subcommand's --help output includes the expected description."""

    def test_app_help(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "Real Estate Radar" in result.output

    def test_db_init_help(self):
        result = runner.invoke(app, ["db-init", "--help"])
        assert result.exit_code == 0
        assert "Apply migrations" in result.output

    def test_seed_countries_help(self):
        result = runner.invoke(app, ["seed-countries", "--help"])
        assert result.exit_code == 0
        assert "Seed the countries table" in result.output

    def test_import_cities_help(self):
        result = runner.invoke(app, ["import-cities", "--help"])
        assert result.exit_code == 0
        assert "Import cities from Geonames dump" in result.output

    def test_serve_help(self):
        result = runner.invoke(app, ["serve", "--help"])
        assert result.exit_code == 0
        assert "Start the FastAPI + HTMX web dashboard" in result.output

    def test_audit_help(self):
        result = runner.invoke(app, ["audit", "--help"])
        assert result.exit_code == 0
        assert "Run a full audit on a website" in result.output

    def test_stats_help(self):
        result = runner.invoke(app, ["stats", "--help"])
        assert result.exit_code == 0
        assert "Show database statistics" in result.output

    def test_batch_audit_help(self):
        result = runner.invoke(app, ["batch-audit", "--help"])
        assert result.exit_code == 0
        assert "Audit multiple websites concurrently" in result.output

    def test_discover_help(self):
        result = runner.invoke(app, ["discover", "--help"])
        assert result.exit_code == 0
        assert "Discover real estate agencies" in result.output

    def test_run_command_help(self):
        result = runner.invoke(app, ["run", "--help"])
        assert result.exit_code == 0
        assert "Execute full operational loop for one country" in result.output

    def test_run_all_command_help(self):
        result = runner.invoke(app, ["run-all", "--help"])
        assert result.exit_code == 0
        assert "Execute full operational loop for all countries" in result.output

    def test_qc_command_help(self):
        result = runner.invoke(app, ["qc", "--help"])
        assert result.exit_code == 0
        assert "Run quality control checks" in result.output

    def test_reaudit_command_help(self):
        result = runner.invoke(app, ["reaudit", "--help"])
        assert result.exit_code == 0
        assert "Manage re-audit queue" in result.output

    def test_progress_command_help(self):
        result = runner.invoke(app, ["progress", "--help"])
        assert result.exit_code == 0
        assert "Show overall pipeline progress" in result.output


# ──────────────────────────────────────────────────────────────────────
# argument validation
# ──────────────────────────────────────────────────────────────────────


def test_audit_arg_validation():
    """audit requires --website-id or --url; exits with error otherwise."""
    with patch("agency_audit.cli.asyncio.run") as mock_asyncio:
        result = runner.invoke(app, ["audit"])
        assert result.exit_code == 1
        assert "Either --website-id or --url is required" in result.output
        mock_asyncio.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# stats command
# ──────────────────────────────────────────────────────────────────────


def test_stats_command_executes():
    """stats command prints database statistics table."""
    mock_pool, mock_conn = _make_pool_mock()
    # stats uses pool.fetchval() directly, not acquire()
    mock_pool.fetchval = AsyncMock(return_value=10)

    mock_get_pool = AsyncMock(return_value=mock_pool)
    mock_close_pool = AsyncMock()

    with (
        patch("agency_audit.cli.get_pool", new=mock_get_pool),
        patch("agency_audit.cli.close_pool", new=mock_close_pool),
    ):
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0
        assert "Database Stats" in result.output
        assert "Countries" in result.output
        assert "Cities" in result.output
        assert "Websites" in result.output


# ──────────────────────────────────────────────────────────────────────
# run command (full loop for one country)
# ──────────────────────────────────────────────────────────────────────


def test_run_command_executes():
    """run command invokes run_country and prints loop results table."""
    with patch("agency_audit.loop.orchestrator.run_country") as mock_run:
        mock_run.return_value = {
            "country": "BG",
            "phases": {},
            "errors": [],
            "duration_seconds": 0.01,
        }
        result = runner.invoke(
            app,
            [
                "run",
                "--country",
                "BG",
                "--skip-discovery",
                "--skip-audit",
                "--skip-qc",
                "--skip-reaudit",
            ],
        )
        assert result.exit_code == 0
        assert "Loop Results" in result.output
        assert "BG" in result.output
        assert "0.01s" in result.output


def test_run_command_with_result_phases():
    """run command prints results for each phase."""
    with patch("agency_audit.loop.orchestrator.run_country") as mock_run:
        mock_run.return_value = {
            "country": "BG",
            "phases": {
                "discovery": {"cities_processed": 3, "agencies_found": 10},
                "audit": {"audits_succeeded": 8, "audits_failed": 2, "websites_audited": 10},
                "qc": {"findings": 2, "suspicious_scores": 1, "duplicate_domains": 1},
                "reaudit": {"queued": 0, "oldest_age_days": None},
            },
            "errors": [],
            "duration_seconds": 5.5,
        }
        result = runner.invoke(app, ["run", "--country", "BG"])
        assert result.exit_code == 0
        assert "Loop Results" in result.output
        assert "BG" in result.output
        assert "Discovery" in result.output
        assert "3 cities" in result.output
        assert "10 agencies" in result.output
        assert "Audit" in result.output
        assert "✓" in result.output
        assert "✗" in result.output
        assert "QC" in result.output
        assert "2 findings" in result.output
        assert "Re-audit" in result.output
        assert "0 websites" in result.output
        assert "5.5s" in result.output


# ──────────────────────────────────────────────────────────────────────
# run-all command
# ──────────────────────────────────────────────────────────────────────


def test_run_all_command_executes():
    """run-all command invokes run_all_countries and prints summary table."""
    with patch("agency_audit.loop.orchestrator.run_all_countries") as mock_run:
        mock_run.return_value = {
            "results": {},
            "totals": {
                "countries_processed": 0,
                "cities_processed": 0,
                "agencies_found": 0,
                "websites_audited": 0,
                "audits_succeeded": 0,
                "audits_failed": 0,
                "qc_findings": 0,
                "reaudit_queued": 0,
                "errors": [],
            },
        }
        result = runner.invoke(app, ["run-all", "--countries", "BG"])
        assert result.exit_code == 0
        assert "Run-All Results" in result.output
        assert "Countries processed" in result.output


# ──────────────────────────────────────────────────────────────────────
# qc command
# ──────────────────────────────────────────────────────────────────────


def test_qc_run_action():
    """qc --action run invokes run_qc_checks and prints results table."""
    with patch("agency_audit.loop.qc.run_qc_checks") as mock_qc:
        mock_qc.return_value = {
            "suspicious_scores": 2,
            "duplicate_domains": 1,
            "total_findings": 3,
        }
        result = runner.invoke(app, ["qc", "--action", "run"])
        assert result.exit_code == 0
        assert "QC Check Results" in result.output
        assert "Suspicious scores" in result.output
        assert "3" in result.output


def test_qc_list_review_action():
    """qc --action list-review invokes get_websites_needing_review and prints result."""
    with patch("agency_audit.loop.qc.get_websites_needing_review") as mock_review:
        mock_review.return_value = []
        result = runner.invoke(app, ["qc", "--action", "list-review"])
        assert result.exit_code == 0
        assert "No websites flagged" in result.output


def test_qc_list_review_action_with_data():
    """qc --action list-review with flagged websites prints review table."""
    with patch("agency_audit.loop.qc.get_websites_needing_review") as mock_review:
        mock_review.return_value = [
            {
                "id": 1,
                "url": "https://example.com",
                "label": "Example Agency",
                "score": 0,
                "review_reason": "suspicious score",
                "qc_checks": None,
            },
            {
                "id": 2,
                "url": "https://test.org",
                "label": "Test Agency",
                "score": 100,
                "review_reason": "perfect score",
                "qc_checks": None,
            },
        ]
        result = runner.invoke(app, ["qc", "--action", "list-review"])
        assert result.exit_code == 0
        assert "Websites Needing Review (2)" in result.output
        assert "example.com" in result.output
        assert "test.org" in result.output
        assert "suspicious score" in result.output
        assert "perfect score" in result.output


def test_qc_mark_review_action():
    """qc --action mark-review invokes mark_for_manual_review and prints confirmation."""
    with patch("agency_audit.loop.qc.mark_for_manual_review"):
        result = runner.invoke(
            app,
            [
                "qc",
                "--action",
                "mark-review",
                "--website-id",
                "42",
                "--reason",
                "suspicious",
            ],
        )
        assert result.exit_code == 0
        assert "Flagged website 42 for manual review: suspicious" in result.output


def test_qc_mark_review_missing_args():
    """qc --action mark-review without required args exits with error and message."""
    with patch("agency_audit.loop.qc.mark_for_manual_review"):
        result = runner.invoke(app, ["qc", "--action", "mark-review"])
        assert result.exit_code == 1
        assert "--website-id and --reason are required" in result.output


# ──────────────────────────────────────────────────────────────────────
# reaudit command
# ──────────────────────────────────────────────────────────────────────


def test_reaudit_trigger_action():
    """reaudit --action trigger invokes schedule_reaudits and prints result."""
    with patch("agency_audit.loop.reaudit.schedule_reaudits") as mock_sched:
        mock_sched.return_value = {"queued": 10, "oldest_age_days": 45}
        result = runner.invoke(app, ["reaudit", "--action", "trigger"])
        assert result.exit_code == 0
        assert "10 websites queued" in result.output
        assert "oldest: 45d" in result.output


def test_reaudit_trigger_with_country():
    """reaudit --action trigger filters by country and prints result."""
    with patch("agency_audit.loop.reaudit.schedule_reaudits") as mock_sched:
        mock_sched.return_value = {"queued": 3, "oldest_age_days": 30}
        result = runner.invoke(app, ["reaudit", "--action", "trigger", "--country", "BG"])
        assert result.exit_code == 0
        assert "3 websites queued" in result.output
        assert "oldest: 30d" in result.output


def test_reaudit_queue_action_empty():
    """reaudit --action queue shows empty queue message."""
    with patch("agency_audit.loop.reaudit.get_reaudit_queue") as mock_queue:
        mock_queue.return_value = []
        result = runner.invoke(app, ["reaudit", "--action", "queue"])
        assert result.exit_code == 0
        assert "No websites overdue" in result.output


def test_reaudit_queue_action_with_data():
    """reaudit --action queue with results shows table."""
    with patch("agency_audit.loop.reaudit.get_reaudit_queue") as mock_queue:
        mock_queue.return_value = [
            {
                "id": 1,
                "url": "https://example.com",
                "label": "Test",
                "score": 50,
                "last_audited_at": "2026-01-01T00:00:00",
                "age_days": 170,
                "country": "BG",
            }
        ]
        result = runner.invoke(app, ["reaudit", "--action", "queue"])
        assert result.exit_code == 0
        assert "Re-Audit Queue" in result.output
        assert "example.com" in result.output
        assert "170d" in result.output


# ──────────────────────────────────────────────────────────────────────
# progress command
# ──────────────────────────────────────────────────────────────────────


def test_progress_command_with_data():
    """progress command displays progress tables with data."""
    with patch("agency_audit.loop.tracking.get_progress") as mock_prog:
        mock_prog.return_value = {
            "overview": {
                "countries": 44,
                "cities_total": 100,
                "cities_done": 50,
                "cities_pending": 50,
                "websites_total": 200,
                "websites_audited": 150,
                "websites_pending": 30,
                "websites_failed": 20,
                "websites_needing_review": 5,
                "avg_score": 75.5,
            },
            "per_country": [
                {
                    "iso": "BG",
                    "label": "Bulgaria",
                    "total_cities": 20,
                    "cities_done": 5,
                    "total_websites": 50,
                    "websites_audited": 30,
                    "avg_score": 78.0,
                }
            ],
            "recent_runs": [
                {
                    "id": 1,
                    "country": "BG",
                    "run_type": "full_loop",
                    "started_at": "2026-06-01T00:00:00",
                    "finished_at": "2026-06-01T00:01:00",
                    "duration_seconds": 60.0,
                    "items_processed": 15,
                    "items_succeeded": 12,
                    "items_failed": 3,
                }
            ],
        }
        result = runner.invoke(app, ["progress"])
        assert result.exit_code == 0
        assert "Agency Audit — Pipeline" in result.output
        assert "44" in result.output
        assert "50" in result.output
        assert "150" in result.output
        assert "Bulgaria" in result.output


# ──────────────────────────────────────────────────────────────────────
# discover command
# ──────────────────────────────────────────────────────────────────────


def test_discover_command():
    """discover command invokes run_discovery and prints results table."""
    with patch("agency_audit.discovery.run_discovery") as mock_disc:
        mock_disc.return_value = {
            "countries_processed": 1,
            "cities_processed": 3,
            "agencies_found": 15,
            "results": {"BG": {"cities": 3, "agencies": 15}},
        }
        result = runner.invoke(app, ["discover", "--country", "BG", "--max-cities", "3"])
        assert result.exit_code == 0
        assert "Discovery Pipeline Results" in result.output
        assert "BG" in result.output
        assert "15" in result.output


def test_discover_command_no_results():
    """discover command with no results shows warning message."""
    with patch("agency_audit.discovery.run_discovery") as mock_disc:
        mock_disc.return_value = {
            "countries_processed": 0,
            "cities_processed": 0,
            "agencies_found": 0,
            "results": {},
        }
        result = runner.invoke(app, ["discover", "--country", "XX"])
        assert result.exit_code == 0
        assert "No pending cities" in result.output


# ──────────────────────────────────────────────────────────────────────
# serve command
# ──────────────────────────────────────────────────────────────────────


def test_serve_command_executes():
    """serve command creates a uvicorn.Server and prints status."""
    with patch("uvicorn.Server") as mock_server_cls:
        mock_server = mock_server_cls.return_value
        mock_server.run = MagicMock()  # prevent actual server start

        with patch("agency_audit.cli.asyncio.run"):
            result = runner.invoke(app, ["serve", "--host", "127.0.0.1", "--port", "9999"])
            assert result.exit_code == 0
            assert "Starting Agency Audit dashboard" in result.output
            mock_server.run.assert_called_once()
