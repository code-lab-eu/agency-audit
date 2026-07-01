"""Integration tests for the geometry module — runs against a live PostGIS database.

These tests exercise query_by_bounding_box, set_location, and
bulk_set_locations against an actual PostGIS instance to verify that
the SQL is valid, the spatial predicates are correct, and the lat/lng
axis handling is right.  The shared ``db_conn`` fixture from
``tests/conftest.py`` guarantees that all migrations (including PostGIS)
are applied and wraps each test in a rollback-only transaction — no
skip-on-unavailable or manual-cleanup scaffolding is needed.

The pure-mock guard tests (missing conn/pool, parameter ordering checks)
remain in tests/test_geometry.py since those genuinely don't need a DB.
"""

from __future__ import annotations

import asyncpg

from agency_audit.geometry import bulk_set_locations, query_by_bounding_box, set_location


async def _insert_website(conn: asyncpg.Connection, url: str, label: str = "") -> int:
    """Insert a minimal test website and return its id."""
    return await conn.fetchval(
        "INSERT INTO websites (url, label) VALUES ($1, $2) RETURNING id",
        url,
        label,
    )


def _point_wkt(lat: float, lng: float) -> str:
    """Return a WKT representation of a point (lng lat for WGS84).

    Uses :g format to match PostGIS 18 output: integer-valued coordinates
    are rendered without a trailing .0 (e.g. POINT(23 42) not POINT(23.0 42.0)).
    """
    return f"POINT({lng:g} {lat:g})"


# ---------------------------------------------------------------------------
# query_by_bounding_box — live PostGIS
# ---------------------------------------------------------------------------


