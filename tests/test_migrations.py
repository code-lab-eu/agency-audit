"""Tests for agency_audit.migrations — SQL migration runner."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agency_audit.migrations import run_migrations


class TestRunMigrations:
    """Tests for run_migrations — applying ordered SQL files to a connection."""

    @pytest.mark.asyncio
    async def test_run_migrations(self):
        """Running migrations against a temp directory with two SQL files."""
        mock_conn = AsyncMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "01_init.sql").write_text("CREATE TABLE test (id INT);")
            (Path(tmpdir) / "02_data.sql").write_text("INSERT INTO test VALUES (1);")

            result = await run_migrations(mock_conn, Path(tmpdir))
            assert len(result) == 2
            assert "01_init.sql" in result
            assert "02_data.sql" in result
            assert mock_conn.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_run_migrations_empty_dir(self):
        """Running migrations on an empty directory returns an empty list."""
        mock_conn = AsyncMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            result = await run_migrations(mock_conn, Path(tmpdir))
            assert result == []
            mock_conn.execute.assert_not_called()
