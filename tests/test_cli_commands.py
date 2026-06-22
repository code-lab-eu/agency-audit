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
# stats command
# ──────────────────────────────────────────────────────────────────────


def test_stats_command_executes():
    """stats command executes with mocked DB."""
    mock_pool, mock_conn = _make_pool_mock()
    # stats uses pool.fetchval() directly, not acquire()
    mock_pool.fetchval = AsyncMock(return_value=10)

    mock_get_pool = AsyncMock(return_value=mock_pool)
    mock_close_pool = AsyncMock()

    with patch("agency_audit.cli.get_pool", new=mock_get_pool), \
         patch("agency_audit.cli.close_pool", new=mock_close_pool):
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0


# ──────────────────────────────────────────────────────────────────────
# run command (full loop for one country)
# ──────────────────────────────────────────────────────────────────────


def test_run_command_executes():
    """run command invokes run_country via asyncio.run."""
    with patch("agency_audit.loop.orchestrator.run_country") as mock_run:
        mock_run.return_value = {
            "country": "BG",
            "phases": {},
            "errors": [],
            "duration_seconds": 0.01,
        }
        result = runner.invoke(app, [
            "run", "--country", "BG",
            "--skip-discovery", "--skip-audit",
            "--skip-qc", "--skip-reaudit",
        ])
        assert result.exit_code == 0


def test_run_command_with_result_phases():
    """run command prints results for each phase."""
    with patch("agency_audit.loop.orchestrator.run_country") as mock_run:
        mock_run.return_value = {
            "country": "BG",
            "phases": {
                "discovery": {"cities_processed": 3, "agencies_found": 10},
                "audit": {"succeeded": 8, "failed": 2, "websites_audited": 10},
                "qc": {"findings": 2, "suspicious_scores": 1, "duplicate_domains": 1},
                "reaudit": {"queued": 0, "oldest_age_days": None},
            },
            "errors": [],
            "duration_seconds": 5.5,
        }
        result = runner.invoke(app, ["run", "--country", "BG"])
        assert result.exit_code == 0


# ──────────────────────────────────────────────────────────────────────
# run-all command
# ──────────────────────────────────────────────────────────────────────


def test_run_all_command_executes():
    """run-all command invokes run_all_countries."""
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


# ──────────────────────────────────────────────────────────────────────
# qc command
# ──────────────────────────────────────────────────────────────────────


def test_qc_run_action():
    """qc --action run invokes run_qc_checks."""
    with patch("agency_audit.loop.qc.run_qc_checks") as mock_qc:
        mock_qc.return_value = {
            "suspicious_scores": 2,
            "duplicate_domains": 1,
            "total_findings": 3,
        }
        result = runner.invoke(app, ["qc", "--action", "run"])
        assert result.exit_code == 0


def test_qc_list_review_action():
    """qc --action list-review invokes get_websites_needing_review."""
    with patch("agency_audit.loop.qc.get_websites_needing_review") as mock_review:
        mock_review.return_value = []
        result = runner.invoke(app, ["qc", "--action", "list-review"])
        assert result.exit_code == 0


def test_qc_mark_review_action():
    """qc --action mark-review invokes mark_for_manual_review."""
    with patch("agency_audit.loop.qc.mark_for_manual_review"):
        result = runner.invoke(app, [
            "qc", "--action", "mark-review",
            "--website-id", "42",
            "--reason", "suspicious",
        ])
        assert result.exit_code == 0


def test_qc_mark_review_missing_args():
    """qc --action mark-review without required args exits with error."""
    with patch("agency_audit.loop.qc.mark_for_manual_review"):
        result = runner.invoke(app, ["qc", "--action", "mark-review"])
        assert result.exit_code == 1


# ──────────────────────────────────────────────────────────────────────
# reaudit command
# ──────────────────────────────────────────────────────────────────────


def test_reaudit_trigger_action():
    """reaudit --action trigger invokes schedule_reaudits."""
    with patch("agency_audit.loop.reaudit.schedule_reaudits") as mock_sched:
        mock_sched.return_value = {"queued": 10, "oldest_age_days": 45}
        result = runner.invoke(app, ["reaudit", "--action", "trigger"])
        assert result.exit_code == 0


def test_reaudit_trigger_with_country():
    """reaudit --action trigger filters by country."""
    with patch("agency_audit.loop.reaudit.schedule_reaudits") as mock_sched:
        mock_sched.return_value = {"queued": 3, "oldest_age_days": 30}
        result = runner.invoke(app, [
            "reaudit", "--action", "trigger", "--country", "BG"
        ])
        assert result.exit_code == 0


def test_reaudit_queue_action_empty():
    """reaudit --action queue shows empty queue message."""
    with patch("agency_audit.loop.reaudit.get_reaudit_queue") as mock_queue:
        mock_queue.return_value = []
        result = runner.invoke(app, ["reaudit", "--action", "queue"])
        assert result.exit_code == 0


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


# ──────────────────────────────────────────────────────────────────────
# progress command
# ──────────────────────────────────────────────────────────────────────


def test_progress_command_with_data():
    """progress command displays progress table."""
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


# ──────────────────────────────────────────────────────────────────────
# discover command
# ──────────────────────────────────────────────────────────────────────


def test_discover_command():
    """discover command invokes run_discovery."""
    with patch("agency_audit.discovery.run_discovery") as mock_disc:
        mock_disc.return_value = {
            "countries_processed": 1,
            "cities_processed": 3,
            "agencies_found": 15,
            "results": {"BG": {"cities": 3, "agencies": 15}},
        }
        result = runner.invoke(app, ["discover", "--country", "BG", "--max-cities", "3"])
        assert result.exit_code == 0


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


# ──────────────────────────────────────────────────────────────────────
# serve command
# ──────────────────────────────────────────────────────────────────────


def test_serve_command_executes():
    """serve command invokes uvicorn.run (mocked to not block)."""
    with patch("uvicorn.run") as mock_run:
        result = runner.invoke(app, ["serve", "--host", "127.0.0.1", "--port", "9999"])
        assert result.exit_code == 0
        mock_run.assert_called_once()
