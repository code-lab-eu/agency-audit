"""Tests for agency_audit.migrations — SQL migration runner with version tracking."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from asyncpg.exceptions import UndefinedTableError

from agency_audit.migrations import run_migrations

# ── helpers ────────────────────────────────────────────────────────────────


def _make_connection(*, fetchval_side_effect=None):
    """Create a mock asyncpg.Connection with transaction support.

    Returns (mock_conn, mock_tx) so callers can assert on the
    transaction manager if needed.
    """
    mock_conn = AsyncMock()
    mock_tx = AsyncMock()
    mock_tx.__aenter__.return_value = mock_tx
    # conn.transaction() returns a Transaction directly (not a coroutine)
    # so it must be a regular MagicMock, not an AsyncMock.
    mock_conn.transaction = MagicMock(return_value=mock_tx)

    if fetchval_side_effect is not None:
        mock_conn.fetchval.side_effect = fetchval_side_effect

    return mock_conn, mock_tx


# ── existing tests (updated for new run_migrations behaviour) ──────────────


class TestRunMigrations:
    """Basic migration runner behaviour (non-skip tests)."""

    @pytest.mark.asyncio
    async def test_run_migrations(self):
        """First run: migrations are executed and versions recorded."""
        # Simulate first-ever run — schema_migrations doesn't exist yet
        mock_conn, _mock_tx = _make_connection(
            fetchval_side_effect=UndefinedTableError("table does not exist")
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "01_init.sql").write_text("CREATE TABLE test (id INT);")
            (Path(tmpdir) / "02_data.sql").write_text("INSERT INTO test VALUES (1);")

            result = await run_migrations(mock_conn, Path(tmpdir))
            assert len(result) == 2
            assert "01_init.sql" in result
            assert "02_data.sql" in result
            # Two execute calls per file: the SQL + the INSERT into schema_migrations
            assert mock_conn.execute.call_count == 4

    @pytest.mark.asyncio
    async def test_run_migrations_empty_dir(self):
        """Running migrations on an empty directory returns an empty list."""
        mock_conn, _mock_tx = _make_connection()

        with tempfile.TemporaryDirectory() as tmpdir:
            result = await run_migrations(mock_conn, Path(tmpdir))
            assert result == []
            mock_conn.execute.assert_not_called()


# ── new tests: schema_migrations ledger / skip behaviour ────────────────────


class TestMigrationSkip:
    """Prove already-applied migrations are skipped on re-run."""

    @pytest.mark.asyncio
    async def test_skips_all_when_already_applied(self):
        """Second run: every file is already in schema_migrations → none executed."""
        mock_conn, _mock_tx = _make_connection(
            fetchval_side_effect=[True, True, True]  # all already applied
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "001_a.sql").write_text("CREATE TABLE a ();")
            (Path(tmpdir) / "002_b.sql").write_text("CREATE TABLE b ();")
            (Path(tmpdir) / "003_c.sql").write_text("CREATE TABLE c ();")

            result = await run_migrations(mock_conn, Path(tmpdir))
            assert result == []
            mock_conn.execute.assert_not_called()
            assert mock_conn.transaction.call_count == 0

    @pytest.mark.asyncio
    async def test_skips_only_applied_ones(self):
        """Mixed state: some applied, some not — only the unapplied one runs."""
        mock_conn, _mock_tx = _make_connection(
            fetchval_side_effect=[True, False]  # first skipped, second applied
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "001_a.sql").write_text("CREATE TABLE a ();")
            (Path(tmpdir) / "002_b.sql").write_text("CREATE TABLE b ();")

            result = await run_migrations(mock_conn, Path(tmpdir))
            assert result == ["002_b.sql"]
            # Only one SQL executed + one INSERT = 2 calls
            assert mock_conn.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_first_run_applies_all_and_records(self):
        """Fresh database: UndefinedTableError on every check → all applied."""
        call_count = 0

        def _check_version(_sql, *args):
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                # First three checks — schema_migrations not found
                raise UndefinedTableError("table does not exist")
            # After 000 creates the table, remaining checks succeed
            return False

        mock_conn, _mock_tx = _make_connection(
            fetchval_side_effect=_check_version
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "000_schema_migrations.sql").write_text(
                "CREATE TABLE IF NOT EXISTS schema_migrations (...);"
            )
            (Path(tmpdir) / "001_init.sql").write_text("CREATE TABLE sites ();")
            (Path(tmpdir) / "002_add.sql").write_text("ALTER TABLE sites ADD x INT;")

            result = await run_migrations(mock_conn, Path(tmpdir))
            assert len(result) == 3
            # 3 SQL files + 3 INSERTs = 6 execute calls
            assert mock_conn.execute.call_count == 6

    @pytest.mark.asyncio
    async def test_each_migration_wrapped_in_transaction(self):
        """Each migration executes inside its own transaction."""
        mock_conn, _mock_tx = _make_connection(
            fetchval_side_effect=UndefinedTableError("first run")
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "001_a.sql").write_text("CREATE TABLE a ();")
            (Path(tmpdir) / "002_b.sql").write_text("CREATE TABLE b ();")

            await run_migrations(mock_conn, Path(tmpdir))

            # Two files → two transactions
            assert mock_conn.transaction.call_count == 2

    @pytest.mark.asyncio
    async def test_version_insert_uses_correct_filename(self):
        """The INSERT into schema_migrations records the file's version name."""
        mock_conn, _mock_tx = _make_connection(
            fetchval_side_effect=UndefinedTableError("first run")
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "001_init.sql").write_text("CREATE TABLE sites ();")

            await run_migrations(mock_conn, Path(tmpdir))

            # Two execute calls: one for the SQL, one for the INSERT
            calls = mock_conn.execute.call_args_list
            # The INSERT call should contain the version filename
            insert_call = calls[1]
            assert "schema_migrations" in insert_call.args[0]
            assert insert_call.args[1] == "001_init.sql"