class TestQueryByBoundingBoxLive:
    """Integration tests for query_by_bounding_box against real PostGIS."""

    async def test_finds_websites_inside_bbox(self, db_conn: asyncpg.Connection):
        """Websites whose location falls within the bbox should be returned."""
        # Sofia: ~42.7N, 23.3E
        id_a = await _insert_website(db_conn, "https://test-geo-a.example.com", "A")
        id_b = await _insert_website(db_conn, "https://test-geo-b.example.com", "B")

        await db_conn.execute(
            "UPDATE websites SET location = ST_SetSRID(ST_MakePoint($1, $2), 4326) WHERE id = $3",
            23.3219,
            42.6977,
            id_a,
        )
        await db_conn.execute(
            "UPDATE websites SET location = ST_SetSRID(ST_MakePoint($1, $2), 4326) WHERE id = $3",
            23.3300,
            42.6900,
            id_b,
        )

        results = await query_by_bounding_box(42.0, 23.0, 43.0, 24.0, conn=db_conn)

        ids = {r["id"] for r in results}
        assert id_a in ids
        assert id_b in ids

    async def test_excludes_websites_outside_bbox(self, db_conn: asyncpg.Connection):
        """Websites whose location is outside the bbox should NOT be returned."""
        inside_id = await _insert_website(db_conn, "https://test-geo-inside.example.com", "Inside")
        outside_id = await _insert_website(
            db_conn, "https://test-geo-outside.example.com", "Outside"
        )

        # Sofia (inside bbox 42-43, 23-24)
        await db_conn.execute(
            "UPDATE websites SET location = ST_SetSRID(ST_MakePoint($1, $2), 4326) WHERE id = $3",
            23.5,
            42.5,
            inside_id,
        )
        # London (far outside)
        await db_conn.execute(
            "UPDATE websites SET location = ST_SetSRID(ST_MakePoint($1, $2), 4326) WHERE id = $3",
            -0.1278,
            51.5074,
            outside_id,
        )

        results = await query_by_bounding_box(42.0, 23.0, 43.0, 24.0, conn=db_conn)

        ids = {r["id"] for r in results}
        assert inside_id in ids
        assert outside_id not in ids

    async def test_excludes_null_location(self, db_conn: asyncpg.Connection):
        """Websites without a location should NOT appear in results."""
        with_loc_id = await _insert_website(
            db_conn, "https://test-geo-withloc.example.com", "WithLoc"
        )
        no_loc_id = await _insert_website(db_conn, "https://test-geo-noloc.example.com", "NoLoc")

        await db_conn.execute(
            "UPDATE websites SET location = ST_SetSRID(ST_MakePoint($1, $2), 4326) WHERE id = $3",
            23.5,
            42.5,
            with_loc_id,
        )
        # no_loc_id gets no location — leave it null

        results = await query_by_bounding_box(42.0, 23.0, 43.0, 24.0, conn=db_conn)

        ids = {r["id"] for r in results}
        assert with_loc_id in ids
        assert no_loc_id not in ids

    async def test_precise_bbox_edge_case(self, db_conn: asyncpg.Connection):
        """A point exactly on the bbox edge should be included (&& is inclusive)."""
        edge_id = await _insert_website(db_conn, "https://test-geo-edge.example.com", "Edge")

        # Exactly at (42.0, 23.0) — the min corner
        await db_conn.execute(
            "UPDATE websites SET location = ST_SetSRID(ST_MakePoint($1, $2), 4326) WHERE id = $3",
            23.0,
            42.0,
            edge_id,
        )

        results = await query_by_bounding_box(42.0, 23.0, 43.0, 24.0, conn=db_conn)

        ids = {r["id"] for r in results}
        assert edge_id in ids, "Point on bbox edge should be included"

    async def test_bbox_crosses_equator_and_meridian(self, db_conn: asyncpg.Connection):
        """Bbox crossing equator / prime meridian should work correctly."""
        ny_id = await _insert_website(db_conn, "https://test-geo-ny.example.com", "NY")
        sp_id = await _insert_website(db_conn, "https://test-geo-sp.example.com", "SP")

        # New York (~40.7N, -74.0W)
        await db_conn.execute(
            "UPDATE websites SET location = ST_SetSRID(ST_MakePoint($1, $2), 4326) WHERE id = $3",
            -74.0,
            40.7,
            ny_id,
        )
        # São Paulo (~23.5S, -46.6W)
        await db_conn.execute(
            "UPDATE websites SET location = ST_SetSRID(ST_MakePoint($1, $2), 4326) WHERE id = $3",
            -46.6,
            -23.5,
            sp_id,
        )

        # Bbox covering both Americas
        results = await query_by_bounding_box(-30.0, -80.0, 50.0, -40.0, conn=db_conn)

        ids = {r["id"] for r in results}
        assert ny_id in ids, "New York should be in Americas bbox"
        assert sp_id in ids, "São Paulo should be in Americas bbox"

    async def test_returns_location_wkt(self, db_conn: asyncpg.Connection):
        """Each result row should include a location_wkt field from ST_AsText."""
        wid = await _insert_website(db_conn, "https://test-geo-wkt.example.com", "WKT")
        await db_conn.execute(
            "UPDATE websites SET location = ST_SetSRID(ST_MakePoint($1, $2), 4326) WHERE id = $3",
            23.3219,
            42.6977,
            wid,
        )

        results = await query_by_bounding_box(42.0, 23.0, 43.0, 24.0, conn=db_conn)

        assert len(results) == 1
        assert results[0]["location_wkt"] == _point_wkt(42.6977, 23.3219)

    async def test_empty_bbox_returns_empty_list(self, db_conn: asyncpg.Connection):
        """A bbox containing no websites should return an empty list."""
        wid = await _insert_website(db_conn, "https://test-geo-empty.example.com", "Empty")
        # Sofia centre
        await db_conn.execute(
            "UPDATE websites SET location = ST_SetSRID(ST_MakePoint($1, $2), 4326) WHERE id = $3",
            23.3219,
            42.6977,
            wid,
        )

        # Query a bbox far away (middle of Pacific ocean)
        results = await query_by_bounding_box(0.0, 0.0, 1.0, 1.0, conn=db_conn)

        assert results == []


# ---------------------------------------------------------------------------
# set_location — live PostGIS
# ---------------------------------------------------------------------------


