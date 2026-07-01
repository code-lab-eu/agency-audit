"""Tests for DiscoveryPipeline.search_tiled — adaptive quadtree-based
tiled Places Text Search.

All tests mock search_text and resolve_city_viewport — no live network
or database.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest

from agency_audit.config import settings
from agency_audit.discovery import (
    DiscoveryPipeline,
    PlaceResult,
    PlacesAPIClient,
    TextSearchResult,
)
from agency_audit.discovery_geo import Rectangle


def _make_place(place_id: str, name: str = "Test Agency") -> PlaceResult:
    """Create a PlaceResult with minimal fields."""
    return PlaceResult(place_id=place_id, name=name)


def _make_places(count: int, prefix: str = "pid") -> list[PlaceResult]:
    """Return *count* PlaceResult objects with unique ids."""
    return [_make_place(f"{prefix}-{i}", f"Agency {i}") for i in range(count)]


def _make_city(
    label: str = "Sofia",
    country: str = "BG",
    latitude: float = 42.6977,
    longitude: float = 23.3219,
    viewport_low_lat: float | None = None,
    viewport_low_lng: float | None = None,
    viewport_high_lat: float | None = None,
    viewport_high_lng: float | None = None,
) -> dict:
    """Build a city dict with optional cached viewport columns."""
    return dict(
        id=1,
        label=label,
        country=country,
        latitude=latitude,
        longitude=longitude,
        viewport_low_lat=viewport_low_lat,
        viewport_low_lng=viewport_low_lng,
        viewport_high_lat=viewport_high_lat,
        viewport_high_lng=viewport_high_lng,
    )


def _make_places_client() -> AsyncMock:
    """Return an AsyncMock PlacesAPIClient with api_call_count tracking."""
    client = AsyncMock(spec=PlacesAPIClient)
    client.api_call_count = 0
    return client


def _mock_search_text_wrapper(
    places_client: AsyncMock,
    fn,
):
    """Wrap a mock search_text function to also increment api_call_count on
    every call (simulating one HTTP request per non-paginated search).

    Mocks may return either a bare ``list[PlaceResult]`` (wrapped into a
    non-truncated ``TextSearchResult`` here) or a ``TextSearchResult`` when
    they need to signal ``budget_truncated``."""

    async def wrapped(*args, **kwargs):
        result = await fn(*args, **kwargs)
        places_client.api_call_count += 1
        if isinstance(result, list):
            return TextSearchResult(places=result)
        return result

    return wrapped


# ──────────────────────────────────────────────────────────────────────
# Saturated tile subdivision
# ──────────────────────────────────────────────────────────────────────


class TestSearchTiledSubdivision:
    """Verify that saturated tiles are subdivided and sparse tiles are not."""

    async def test_saturated_tile_is_subdivided(self, monkeypatch):
        """A tile returning >= threshold results is subdivided into 4 children,
        and each child is searched.  Sparse sibling tiles are NOT subdivided."""
        monkeypatch.setattr(settings, "places_tile_saturation_threshold", 4)
        monkeypatch.setattr(settings, "places_tile_max_depth", 2)

        searched_tiles: list[Rectangle] = []

        async def mock_search_text(
            query: str,
            location_restriction: Rectangle | None = None,
            **kwargs,
        ) -> list[PlaceResult]:
            assert location_restriction is not None
            searched_tiles.append(location_restriction)

            # Identify the tile by its centre
            centre_lat = (location_restriction.low_lat + location_restriction.high_lat) / 2
            centre_lng = (location_restriction.low_lng + location_restriction.high_lng) / 2

            # Root viewport: 42.0,23.0 → 43.0,24.0  (centre ~42.5,23.5)
            # NW (north-west): centre ~42.75,23.25 → SATURATED
            # NE (north-east): centre ~42.75,23.75 → SPARSE
            # SW (south-west): centre ~42.25,23.25 → SPARSE
            # SE (south-east): centre ~42.25,23.75 → SPARSE

            def _near(val: float, expected: float) -> bool:
                return pytest.approx(val, abs=0.1) == expected

            is_nw = _near(centre_lat, 42.75) and _near(centre_lng, 23.25)
            is_ne = _near(centre_lat, 42.75) and _near(centre_lng, 23.75)
            is_sw = _near(centre_lat, 42.25) and _near(centre_lng, 23.25)
            is_se = _near(centre_lat, 42.25) and _near(centre_lng, 23.75)

            if is_nw:
                return _make_places(5, "nw")  # saturated → subdivide
            if is_ne:
                return _make_places(2, "ne")  # sparse → keep
            if is_sw:
                return _make_places(2, "sw")  # sparse → keep
            if is_se:
                return _make_places(2, "se")  # sparse → keep

            # Root tile and NW's depth-2 children: return saturated
            return _make_places(5, "root")

        places_client = _make_places_client()
        places_client.search_text = _mock_search_text_wrapper(places_client, mock_search_text)

        pipeline = DiscoveryPipeline(places_client=places_client)
        viewport = Rectangle(42.0, 23.0, 43.0, 24.0)

        with patch.object(pipeline, "resolve_city_viewport", return_value=viewport):
            _ = await pipeline.search_tiled(query="real estate agent", city=_make_city())

        # 1 root + 4 root-children + 4 NW-children = 9 calls
        # (NE, SW, SE are sparse → not subdivided)
        assert len(searched_tiles) == 9
        await pipeline.close()

    async def test_sparse_tile_not_subdivided(self, monkeypatch):
        """When all tiles return below the threshold, none are subdivided."""
        monkeypatch.setattr(settings, "places_tile_saturation_threshold", 60)
        monkeypatch.setattr(settings, "places_tile_max_depth", 3)

        call_count = 0

        async def mock_search_text(
            query: str,
            location_restriction: Rectangle | None = None,
            **kwargs,
        ) -> list[PlaceResult]:
            nonlocal call_count
            call_count += 1
            return _make_places(5, f"sparse-{call_count}")

        places_client = _make_places_client()
        places_client.search_text = _mock_search_text_wrapper(places_client, mock_search_text)

        pipeline = DiscoveryPipeline(places_client=places_client)
        viewport = Rectangle(42.0, 23.0, 43.0, 24.0)

        with patch.object(pipeline, "resolve_city_viewport", return_value=viewport):
            _ = await pipeline.search_tiled(query="real estate agent", city=_make_city())

        # Only the root tile was searched — no subdivision occurred
        assert call_count == 1
        await pipeline.close()


# ──────────────────────────────────────────────────────────────────────
# Max-depth guard
# ──────────────────────────────────────────────────────────────────────


class TestSearchTiledMaxDepth:
    """Depth stops at places_tile_max_depth even if tiles are saturated."""

    async def test_max_depth_prevents_infinite_recursion(self, monkeypatch):
        """At depth == max_depth, saturated tiles are NOT subdivided."""
        monkeypatch.setattr(settings, "places_tile_saturation_threshold", 1)
        monkeypatch.setattr(settings, "places_tile_max_depth", 2)

        call_count = 0

        async def mock_search_text(
            query: str,
            location_restriction: Rectangle | None = None,
            **kwargs,
        ) -> list[PlaceResult]:
            nonlocal call_count
            call_count += 1
            # Always return saturated to tempt subdivision
            return _make_places(2, f"saturated-{call_count}")

        places_client = _make_places_client()
        places_client.search_text = _mock_search_text_wrapper(places_client, mock_search_text)

        pipeline = DiscoveryPipeline(places_client=places_client)
        viewport = Rectangle(42.0, 23.0, 43.0, 24.0)

        with patch.object(pipeline, "resolve_city_viewport", return_value=viewport):
            await pipeline.search_tiled(query="test", city=_make_city())

        # Depth 0: 1 root call
        # Depth 1: 4 children
        # Depth 2: 4×4 = 16 children (no further subdivision)
        # Total: 1 + 4 + 16 = 21
        assert call_count == 21
        await pipeline.close()


# ──────────────────────────────────────────────────────────────────────
# Deduplication by place_id
# ──────────────────────────────────────────────────────────────────────


class TestSearchTiledDedup:
    """Results are deduplicated by place_id — first occurrence wins."""

    async def test_dedup_across_tiles(self, monkeypatch):
        """When the same place_id appears in multiple tiles, only the
        first occurrence is kept."""
        monkeypatch.setattr(settings, "places_tile_saturation_threshold", 4)
        monkeypatch.setattr(settings, "places_tile_max_depth", 2)

        call_count = 0

        async def mock_search_text(
            query: str,
            location_restriction: Rectangle | None = None,
            **kwargs,
        ) -> list[PlaceResult]:
            nonlocal call_count
            call_count += 1
            # First call (root) returns saturated → subdivide
            if call_count == 1:
                return [_make_place("dup-1", "Dup One"), _make_place("unique-root")]
            # Call 2 (NW) returns duplicate + unique
            if call_count == 2:
                return [
                    _make_place("dup-1", "Dup One Again"),
                    _make_place("nw-unique", "NW Unique"),
                ]
            # Calls 3-5 (NE, SW, SE) return saturated
            if call_count in (3, 4, 5):
                return _make_places(5, f"tile-{call_count}")
            # Children of NW (depth 2) — stop here
            return _make_places(2, f"deep-{call_count}")

        places_client = _make_places_client()
        places_client.search_text = _mock_search_text_wrapper(places_client, mock_search_text)

        pipeline = DiscoveryPipeline(places_client=places_client)
        viewport = Rectangle(42.0, 23.0, 43.0, 24.0)

        with patch.object(pipeline, "resolve_city_viewport", return_value=viewport):
            results = await pipeline.search_tiled(query="test", city=_make_city())

        # "dup-1" should appear exactly once
        dup_count = sum(1 for r in results if r.place_id == "dup-1")
        assert dup_count == 1

        # The kept duplicate should be from the root (first occurrence)
        dup = next(r for r in results if r.place_id == "dup-1")
        assert dup.name == "Dup One"

        await pipeline.close()

    async def test_empty_place_id_excluded(self, monkeypatch):
        """PlaceResults with empty string place_id are excluded from results
        (consistent with discover_city filtering)."""
        monkeypatch.setattr(settings, "places_tile_saturation_threshold", 60)

        async def mock_search_text(
            query: str,
            location_restriction: Rectangle | None = None,
            **kwargs,
        ) -> list[PlaceResult]:
            return [
                _make_place("valid-1", "Valid One"),
                _make_place("", "Empty ID"),
                _make_place("valid-2", "Valid Two"),
            ]

        places_client = _make_places_client()
        places_client.search_text = _mock_search_text_wrapper(places_client, mock_search_text)

        pipeline = DiscoveryPipeline(places_client=places_client)
        viewport = Rectangle(42.0, 23.0, 43.0, 24.0)

        with patch.object(pipeline, "resolve_city_viewport", return_value=viewport):
            results = await pipeline.search_tiled(query="test", city=_make_city())

        # Only results with non-empty place_id are kept
        assert len(results) == 2
        assert results[0].place_id == "valid-1"
        assert results[1].place_id == "valid-2"

        await pipeline.close()


# ──────────────────────────────────────────────────────────────────────
# Call budget / places_max_calls_per_city
# ──────────────────────────────────────────────────────────────────────


class TestSearchTiledCallBudget:
    """places_max_calls_per_city caps recursion and logs skipped tiles."""

    async def test_budget_exhausted_logs_warning(self, monkeypatch, caplog):
        """When call_count hits the budget, further tiles are skipped
        and a WARNING is logged with the count of skipped tiles."""
        monkeypatch.setattr(settings, "places_tile_saturation_threshold", 1)
        monkeypatch.setattr(settings, "places_tile_max_depth", 3)
        monkeypatch.setattr(settings, "places_max_calls_per_city", 3)

        caplog.set_level(logging.WARNING)

        async def mock_search_text(
            query: str,
            location_restriction: Rectangle | None = None,
            **kwargs,
        ) -> list[PlaceResult]:
            # Every tile is "saturated" so we always subdivide
            return _make_places(2)

        places_client = _make_places_client()
        places_client.search_text = _mock_search_text_wrapper(places_client, mock_search_text)

        pipeline = DiscoveryPipeline(places_client=places_client)
        viewport = Rectangle(42.0, 23.0, 43.0, 24.0)

        with patch.object(pipeline, "resolve_city_viewport", return_value=viewport):
            _ = await pipeline.search_tiled(query="test", city=_make_city(label="TestCity"))

        # Budget was hit — should log the warning
        assert "TestCity" in caplog.text
        assert "budget exhausted" in caplog.text
        assert "tiles skipped" in caplog.text

        await pipeline.close()

    async def test_budget_exhausted_counts_child_tiles(self, monkeypatch, caplog):
        """When the budget is exhausted by a saturated tile that cannot
        subdivide, its direct children are counted as skipped (not silently
        dropped).  Reproduction from the review: max_calls=1 + saturated root."""
        monkeypatch.setattr(settings, "places_tile_saturation_threshold", 1)
        monkeypatch.setattr(settings, "places_tile_max_depth", 3)
        monkeypatch.setattr(settings, "places_max_calls_per_city", 1)

        caplog.set_level(logging.WARNING)

        async def mock_search_text(
            query: str,
            location_restriction: Rectangle | None = None,
            **kwargs,
        ) -> list[PlaceResult]:
            return _make_places(2)

        places_client = _make_places_client()
        places_client.search_text = _mock_search_text_wrapper(places_client, mock_search_text)

        pipeline = DiscoveryPipeline(places_client=places_client)
        viewport = Rectangle(42.0, 23.0, 43.0, 24.0)

        with patch.object(pipeline, "resolve_city_viewport", return_value=viewport):
            _ = await pipeline.search_tiled(query="test", city=_make_city(label="TestCity"))

        # The root tile was searched (1 call, saturated) and its 4 children
        # are counted as skipped because budget prevents subdivision.
        assert "budget exhausted" in caplog.text
        assert "4 tiles skipped" in caplog.text

        await pipeline.close()

    async def test_budget_exhausted_deep_tree_skipped_count(self, monkeypatch, caplog):
        """Skipped tiles from budget exhaustion at deeper levels are counted
        correctly.  With max_calls=3, the last saturated call cannot subdivide,
        so 4 direct children are skipped plus the remaining siblings at all
        levels."""
        monkeypatch.setattr(settings, "places_tile_saturation_threshold", 1)
        monkeypatch.setattr(settings, "places_tile_max_depth", 3)
        monkeypatch.setattr(settings, "places_max_calls_per_city", 3)

        caplog.set_level(logging.WARNING)

        async def mock_search_text(
            query: str,
            location_restriction: Rectangle | None = None,
            **kwargs,
        ) -> list[PlaceResult]:
            return _make_places(2)

        places_client = _make_places_client()
        places_client.search_text = _mock_search_text_wrapper(places_client, mock_search_text)

        pipeline = DiscoveryPipeline(places_client=places_client)
        viewport = Rectangle(42.0, 23.0, 43.0, 24.0)

        with patch.object(pipeline, "resolve_city_viewport", return_value=viewport):
            _ = await pipeline.search_tiled(query="test", city=_make_city(label="TestCity"))

        # Execution trace (3 calls, depth ≤ 3, always saturated):
        #   call 1 (root, d=0): saturated → subdivide
        #   call 2 (NW, d=1):   saturated → subdivide
        #   call 3 (NW-NW, d=2): saturated, budget exhausted → +4 skipped
        #   NW-NE, NW-SW, NW-SE (d=2): each +1 skipped (budget guard)
        #   NE, SW, SE (d=1): each +1 skipped (budget guard)
        #   Total skipped = 4 + 3 + 3 = 10
        assert "budget exhausted" in caplog.text
        assert "10 tiles skipped" in caplog.text

        await pipeline.close()

    async def test_budget_not_exhausted_no_warning(self, monkeypatch, caplog):
        """When call_count stays below the budget, no warning is logged."""
        monkeypatch.setattr(settings, "places_tile_saturation_threshold", 60)
        monkeypatch.setattr(settings, "places_max_calls_per_city", 100)

        caplog.set_level(logging.WARNING)

        async def mock_search_text(
            query: str,
            location_restriction: Rectangle | None = None,
            **kwargs,
        ) -> list[PlaceResult]:
            return _make_places(5)

        places_client = _make_places_client()
        places_client.search_text = _mock_search_text_wrapper(places_client, mock_search_text)

        pipeline = DiscoveryPipeline(places_client=places_client)
        viewport = Rectangle(42.0, 23.0, 43.0, 24.0)

        with patch.object(pipeline, "resolve_city_viewport", return_value=viewport):
            await pipeline.search_tiled(query="test", city=_make_city())

        assert "budget exhausted" not in caplog.text

        await pipeline.close()

    async def test_max_requests_bounds_tile_search(self, monkeypatch, caplog):
        """max_requests limits each tile search to the remaining budget,
        preventing overrun.  Captured max_requests values decrease as the
        budget is consumed, and the last call receives exactly 1."""
        monkeypatch.setattr(settings, "places_tile_saturation_threshold", 1)
        monkeypatch.setattr(settings, "places_tile_max_depth", 2)
        monkeypatch.setattr(settings, "places_max_calls_per_city", 3)

        caplog.set_level(logging.WARNING)

        captured_max_requests: list[int | None] = []

        async def mock_search_text(
            query: str,
            location_restriction: Rectangle | None = None,
            max_requests: int | None = None,
            **kwargs,
        ) -> list[PlaceResult]:
            captured_max_requests.append(max_requests)
            return _make_places(2)

        places_client = _make_places_client()
        places_client.search_text = _mock_search_text_wrapper(places_client, mock_search_text)

        pipeline = DiscoveryPipeline(places_client=places_client)
        viewport = Rectangle(42.0, 23.0, 43.0, 24.0)

        with patch.object(pipeline, "resolve_city_viewport", return_value=viewport):
            _ = await pipeline.search_tiled(query="test", city=_make_city(label="TestCity"))

        # Budget=3, always saturated, depth≤2:
        # call 1 (root, d=0): remaining=3 → max_requests=3
        # call 2 (NW, d=1):   remaining=2 → max_requests=2
        # call 3 (NW-NW, d=2): remaining=1 → max_requests=1, depth==max_depth
        # Budget exhausted → leaf siblings + NE/SW/SE skipped
        assert captured_max_requests == [3, 2, 1]
        assert "budget exhausted" in caplog.text

        await pipeline.close()

    async def test_budget_truncated_tile_is_observable(self, monkeypatch, caplog):
        """When search_text reports budget_truncated, search_tiled logs and
        counts the truncation — the caller knows the tile is partial.

        Truncation is taken from search_text's explicit signal, not inferred
        from request counts.  Here every search_text call reports that it
        stopped with a page still pending."""
        monkeypatch.setattr(settings, "places_tile_saturation_threshold", 1)
        monkeypatch.setattr(settings, "places_tile_max_depth", 2)
        monkeypatch.setattr(settings, "places_max_calls_per_city", 2)

        caplog.set_level(logging.WARNING)

        async def mock_search_text(
            query: str,
            location_restriction: Rectangle | None = None,
            **kwargs,
        ) -> TextSearchResult:
            # Explicitly signal that pagination was cut off with more available.
            return TextSearchResult(places=_make_places(2), budget_truncated=True)

        places_client = _make_places_client()
        places_client.search_text = _mock_search_text_wrapper(places_client, mock_search_text)

        pipeline = DiscoveryPipeline(places_client=places_client)
        viewport = Rectangle(42.0, 23.0, 43.0, 24.0)

        with patch.object(pipeline, "resolve_city_viewport", return_value=viewport):
            await pipeline.search_tiled(query="test", city=_make_city(label="Varna"))

        # max_calls=2, always saturated (threshold=1):
        # call 1 (root, d=0): truncated → +1 truncated; saturated, call_count=1<2 → subdivide
        # call 2 (NW, d=1):   truncated → +1 truncated; saturated, call_count=2>=2 → +4 skipped
        # NE, SW, SE (d=1): each +1 skipped (top-of-function budget guard)
        # Truncated: 2, Skipped: 4 + 3 = 7
        assert "budget exhausted" in caplog.text
        assert "Varna" in caplog.text
        assert "7 tiles skipped" in caplog.text
        assert "2 tiles budget-truncated" in caplog.text

        await pipeline.close()

    async def test_complete_last_tile_not_flagged_as_truncated(self, monkeypatch, caplog):
        """A complete response on the last allowed call must NOT be reported as
        budget-truncated.

        Regression for the count-based heuristic: previously a sparse, complete
        one-page tile that happened to spend the final request unit was accused
        of truncation.  Now truncation comes only from search_text's explicit
        flag, so a complete tile (budget_truncated=False) is never flagged."""
        monkeypatch.setattr(settings, "places_tile_saturation_threshold", 60)
        monkeypatch.setattr(settings, "places_tile_max_depth", 2)
        monkeypatch.setattr(settings, "places_max_calls_per_city", 1)

        caplog.set_level(logging.WARNING)

        async def mock_search_text(
            query: str,
            location_restriction: Rectangle | None = None,
            **kwargs,
        ) -> TextSearchResult:
            # One complete page, no pending next page → not truncated.
            return TextSearchResult(places=_make_places(1), budget_truncated=False)

        places_client = _make_places_client()
        places_client.search_text = _mock_search_text_wrapper(places_client, mock_search_text)

        pipeline = DiscoveryPipeline(places_client=places_client)
        viewport = Rectangle(42.0, 23.0, 43.0, 24.0)

        with patch.object(pipeline, "resolve_city_viewport", return_value=viewport):
            await pipeline.search_tiled(query="test", city=_make_city(label="Sofia"))

        # max_calls=1, sparse (1 < threshold 60) → no subdivision, no skips.
        # The single call completed cleanly, so nothing is truncated.
        assert "budget-truncated" not in caplog.text
        assert "budget exhausted" not in caplog.text

        await pipeline.close()


# ──────────────────────────────────────────────────────────────────────
# resolve_city_viewport integration
# ──────────────────────────────────────────────────────────────────────


class TestSearchTiledViewportResolution:
    """search_tiled calls resolve_city_viewport and uses the returned Rectangle."""

    async def test_uses_resolve_city_viewport_result(self, monkeypatch):
        """The viewport returned by resolve_city_viewport is passed to
        the first search_text call as the location_restriction."""
        monkeypatch.setattr(settings, "places_tile_saturation_threshold", 60)

        captured_tile: Rectangle | None = None

        async def mock_search_text(
            query: str,
            location_restriction: Rectangle | None = None,
            **kwargs,
        ) -> list[PlaceResult]:
            nonlocal captured_tile
            captured_tile = location_restriction
            return _make_places(5)

        places_client = _make_places_client()
        places_client.search_text = _mock_search_text_wrapper(places_client, mock_search_text)

        pipeline = DiscoveryPipeline(places_client=places_client)
        viewport = Rectangle(42.5, 23.0, 42.9, 23.8)

        with patch.object(pipeline, "resolve_city_viewport", return_value=viewport):
            await pipeline.search_tiled(query="test", city=_make_city())

        assert captured_tile is not None
        assert captured_tile.low_lat == 42.5
        assert captured_tile.low_lng == 23.0
        assert captured_tile.high_lat == 42.9
        assert captured_tile.high_lng == 23.8

        await pipeline.close()
