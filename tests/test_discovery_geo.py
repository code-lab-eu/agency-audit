"""Tests for pure-Python geometry helpers (discovery_geo)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agency_audit.discovery_geo import (
    Rectangle,
    is_saturated,
    subdivide,
)

# ── Rectangle ─────────────────────────────────────────────────────────


def test_rectangle_attributes():
    """Rectangle stores its four corners."""
    r = Rectangle(low_lat=42.0, low_lng=23.0, high_lat=43.0, high_lng=24.0)
    assert r.low_lat == 42.0
    assert r.low_lng == 23.0
    assert r.high_lat == 43.0
    assert r.high_lng == 24.0


def test_rectangle_is_frozen():
    """Rectangle is immutable (frozen=True)."""
    r = Rectangle(0.0, 0.0, 1.0, 1.0)
    with pytest.raises(FrozenInstanceError):
        r.low_lat = 0.5  # type: ignore[misc]


def test_rectangle_area():
    """Area is width × height in square degrees."""
    r = Rectangle(40.0, 20.0, 42.0, 24.0)
    assert r.area == pytest.approx(8.0)


# ── subdivide ─────────────────────────────────────────────────────────


class TestSubdivide:
    """Verify that subdivide produces four non-overlapping quadrants that
    exactly tile the parent rectangle."""

    def test_four_children(self):
        """subdivide always returns exactly 4 rectangles."""
        r = Rectangle(40.0, 20.0, 44.0, 28.0)
        children = subdivide(r)
        assert len(children) == 4

    def test_each_child_is_smaller(self):
        """Every child has strictly smaller area than the parent (unless
        parent is degenerate)."""
        r = Rectangle(40.0, 20.0, 44.0, 28.0)
        for c in subdivide(r):
            assert c.area < r.area

    def test_children_sum_to_parent_area(self):
        """The sum of the four children's areas equals the parent area."""
        r = Rectangle(40.0, 20.0, 44.0, 28.0)
        child_area = sum(c.area for c in subdivide(r))
        assert child_area == pytest.approx(r.area)

    def test_no_gaps_centers_touch(self):
        """The inner corner of every child meets at the parent's centre, so
        there is no gap in the middle."""
        r = Rectangle(40.0, 20.0, 44.0, 28.0)
        mid_lat = (r.low_lat + r.high_lat) / 2
        mid_lng = (r.low_lng + r.high_lng) / 2

        nw, ne, sw, se = subdivide(r)

        # Every child shares *at least* the mid-point on one of its edges.
        assert nw.high_lat == pytest.approx(mid_lat) or nw.low_lat == pytest.approx(mid_lat)
        assert nw.high_lng == pytest.approx(mid_lng) or nw.low_lng == pytest.approx(mid_lng)

    def test_quadrant_names_match_geometry(self):
        """NW is north-west of centre, NE is north-east, etc."""
        r = Rectangle(40.0, 20.0, 44.0, 28.0)
        mid_lat = (r.low_lat + r.high_lat) / 2
        mid_lng = (r.low_lng + r.high_lng) / 2

        nw, ne, sw, se = subdivide(r)

        # NW: high_lat == parent high, low_lng == parent low
        assert nw.high_lat == r.high_lat
        assert nw.low_lng == r.low_lng
        assert nw.low_lat == pytest.approx(mid_lat)
        assert nw.high_lng == pytest.approx(mid_lng)

        # NE: high_lat == parent high, high_lng == parent high
        assert ne.high_lat == r.high_lat
        assert ne.high_lng == r.high_lng
        assert ne.low_lat == pytest.approx(mid_lat)
        assert ne.low_lng == pytest.approx(mid_lng)

        # SW: low_lat == parent low, low_lng == parent low
        assert sw.low_lat == r.low_lat
        assert sw.low_lng == r.low_lng
        assert sw.high_lat == pytest.approx(mid_lat)
        assert sw.high_lng == pytest.approx(mid_lng)

        # SE: low_lat == parent low, high_lng == parent high
        assert se.low_lat == r.low_lat
        assert se.high_lng == r.high_lng
        assert se.high_lat == pytest.approx(mid_lat)
        assert se.low_lng == pytest.approx(mid_lng)

    def test_no_overlap(self):
        """Quadrants are non-overlapping — the top of SW equals the bottom of
        NW, the left of SE equals the right of SW, etc."""
        r = Rectangle(40.0, 20.0, 44.0, 28.0)
        nw, ne, sw, se = subdivide(r)

        # NW and SW share the same longitude range and meet at latitude.
        assert sw.high_lat == pytest.approx(nw.low_lat)
        # NE and SE share the same longitude range and meet at latitude.
        assert se.high_lat == pytest.approx(ne.low_lat)
        # NW and NE share the same latitude range and meet at longitude.
        assert nw.high_lng == pytest.approx(ne.low_lng)
        # SW and SE share the same latitude range and meet at longitude.
        assert sw.high_lng == pytest.approx(se.low_lng)

    def test_various_rectangles(self):
        """subdivide works on rectangles with negative coordinates and
        different aspect ratios."""
        cases = [
            Rectangle(-10.0, -20.0, 10.0, 20.0),  # centred on origin
            Rectangle(50.0, 10.0, 51.0, 10.1),  # tall and narrow
            Rectangle(0.0, 0.0, 0.1, 10.0),  # short and wide
            Rectangle(-45.0, -180.0, 45.0, 180.0),  # large
        ]
        for r in cases:
            children = subdivide(r)
            assert len(children) == 4
            assert sum(c.area for c in children) == pytest.approx(r.area)


# ── is_saturated ─────────────────────────────────────────────────────


class TestIsSaturated:
    """Boundary and edge-case tests for is_saturated."""

    def test_below_threshold_is_false(self):
        """result_count == threshold - 1 → False."""
        assert is_saturated(59, 60) is False
        assert is_saturated(0, 60) is False
        assert is_saturated(1, 2) is False

    def test_at_threshold_is_true(self):
        """result_count == threshold → True."""
        assert is_saturated(60, 60) is True
        assert is_saturated(0, 0) is True
        assert is_saturated(2, 2) is True

    def test_above_threshold_is_true(self):
        """result_count > threshold → True."""
        assert is_saturated(61, 60) is True
        assert is_saturated(1000, 60) is True

    def test_invariants(self):
        """Sanity-check a few invariants."""
        # Once saturated, adding more results stays saturated.
        assert is_saturated(60, 60) is True
        assert is_saturated(999, 60) is True
        # Zero threshold is always saturated for non-negative counts.
        for n in (0, 1, 100):
            assert is_saturated(n, 0) is True
