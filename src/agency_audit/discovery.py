"""
Google Maps Places discovery pipeline for agency-audit.

Discovers real estate agencies per city using:
1. Google Maps Places API (Text Search) — primary, requires API key
2. Browser-based Google Maps scraping — fallback

Reports findings to the agency-audit MCP database.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from agency_audit.db import get_pool

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────

# Local-language query templates for real estate agencies
# Key = country ISO code, Value = list of search queries (primary first)
COUNTRY_QUERIES: dict[str, list[str]] = {
    "BG": ["Агенция за недвижими имоти", "брокер на недвижими имоти"],
    "GB": ["estate agent", "real estate agent", "property agent"],
    "IE": ["estate agent", "property agent"],
    "DE": ["Immobilienmakler", "Makler"],
    "AT": ["Immobilienmakler", "Makler"],
    "CH": ["Immobilienmakler", "Immobilienbüro"],
    "FR": ["agent immobilier", "agence immobilière"],
    "IT": ["agenzia immobiliare", "immobiliare"],
    "ES": ["inmobiliaria", "agencia inmobiliaria"],
    "PT": ["imobiliária", "agente imobiliário"],
    "NL": ["makelaar", "vastgoedmakelaar"],
    "BE": ["makelaar", "immobiliënkantoor"],
    "LU": ["immobilienmakler", "makler"],
    "PL": ["biuro nieruchomości", "agenci nieruchomości"],
    "CZ": ["realitní kancelář", "makléř"],
    "SK": ["realitná kancelária", "maklér"],
    "HU": ["ingatlanügynökség", "ingatlanközvetítő"],
    "RO": ["agentie imobiliara", "imobiliare"],
    "HR": ["agencija za nekretnine", "posrednik u prometu nekretnina"],
    "RS": ["agencija za nekretnine", "posrednik"],
    "BA": ["agencija za nekretnine", "posrednik"],
    "SI": ["nepremičninska agencija", "nepremičninski posrednik"],
    "ME": ["agencija za nekretnine"],
    "MK": ["агенција за недвижности"],
    "AL": ["agjenci imobiliare", "ndërmjetës imobiliar"],
    "XK": ["agenci për pasuri të paluajtshme"],
    "GR": ["μεσιτικό γραφείο", "κτηματομεσιτικό γραφείο"],
    "CY": ["μεσιτικό γραφείο", "estate agent"],
    "MT": ["aġenzija tal-proprjetà", "real estate agent"],
    "TR": ["emlakçı", "gayrimenkul danışmanı"],
    "DK": ["ejendomsmægler", "ejendomsmæglerfirma"],
    "NO": ["eiendomsmegler", "megler"],
    "SE": ["fastighetsmäklare", "mäklare"],
    "FI": ["kiinteistönvälittäjä", "välittäjä"],
    "EE": ["kinnisvaramaakler", "kinnisvarabüroo"],
    "LV": ["nekustamo īpašumu aģents", "mākleris"],
    "LT": ["nekilnojamojo turto agentūra", "makleris"],
    "MD": ["agentie imobiliara", "imobiliare"],
    "UA": ["агентство нерухомості", "рієлтор"],
    "BY": ["агентство недвижимости", "риелтор"],
    "RU": ["агентство недвижимости", "риелтор"],
    "IS": ["fasteignasali", "fasteignamiðlari"],
    "LI": ["immobilienmakler", "makler"],
    "SM": ["agenzia immobiliare"],
    "MC": ["agent immobilier", "agence immobilière"],
    "VA": ["agenzia immobiliare"],
}

# Default Google Maps Text Search radius in meters
DEFAULT_RADIUS = 10000


@dataclass
class PlaceResult:
    """A discovered real estate agency from Google Maps."""
    place_id: str
    name: str
    formatted_address: str | None = None
    phone: str | None = None
    website: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    rating: float | None = None
    user_ratings_total: int | None = None


# ──────────────────────────────────────────────────────────────────────
# Places API Client
# ──────────────────────────────────────────────────────────────────────


class PlacesAPIClient:
    """HTTP client for Google Maps Places API (New) Text Search.

    Uses the Places API (New) endpoint:
      POST https://places.googleapis.com/v1/places:searchText

    Requires an API key set via GOOGLE_MAPS_API_KEY env var or
    AGENCY_AUDIT_GOOGLE_MAPS_API_KEY env var.
    """

    BASE_URL = "https://places.googleapis.com/v1/places:searchText"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or self._load_api_key()
        self._client: httpx.AsyncClient | None = None
        self._request_count = 0
        self._last_request_time = 0.0

    def _load_api_key(self) -> str:
        """Load API key from environment."""
        import os
        key = os.environ.get("AGENCY_AUDIT_GOOGLE_MAPS_API_KEY") or \
              os.environ.get("GOOGLE_MAPS_API_KEY") or \
              os.environ.get("GOOGLE_PLACES_API_KEY") or ""
        if not key:
            logger.warning("No Google Maps API key found. Set GOOGLE_MAPS_API_KEY or AGENCY_AUDIT_GOOGLE_MAPS_API_KEY.")
        return key

    @property
    def available(self) -> bool:
        """Whether the API client can make requests."""
        return bool(self.api_key)

    async def _ensure_client(self):
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": self.api_key,
                    "X-Goog-FieldMask": (
                        "places.id,places.displayName,places.formattedAddress,"
                        "places.internationalPhoneNumber,places.websiteUri,"
                        "places.location,places.rating,places.userRatingCount"
                    ),
                },
            )
        return self._client

    async def search_text(
        self,
        query: str,
        location_bias: tuple[float, float] | None = None,
        radius: int = DEFAULT_RADIUS,
        max_results: int = 60,
    ) -> list[PlaceResult]:
        """Search Google Maps Places API for a text query.

        Handles pagination up to max_results (default 60, max 60 for
        Text Search, but we paginate across multiple requests).

        Returns up to max_results PlaceResult objects.
        """
        await self._rate_limit()

        client = await self._ensure_client()
        results: list[PlaceResult] = []
        next_page_token: str | None = None

        while len(results) < max_results:
            body: dict[str, Any] = {"textQuery": query, "pageSize": min(20, max_results - len(results))}
            if location_bias:
                body["locationBias"] = {
                    "circle": {
                        "center": {"latitude": location_bias[0], "longitude": location_bias[1]},
                        "radius": radius,
                    }
                }
            if next_page_token:
                body["pageToken"] = next_page_token
                # Rate limit between pages
                await asyncio.sleep(0.5)

            try:
                resp = await client.post(self.BASE_URL, json=body)
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"Places API error {e.response.status_code}: {e.response.text}")
                raise
            except httpx.TimeoutException:
                logger.warning("Places API request timed out")
                break

            places = data.get("places", [])
            for p in places:
                loc = p.get("location", {})
                results.append(PlaceResult(
                    place_id=p.get("id", ""),
                    name=p.get("displayName", {}).get("text", ""),
                    formatted_address=p.get("formattedAddress"),
                    phone=p.get("internationalPhoneNumber"),
                    website=p.get("websiteUri"),
                    latitude=loc.get("latitude"),
                    longitude=loc.get("longitude"),
                    rating=p.get("rating"),
                    user_ratings_total=p.get("userRatingCount"),
                ))

            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break

        logger.info(f"Places API search '{query}': got {len(results)} results")
        return results[:max_results]

    async def _rate_limit(self):
        """Simple rate limiting — max 5 QPS for Text Search."""
        now = time.monotonic()
        elapsed = now - self._last_request_time
        min_interval = 0.2  # 5 QPS
        if elapsed < min_interval:
            await asyncio.sleep(min_interval - elapsed)
        self._last_request_time = time.monotonic()
        self._request_count += 1

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None


# ──────────────────────────────────────────────────────────────────────
# Discovery Orchestrator
# ──────────────────────────────────────────────────────────────────────


class DiscoveryPipeline:
    """Orchestrates the discovery of real estate agencies across cities.

    Fetches cities via MCP get_next_city, searches Google Maps for
    agencies, and reports findings via MCP report_website.
    """

    def __init__(
        self,
        places_client: PlacesAPIClient | None = None,
        use_browser_fallback: bool = False,
        batch_size: int = 10,
    ):
        self.places = places_client or PlacesAPIClient()
        self.use_browser_fallback = use_browser_fallback
        self.batch_size = batch_size
        self._pool = None

    async def _get_pool(self):
        if self._pool is None:
            self._pool = await get_pool()
        return self._pool

    async def query_for_country(self, country_iso: str) -> list[str]:
        """Get search query templates for a country."""
        return COUNTRY_QUERIES.get(country_iso, ["real estate agent"])

    async def discover_city(
        self,
        city_id: int,
        city_label: str,
        city_slug: str,
        country_iso: str,
        latitude: float | None,
        longitude: float | None,
    ) -> int:
        """Discover real estate agencies for a single city.

        Returns the number of agencies found and reported.
        """
        logger.info(f"Discovering agencies in {city_label}, {country_iso}")

        queries = await self.query_for_country(country_iso)
        location = (latitude, longitude) if latitude and longitude else None
        found_places: list[PlaceResult] = []
        seen_place_ids: set[str] = set()

        for query in queries:
            # Build full search query
            search_query = f"{query} {city_label}"

            try:
                if self.places.available:
                    places = await self.places.search_text(
                        query=search_query,
                        location_bias=location,
                        max_results=60,
                    )
                else:
                    logger.warning(f"Places API not available for {city_label}")
                    break

                for p in places:
                    if p.place_id and p.place_id not in seen_place_ids:
                        # Filter: must have a name and either website or place_id
                        if p.name:
                            found_places.append(p)
                            seen_place_ids.add(p.place_id)

                logger.info(f"Query '{search_query}': found {len(places)} places, {len(found_places)} new")

            except Exception as e:
                logger.error(f"Error searching '{search_query}': {e}")
                continue

            # If we already have enough results from the primary query, stop
            if len(found_places) >= 20:
                break

        # Report to database
        reported = 0
        pool = await self._get_pool()
        for place in found_places:
            url = place.website or f"https://maps.google.com/?cid={place.place_id}"
            async with pool.acquire() as conn:
                # Insert or find website
                existing = await conn.fetchrow("SELECT id FROM websites WHERE maps_place_id = $1", place.place_id)
                if existing:
                    website_id = existing["id"]
                    created = False
                else:
                    website_id = await conn.fetchval(
                        """INSERT INTO websites (url, label, maps_place_id, address, phone)
                           VALUES ($1, $2, $3, $4, $5)
                           ON CONFLICT (url) DO UPDATE SET label = EXCLUDED.label
                           RETURNING id""",
                        url,
                        place.name,
                        place.place_id,
                        place.formatted_address,
                        place.phone,
                    )
                    created = True

                # Link website to city
                await conn.execute(
                    """INSERT INTO website_cities (website_id, city_id, discovered_via)
                       VALUES ($1, $2, $3)
                       ON CONFLICT DO NOTHING""",
                    website_id,
                    city_id,
                    "google_maps",
                )

                # Log discovery
                await conn.execute(
                    """INSERT INTO discovery_log (city_id, website_id, agent, search_query, status)
                       VALUES ($1, $2, $3, $4, 'found')""",
                    city_id,
                    website_id,
                    "google_maps",
                    f"{place.name} @ {url}",
                )

                reported += 1

        # Log discovery
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE cities SET discovery_status = 'done' WHERE id = $1",
                city_id,
            )
            # Log city searched
            await conn.execute(
                """INSERT INTO discovery_log (city_id, agent, search_query, status)
                   VALUES ($1, $2, $3, 'searched')""",
                city_id, "google_maps_places_api", ",".join(queries),
            )

        logger.info(f"{city_label}: reported {reported} agencies")
        return reported

    async def run_for_countries(
        self,
        country_codes: list[str] | None = None,
        max_cities_per_country: int = 3,
    ) -> dict[str, Any]:
        """Run discovery for specified countries.

        Args:
            country_codes: List of ISO codes. If None, fetches from any country.
            max_cities_per_country: Max cities to process per country.

        Returns:
            Summary dict with stats.
        """
        pool = await self._get_pool()
        summary: dict[str, Any] = {
            "countries_processed": 0,
            "cities_processed": 0,
            "agencies_found": 0,
            "results": {},
        }

        # Get countries to process
        if country_codes:
            countries = country_codes
        else:
            rows = await pool.fetch(
                "SELECT DISTINCT country FROM cities WHERE discovery_status = 'pending'"
            )
            countries = [r["country"] for r in rows]

        for country in countries:
            country_summary = {"cities": 0, "agencies": 0}

            while country_summary["cities"] < max_cities_per_country:
                # Fetch next pending city for this country
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        """SELECT id, label, slug, population, latitude, longitude
                           FROM cities
                           WHERE country = $1 AND discovery_status = 'pending'
                           ORDER BY population DESC
                           LIMIT 1""",
                        country,
                    )
                    if row is None:
                        break

                    city_id = row["id"]
                    await conn.execute(
                        "UPDATE cities SET discovery_status = 'in_progress' WHERE id = $1",
                        city_id,
                    )

                count = await self.discover_city(
                    city_id=city_id,
                    city_label=row["label"],
                    city_slug=row["slug"],
                    country_iso=country,
                    latitude=row["latitude"],
                    longitude=row["longitude"],
                )

                country_summary["cities"] += 1
                country_summary["agencies"] += count

            summary["cities_processed"] += country_summary["cities"]
            summary["agencies_found"] += country_summary["agencies"]
            summary["results"][country] = country_summary
            if country_summary["cities"] > 0:
                summary["countries_processed"] += 1

        return summary

    async def close(self):
        if self.places:
            await self.places.close()


# ──────────────────────────────────────────────────────────────────────
# CLI helper
# ──────────────────────────────────────────────────────────────────────


async def run_discovery(
    countries: list[str] | None = None,
    max_cities: int = 3,
) -> dict[str, Any]:
    """Run the discovery pipeline and return a summary."""
    pipeline = DiscoveryPipeline(batch_size=max_cities)
    try:
        summary = await pipeline.run_for_countries(
            country_codes=countries,
            max_cities_per_country=max_cities,
        )
        return summary
    finally:
        await pipeline.close()