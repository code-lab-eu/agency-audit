"""Tests for agency_audit.viewport — viewport preset storage layer.

Unit tests (mocked pool) cover the CRUD functions without a live database.
Integration tests (DB-backed) verify the full save→load→delete lifecycle
and the migration (005_viewport_presets) against a real PostgreSQL instance.
"""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from agency_audit.config import settings
from agency_audit.db import close_pool

# ============================================================================
# Unit tests — mocked connection pool
# ============================================================================


class TestSaveViewport:
    """Tests for save_viewport(data: dict) -> int."""

    @pytest.mark.asyncio
    async def test_save_viewport_returns_id(self):
        """save_viewport should insert and return the new row id."""
        from agency_audit.viewport import save_viewport

        with patch("agency_audit.viewport.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.fetchval = AsyncMock(return_value=42)

            data = {
                "user_id": "test-user",
                "name": "Sofia Center",
                "center_lat": 42.6977,
                "center_lng": 23.3219,
                "zoom_level": 12,
                "north": 42.75,
                "south": 42.65,
                "east": 23.40,
                "west": 23.25,
            }

            result = await save_viewport(data)
            assert result == 42
            mock_conn.fetchval.assert_called_once()

    @pytest.mark.asyncio
    async def test_save_viewport_without_user_id(self):
        """save_viewport should handle missing user_id (NULL in DB)."""
        from agency_audit.viewport import save_viewport

        with patch("agency_audit.viewport.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.fetchval = AsyncMock(return_value=7)

            data = {
                "name": "No User Preset",
                "center_lat": 40.0,
                "center_lng": 20.0,
                "zoom_level": 8,
                "north": 45.0,
                "south": 35.0,
                "east": 25.0,
                "west": 15.0,
            }

            result = await save_viewport(data)
            assert result == 7

            # Verify user_id was passed as None
            call_args = mock_conn.fetchval.call_args
            # The first positional arg after the SQL should be None
            assert call_args.args[1] is None


class TestLoadViewports:
    """Tests for load_viewports(user_id: str | None) -> list[dict]."""

    @pytest.mark.asyncio
    async def test_load_viewports_empty(self):
        """load_viewports should return empty list when no presets exist."""
        from agency_audit.viewport import load_viewports

        with patch("agency_audit.viewport.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.fetch = AsyncMock(return_value=[])

            result = await load_viewports("test-user")
            assert result == []

    @pytest.mark.asyncio
    async def test_load_viewports_returns_rows(self):
        """load_viewports should return parsed rows with correct structure."""
        from agency_audit.viewport import load_viewports

        now = datetime.now(UTC)

        with patch("agency_audit.viewport.get_pool") as mock_get_pool:
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
                        "user_id": "test-user",
                        "name": "Preset A",
                        "center_lat": 42.0,
                        "center_lng": 24.0,
                        "zoom_level": 10,
                        "north": 43.0,
                        "south": 41.0,
                        "east": 25.0,
                        "west": 23.0,
                        "created_at": now,
                        "updated_at": now,
                    },
                    {
                        "id": 2,
                        "user_id": "test-user",
                        "name": "Preset B",
                        "center_lat": 43.0,
                        "center_lng": 25.0,
                        "zoom_level": 11,
                        "north": 44.0,
                        "south": 42.0,
                        "east": 26.0,
                        "west": 24.0,
                        "created_at": now,
                        "updated_at": now,
                    },
                ]
            )

            result = await load_viewports("test-user")
            assert len(result) == 2

            # Check first row
            assert result[0]["id"] == 1
            assert result[0]["user_id"] == "test-user"
            assert result[0]["name"] == "Preset A"
            assert result[0]["center_lat"] == 42.0
            assert result[0]["center_lng"] == 24.0
            assert result[0]["zoom_level"] == 10
            assert result[0]["north"] == 43.0
            assert result[0]["south"] == 41.0
            assert result[0]["east"] == 25.0
            assert result[0]["west"] == 23.0
            assert isinstance(result[0]["created_at"], str)
            assert isinstance(result[0]["updated_at"], str)

    @pytest.mark.asyncio
    async def test_load_viewports_filters_by_user_id(self):
        """load_viewports should filter by the given user_id only."""
        from agency_audit.viewport import load_viewports

        with patch("agency_audit.viewport.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.fetch = AsyncMock(return_value=[])

            await load_viewports("specific-user")

            # Verify the query used the right user_id
            call_args = mock_conn.fetch.call_args
            assert call_args.args[1] == "specific-user"

    @pytest.mark.asyncio
    async def test_load_viewports_anonymous_with_default(self):
        """load_viewports() with no argument loads anonymous (NULL user_id) presets."""
        from agency_audit.viewport import load_viewports

        with patch("agency_audit.viewport.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.fetch = AsyncMock(return_value=[])

            await load_viewports()

            # Verify the query passed None for user_id (anonymous presets)
            call_args = mock_conn.fetch.call_args
            assert call_args.args[1] is None

    @pytest.mark.asyncio
    async def test_load_viewports_anonymous_explicit_none(self):
        """load_viewports(None) loads anonymous (NULL user_id) presets."""
        from agency_audit.viewport import load_viewports

        with patch("agency_audit.viewport.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.fetch = AsyncMock(return_value=[])

            await load_viewports(None)

            call_args = mock_conn.fetch.call_args
            assert call_args.args[1] is None


class TestDeleteViewport:
    """Tests for delete_viewport(id: int) -> bool."""

    @pytest.mark.asyncio
    async def test_delete_viewport_returns_true(self):
        """delete_viewport should return True when a row is deleted."""
        from agency_audit.viewport import delete_viewport

        with patch("agency_audit.viewport.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.execute = AsyncMock(return_value="DELETE 1")

            result = await delete_viewport(42)
            assert result is True

    @pytest.mark.asyncio
    async def test_delete_viewport_returns_false(self):
        """delete_viewport should return False when no row matches."""
        from agency_audit.viewport import delete_viewport

        with patch("agency_audit.viewport.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.execute = AsyncMock(return_value="DELETE 0")

            result = await delete_viewport(999)
            assert result is False

    @pytest.mark.asyncio
    async def test_delete_viewport_uses_correct_id(self):
        """delete_viewport should pass the correct id to the query."""
        from agency_audit.viewport import delete_viewport

        with patch("agency_audit.viewport.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.execute = AsyncMock(return_value="DELETE 1")

            await delete_viewport(77)

            call_args = mock_conn.execute.call_args
            assert call_args.args[1] == 77


class TestNoSearchGeometryDeps:
    """The viewport module must not import from search or geometry modules."""

    def test_viewport_module_no_search_imports(self):
        """viewport.py must not import discovery, audit, or geometry modules."""
        import ast
        from pathlib import Path

        viewport_path = Path(__file__).parent.parent / "src" / "agency_audit" / "viewport.py"
        source = viewport_path.read_text()
        tree = ast.parse(source)

        forbidden = {"discovery", "geometry", "search", "audit"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name.split(".")[-1] if "." in alias.name else alias.name
                    assert name not in forbidden, f"viewport.py imports forbidden module: {name}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                name = node.module.split(".")[-1] if "." in node.module else node.module
                assert name not in forbidden, f"viewport.py imports forbidden module: {name}"


class TestModuleExports:
    """Test that the module exports are importable."""

    def test_save_viewport_importable(self):
        """save_viewport should be importable from agency_audit.viewport."""
        from agency_audit.viewport import save_viewport

        assert callable(save_viewport)

    def test_load_viewports_importable(self):
        """load_viewports should be importable from agency_audit.viewport."""
        from agency_audit.viewport import load_viewports

        assert callable(load_viewports)

    def test_delete_viewport_importable(self):
        """delete_viewport should be importable from agency_audit.viewport."""
        from agency_audit.viewport import delete_viewport

        assert callable(delete_viewport)


# ============================================================================
# Integration tests — live PostgreSQL database
# ============================================================================


@pytest.fixture
async def db_conn():
    """Direct connection for viewport integration test setup/teardown.

    Uses a fresh connection (not the pool) so it works reliably across
    pytest-asyncio's per-function event loops.

    Skips the whole module when no PostgreSQL is reachable (e.g. in CI
    without a Postgres service container).
    """
    try:
        conn = await asyncpg.connect(dsn=settings.dsn)
    except OSError as exc:
        pytest.skip(f"PostgreSQL not available for integration tests: {exc}")
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
async def integration_cleanup(db_conn):
    """Reset viewport data before and after each integration test.

    Also runs the 005_viewport_presets migration if the table doesn't
    exist yet, and closes the shared pool after each test so the next
    test gets a fresh pool on its own event loop.
    """
    # Ensure the table and schema exist
    migrations_dir = Path(__file__).parent.parent / "src" / "agency_audit" / "migrations"
    migration_path = migrations_dir / "005_viewport_presets.sql"
    if migration_path.exists():
        sql = migration_path.read_text(encoding="utf-8")
        await db_conn.execute(sql)
    else:
        # Migration not available — skip integration tests
        pytest.skip("005_viewport_presets.sql migration not found")

    # Clean up any test data left from previous runs
    await db_conn.execute("DELETE FROM viewport_presets WHERE name LIKE 'test-%'")
    await db_conn.execute("DELETE FROM viewport_presets WHERE name LIKE 'itest-%'")

    yield

    # Clean up test data
    await db_conn.execute("DELETE FROM viewport_presets WHERE name LIKE 'test-%'")
    await db_conn.execute("DELETE FROM viewport_presets WHERE name LIKE 'itest-%'")

    # Reset the module-level pool so the next test creates a fresh one
    # on its own event loop
    await close_pool()


class TestViewportIntegration:
    """End-to-end CRUD tests against a live PostgreSQL database."""

    async def test_save_and_load_user_viewport(self):
        """Save a viewport with user_id, then load it back."""
        from agency_audit.viewport import load_viewports, save_viewport

        data = {
            "user_id": "itest-user",
            "name": "itest-Sofia-User",
            "center_lat": 42.6977,
            "center_lng": 23.3219,
            "zoom_level": 12,
            "north": 42.75,
            "south": 42.65,
            "east": 23.40,
            "west": 23.25,
        }

        preset_id = await save_viewport(data)
        assert isinstance(preset_id, int)
        assert preset_id > 0

        results = await load_viewports("itest-user")
        assert len(results) >= 1

        found = next((r for r in results if r["id"] == preset_id), None)
        assert found is not None
        assert found["user_id"] == "itest-user"
        assert found["name"] == "itest-Sofia-User"
        assert found["center_lat"] == 42.6977
        assert found["center_lng"] == 23.3219
        assert found["zoom_level"] == 12
        assert found["north"] == 42.75
        assert found["south"] == 42.65
        assert found["east"] == 23.40
        assert found["west"] == 23.25
        assert found["created_at"] is not None
        assert found["updated_at"] is not None

    async def test_save_and_load_anonymous_viewport(self):
        """Save a viewport without user_id (NULL), then load it as anonymous."""
        from agency_audit.viewport import load_viewports, save_viewport

        data = {
            "name": "itest-Anonymous-Preset",
            "center_lat": 40.0,
            "center_lng": 20.0,
            "zoom_level": 8,
            "north": 45.0,
            "south": 35.0,
            "east": 25.0,
            "west": 15.0,
        }

        preset_id = await save_viewport(data)
        assert preset_id > 0

        # Load anonymous presets (user_id is NULL)
        results = await load_viewports(None)
        assert len(results) >= 1

        found = next((r for r in results if r["id"] == preset_id), None)
        assert found is not None
        assert found["user_id"] is None
        assert found["name"] == "itest-Anonymous-Preset"

        # Also test with default argument (no user_id passed)
        results_default = await load_viewports()
        found_default = next((r for r in results_default if r["id"] == preset_id), None)
        assert found_default is not None
        assert found_default["user_id"] is None

    async def test_anonymous_presets_not_mixed_with_user_presets(self):
        """Anonymous presets must not appear when loading a specific user's presets."""
        from agency_audit.viewport import load_viewports, save_viewport

        # Save an anonymous preset
        await save_viewport(
            {
                "name": "itest-Anon-Mixed",
                "center_lat": 41.0,
                "center_lng": 21.0,
                "zoom_level": 7,
                "north": 42.0,
                "south": 40.0,
                "east": 22.0,
                "west": 20.0,
            }
        )

        # Save a user preset
        await save_viewport(
            {
                "user_id": "itest-mixed-user",
                "name": "itest-User-Mixed",
                "center_lat": 42.0,
                "center_lng": 23.0,
                "zoom_level": 10,
                "north": 43.0,
                "south": 41.0,
                "east": 24.0,
                "west": 22.0,
            }
        )

        # Loading the specific user should NOT include anonymous presets
        user_results = await load_viewports("itest-mixed-user")
        user_names = {r["name"] for r in user_results}
        assert "itest-Anon-Mixed" not in user_names
        assert "itest-User-Mixed" in user_names

        # Loading anonymous should NOT include the user's preset
        anon_results = await load_viewports(None)
        anon_names = {r["name"] for r in anon_results}
        assert "itest-User-Mixed" not in anon_names
        assert "itest-Anon-Mixed" in anon_names

    async def test_delete_existing_viewport(self):
        """Delete an existing viewport and confirm it's gone."""
        from agency_audit.viewport import delete_viewport, load_viewports, save_viewport

        data = {
            "user_id": "itest-delete-user",
            "name": "itest-Delete-Me",
            "center_lat": 41.0,
            "center_lng": 20.0,
            "zoom_level": 9,
            "north": 42.0,
            "south": 40.0,
            "east": 21.0,
            "west": 19.0,
        }

        preset_id = await save_viewport(data)
        assert preset_id > 0

        # Confirm it exists
        results_before = await load_viewports("itest-delete-user")
        assert any(r["id"] == preset_id for r in results_before)

        # Delete it
        result = await delete_viewport(preset_id)
        assert result is True

        # Confirm it's gone
        results_after = await load_viewports("itest-delete-user")
        assert not any(r["id"] == preset_id for r in results_after)

    async def test_delete_nonexistent_viewport(self):
        """Deleting a non-existent id returns False."""
        from agency_audit.viewport import delete_viewport

        result = await delete_viewport(99999999)
        assert result is False

    async def test_load_viewports_returns_newest_first(self):
        """load_viewports should order results by created_at DESC."""
        from agency_audit.viewport import load_viewports, save_viewport

        base = {
            "user_id": "itest-order-user",
            "center_lat": 42.0,
            "center_lng": 23.0,
            "zoom_level": 10,
            "north": 43.0,
            "south": 41.0,
            "east": 24.0,
            "west": 22.0,
        }

        id_a = await save_viewport({**base, "name": "itest-Order-A"})
        id_b = await save_viewport({**base, "name": "itest-Order-B"})
        id_c = await save_viewport({**base, "name": "itest-Order-C"})

        results = await load_viewports("itest-order-user")
        ids = [r["id"] for r in results]

        # Only check our test presets (others may exist in the DB)
        test_ids = [i for i in ids if i in (id_a, id_b, id_c)]
        assert len(test_ids) == 3
        # C was inserted last, so it appears first (DESC order)
        assert test_ids[0] == id_c
        assert test_ids[1] == id_b
        assert test_ids[2] == id_a

    async def test_migration_005_table_exists(self, db_conn):
        """005_viewport_presets migration should create the table and indexes."""
        # Table exists
        exists = await db_conn.fetchval(
            "SELECT EXISTS ("
            "  SELECT FROM information_schema.tables "
            "  WHERE table_name = 'viewport_presets'"
            ")"
        )
        assert exists is True

        # Required columns
        columns = await db_conn.fetch(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = 'viewport_presets' "
            "ORDER BY ordinal_position"
        )
        col_names = {row["column_name"] for row in columns}

        expected = {
            "id",
            "user_id",
            "name",
            "center_lat",
            "center_lng",
            "zoom_level",
            "north",
            "south",
            "east",
            "west",
            "created_at",
            "updated_at",
        }
        assert expected.issubset(col_names), f"Missing columns: {expected - col_names}"

        # user_id must be nullable (optional)
        user_id_col = next((row for row in columns if row["column_name"] == "user_id"), None)
        assert user_id_col is not None
        assert user_id_col["is_nullable"] == "YES"

        # Indexes
        indexes = await db_conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'viewport_presets'"
        )
        index_names = {row["indexname"] for row in indexes}
        assert "idx_viewport_presets_user" in index_names
        assert "idx_viewport_presets_created" in index_names
