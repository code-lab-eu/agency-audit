"""Tests for seed/countries.sql: the discovery order of the 44 countries."""

import asyncpg

EU = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
    "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
}  # fmt: skip
EFTA = {"IS", "LI", "NO", "CH"}
OTHER = {"AD", "AL", "BA", "GB", "MC", "MD", "ME", "MK", "RS", "SM", "TR", "UA", "XK"}


async def test_seed_ranks_eu_then_efta_then_others(db_conn: asyncpg.Connection) -> None:
    rows = await db_conn.fetch("SELECT iso, discovery_rank FROM countries ORDER BY discovery_rank")
    ranks = [row["discovery_rank"] for row in rows]
    order = [row["iso"] for row in rows]

    assert ranks == list(range(1, 45))
    assert set(order[:27]) == EU
    assert set(order[27:31]) == EFTA
    assert set(order[31:]) == OTHER


async def test_seed_ranks_active_countries_first_in_their_group(
    db_conn: asyncpg.Connection,
) -> None:
    rows = await db_conn.fetch("SELECT iso FROM countries ORDER BY discovery_rank")
    order = [row["iso"] for row in rows]

    assert order[:3] == ["ES", "BE", "BG"]
    assert order[31] == "RS"
