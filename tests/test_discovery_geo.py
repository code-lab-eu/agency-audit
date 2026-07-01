"""Tests for pure-Python geometry helpers (discovery_geo)."""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError

import pytest

from agency_audit.discovery_geo import (
    Rectangle,
    bbox_from_center,
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


# ── bbox_from_center ──────────────────────────────────────────────────


class TestBboxFromCenter:
    """Verify that bbox_from_center produces a square bounding box whose side
    length is correct at a non-equatorial latitude."""

    def test_centered(self):
        """The returned rectangle is centred on the given point."""
        lat, lon = 50.0, 14.0  # approximately Prague
        half = 5000.0  # 5 km half-extent → 10 km square
        bbox = bbox_from_center(lat, lon, half)

        centre_lat = (bbox.low_lat + bbox.high_lat) / 2
        centre_lng = (bbox.low_lng + bbox.high_lng) / 2

        assert centre_lat == pytest.approx(lat)
        assert centre_lng == pytest.approx(lon)

    def test_latitude_size(self):
        """At any latitude, height in degrees ≈ 2 * half_extent / 111320."""
        for lat in (0.0, 45.0, 60.0, 80.0):
            half = 10000.0  # 10 km half → 20 km square
            bbox = bbox_from_center(lat, 0.0, half)

            expected_deg = (2.0 * half) / 111_320.0
            actual_deg = bbox.high_lat - bbox.low_lat
            assert actual_deg == pytest.approx(expected_deg)

    def test_longitude_extent_at_50n(self):
        """At 50°N, 1° longitude ≈ 111320 * cos(50°) ≈ 71550 m.
        So for half_extent_m = 5000, d_lng ≈ 5000 / 71550 ≈ 0.0699°."""
        lat = 50.0
        half = 5000.0
        bbox = bbox_from_center(lat, 14.0, half)

        m_per_deg_lng = 111_320.0 * math.cos(math.radians(lat))
        expected_d_lng = half / m_per_deg_lng
        actual_d_lng = bbox.high_lng - bbox.low_lng

        # Width should be 2 * d_lng
        assert actual_d_lng == pytest.approx(2.0 * expected_d_lng)

    def test_square_shape(self):
        """Height and width in degrees are equal at the equator (cos(0)=1)."""
        bbox = bbox_from_center(0.0, 0.0, 5000.0)

        height = bbox.high_lat - bbox.low_lat
        width = bbox.high_lng - bbox.low_lng
        assert height == pytest.approx(width)

    def test_longitude_stretched_at_high_latitude(self):
        """At higher latitudes longitude degrees cover less ground, so the
        longitude span in degrees must be larger to cover the same metres."""
        half = 5000.0
        eq = bbox_from_center(0.0, 0.0, half)
        polar = bbox_from_center(60.0, 0.0, half)

        eq_width = eq.high_lng - eq.low_lng
        polar_width = polar.high_lng - polar.low_lng

        # At 60°N longitude degrees should be ~2× as wide in degrees.
        assert polar_width > eq_width


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
