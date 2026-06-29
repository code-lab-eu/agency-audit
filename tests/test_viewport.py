"""Tests for agency_audit.viewport — viewport preset storage layer."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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
    """Tests for load_viewports(user_id: str) -> list[dict]."""

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