class TestSetLocationLive:
    """Integration tests for set_location against real PostGIS."""

    async def test_sets_location_on_website(self, db_conn: asyncpg.Connection):
        """After set_location, the geometry should be retrievable."""
        wid = await _insert_website(db_conn, "https://test-geo-set.example.com", "Set")

        await set_location(wid, 42.6977, 23.3219, conn=db_conn)

        location_wkt = await db_conn.fetchval(
            "SELECT ST_AsText(location) FROM websites WHERE id = $1", wid
        )
        assert location_wkt == _point_wkt(42.6977, 23.3219)

    async def test_overwrites_existing_location(self, db_conn: asyncpg.Connection):
        """Calling set_location again should update, not duplicate."""
        wid = await _insert_website(db_conn, "https://test-geo-overwrite.example.com", "Overwrite")
        await db_conn.execute(
            "UPDATE websites SET location = ST_SetSRID(ST_MakePoint($1, $2), 4326) WHERE id = $3",
            1.0,
            1.0,
            wid,
        )

        await set_location(wid, 42.0, 23.0, conn=db_conn)

        location_wkt = await db_conn.fetchval(
            "SELECT ST_AsText(location) FROM websites WHERE id = $1", wid
        )
        assert location_wkt == _point_wkt(42.0, 23.0)


# ---------------------------------------------------------------------------
# bulk_set_locations — live PostGIS
# ---------------------------------------------------------------------------


class TestBulkSetLocationsLive:
    """Integration tests for bulk_set_locations against real PostGIS."""

    async def test_bulk_sets_multiple_locations(self, db_conn: asyncpg.Connection):
        """Should update multiple websites in a single batch."""
        id_a = await _insert_website(db_conn, "https://test-geo-bulk-a.example.com", "BulkA")
        id_b = await _insert_website(db_conn, "https://test-geo-bulk-b.example.com", "BulkB")
        id_c = await _insert_website(db_conn, "https://test-geo-bulk-c.example.com", "BulkC")

        rows = [
            (id_a, 42.6977, 23.3219),
            (id_b, 43.2100, 27.9200),
            (id_c, 42.1500, 24.7500),
        ]
        count = await bulk_set_locations(rows, conn=db_conn)
        assert count == 3

        loc_a = await db_conn.fetchval(
            "SELECT ST_AsText(location) FROM websites WHERE id = $1", id_a
        )
        loc_b = await db_conn.fetchval(
            "SELECT ST_AsText(location) FROM websites WHERE id = $1", id_b
        )
        loc_c = await db_conn.fetchval(
            "SELECT ST_AsText(location) FROM websites WHERE id = $1", id_c
        )

        assert loc_a == _point_wkt(42.6977, 23.3219)
        assert loc_b == _point_wkt(43.2100, 27.9200)
        assert loc_c == _point_wkt(42.1500, 24.7500)

    async def test_bulk_with_existing_locations_overwrites(self, db_conn: asyncpg.Connection):
        """Bulk update should overwrite, not duplicate, existing locations."""
        wid = await _insert_website(
            db_conn, "https://test-geo-bulk-overwrite.example.com", "BulkOw"
        )
        await db_conn.execute(
            "UPDATE websites SET location = ST_SetSRID(ST_MakePoint($1, $2), 4326) WHERE id = $3",
            1.0,
            1.0,
            wid,
        )

        count = await bulk_set_locations([(wid, 42.0, 23.0)], conn=db_conn)
        assert count == 1

        location_wkt = await db_conn.fetchval(
            "SELECT ST_AsText(location) FROM websites WHERE id = $1", wid
        )
        assert location_wkt == _point_wkt(42.0, 23.0)

    async def test_bulk_empty_returns_zero(self, db_conn: asyncpg.Connection):
        """Empty batch should return 0 without errors."""
        count = await bulk_set_locations([], conn=db_conn)
        assert count == 0

    async def test_bulk_axis_not_swapped(self, db_conn: asyncpg.Connection):
        """Regression guard: lat/lng order is correct (not swapped) against
        real PostGIS. If the axis were swapped, query_by_bounding_box with
        a tight bbox around the stored point would miss it."""
        wid = await _insert_website(db_conn, "https://test-geo-axis.example.com", "Axis")

        # Sofia centre: 42.6977N, 23.3219E
        lat, lng = 42.6977, 23.3219
        await set_location(wid, lat, lng, conn=db_conn)

        # Query a tiny bbox ±0.01 around the point
        results = await query_by_bounding_box(
            lat - 0.01,
            lng - 0.01,
            lat + 0.01,
            lng + 0.01,
            conn=db_conn,
        )

        ids = {r["id"] for r in results}
        assert wid in ids, (
            "Point not found in tight bbox around itself — lat/lng axis may be swapped"
        )
