"""Geonames city import utility.

Downloads the cities15000.zip file from Geonames, parses it, filters by
country whitelist and population threshold, and inserts into the cities table.
"""

import io
import zipfile
from collections.abc import Iterable

import asyncpg
import httpx
from rich.console import Console

from agency_audit.config import settings

console = Console()

# Geonames cities15000.txt column indices (0-based)
# http://download.geonames.org/export/dump/
GEONAMES_COLUMNS = {
    "geonameid": 0,
    "name": 1,
    "asciiname": 2,
    "alternatenames": 3,
    "latitude": 4,
    "longitude": 5,
    "feature_class": 6,
    "feature_code": 7,
    "country_code": 8,
    "cc2": 9,
    "admin1": 10,
    "admin2": 11,
    "admin3": 12,
    "admin4": 13,
    "population": 14,
    "elevation": 15,
    "dem": 16,
    "timezone": 17,
    "modification_date": 18,
}


def _slugify(name: str) -> str:
    """Create a URL-friendly slug from a city name."""
    import re
    import unicodedata

    # Normalize unicode to ASCII
    slug = unicodedata.normalize("NFKD", name)
    slug = slug.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^\w\s-]", "", slug.lower())
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    return slug


def parse_geonames_line(line: str) -> dict | None:
    """Parse a single tab-separated geonames line into a dict."""
    fields = line.strip().split("\t")
    if len(fields) < 15:
        return None

    # Only include populated places (P) and cities
    feature_class = fields[GEONAMES_COLUMNS["feature_class"]]
    if feature_class != "P":
        return None

    country_code = fields[GEONAMES_COLUMNS["country_code"]]
    population = int(fields[GEONAMES_COLUMNS["population"]] or 0)
    if population < settings.geonames_min_population:
        return None

    name = fields[GEONAMES_COLUMNS["asciiname"]] or fields[GEONAMES_COLUMNS["name"]]
    lat = float(fields[GEONAMES_COLUMNS["latitude"]])
    lng = float(fields[GEONAMES_COLUMNS["longitude"]])

    return {
        "country": country_code,
        "label": name,
        "slug": _slugify(name),
        "population": population,
        "latitude": lat,
        "longitude": lng,
    }


def parse_geonames_file(content: bytes, country_filter: set[str] | None = None) -> Iterable[dict]:
    """Parse geonames text content and yield filtered city dicts."""
    text = content.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if not line.strip():
            continue
        city = parse_geonames_line(line)
        if city is None:
            continue
        if country_filter and city["country"] not in country_filter:
            continue
        yield city


async def download_geonames(url: str | None = None) -> bytes:
    """Download and return the contents of the geonames cities15000.zip file."""
    url = url or settings.geonames_url
    console.print(f"[cyan]Downloading geonames data from {url}...[/]")
    async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


def extract_geonames_zip(zip_content: bytes) -> bytes:
    """Extract cities15000.txt from the zip file content."""
    with zipfile.ZipFile(io.BytesIO(zip_content)) as zf:
        # Find the .txt file inside the zip
        txt_name = None
        for name in zf.namelist():
            if name.endswith(".txt"):
                txt_name = name
                break
        if txt_name is None:
            raise ValueError("No .txt file found in geonames zip")
        return zf.read(txt_name)


async def import_geonames(
    conn: asyncpg.Connection,
    country_filter: set[str] | None = None,
    zip_content: bytes | None = None,
) -> int:
    """Download (or use provided) geonames data and import cities into the database.

    Returns the number of cities imported.
    """
    if zip_content is None:
        zip_content = await download_geonames()

    txt_content = extract_geonames_zip(zip_content)
    cities = list(parse_geonames_file(txt_content, country_filter))

    if not cities:
        console.print("[yellow]No cities found matching the filter criteria.[/]")
        return 0

    console.print(f"[green]Found {len(cities)} cities to import.[/]")

    # Batch insert using executemany
    rows = [
        (c["country"], c["label"], c["slug"], c["population"], c["latitude"], c["longitude"])
        for c in cities
    ]

    await conn.executemany(
        """
        INSERT INTO cities (country, label, slug, population, latitude, longitude)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (country, slug) DO UPDATE SET
            label = EXCLUDED.label,
            population = EXCLUDED.population,
            latitude = EXCLUDED.latitude,
            longitude = EXCLUDED.longitude
        """,
        rows,
    )

    console.print(f"[green]Imported {len(rows)} cities.[/]")
    return len(rows)


async def import_geonames_for_countries(
    conn: asyncpg.Connection,
    country_codes: list[str] | None = None,
) -> dict[str, int]:
    """Import geonames cities for specific countries (or all 44 if None).

    Returns a dict mapping country code → number of cities imported.
    """
    if country_codes is None:
        rows = await conn.fetch("SELECT iso FROM countries WHERE active = true")
        country_codes = [r["iso"] for r in rows]

    country_set = set(country_codes)
    console.print(f"[cyan]Importing geonames for {len(country_set)} countries...[/]")

    zip_content = await download_geonames()
    txt_content = extract_geonames_zip(zip_content)

    results: dict[str, int] = {}
    for code in sorted(country_set):
        cities = list(parse_geonames_file(txt_content, {code}))
        if not cities:
            results[code] = 0
            continue

        rows_data = [
            (c["country"], c["label"], c["slug"], c["population"], c["latitude"], c["longitude"])
            for c in cities
        ]
        await conn.executemany(
            """
            INSERT INTO cities (country, label, slug, population, latitude, longitude)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (country, slug) DO UPDATE SET
                label = EXCLUDED.label,
                population = EXCLUDED.population,
                latitude = EXCLUDED.latitude,
                longitude = EXCLUDED.longitude
            """,
            rows_data,
        )
        results[code] = len(rows_data)
        console.print(f"  {code}: {len(rows_data)} cities")

    return results
