"""Phorge tasks for scraping a website: the Conduit client, the task title and description."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from agency_audit.config import settings

CONDUIT_TIMEOUT = 30.0


class ConduitError(Exception):
    """A Conduit call returned an ``error_code``."""

    def __init__(self, method: str, code: str, info: str | None) -> None:
        super().__init__(f"{method}: {code}: {info or ''}".rstrip(": "))
        self.code = code
        self.info = info


@dataclass(frozen=True)
class PhorgeTask:
    id: int
    phid: str


def _flatten(prefix: str, value: Any) -> list[tuple[str, str]]:
    """Encode *value* as PHP-style form fields, as Conduit reads them."""
    if isinstance(value, Mapping):
        return [p for k, v in value.items() for p in _flatten(f"{prefix}[{k}]", v)]
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [p for i, v in enumerate(value) for p in _flatten(f"{prefix}[{i}]", v)]
    if isinstance(value, bool):
        return [(prefix, "1" if value else "0")]
    return [(prefix, str(value))]


class ConduitClient:
    """Calls the Conduit API of a Phorge instance."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = CONDUIT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._transport = transport

    async def call(self, method: str, params: Mapping[str, Any]) -> Any:
        """Call *method* and return its ``result``; raise :class:`ConduitError` on an error."""
        data = {"api.token": self._token}
        for key, value in params.items():
            data.update(_flatten(key, value))
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            response = await client.post(f"{self._base_url}/api/{method}", data=data)
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError as exc:
            raise ConduitError(method, "ERR-NOT-JSON", response.text[:200]) from exc
        if body.get("error_code"):
            raise ConduitError(method, body["error_code"], body.get("error_info"))
        return body["result"]

    async def maniphest_search(self, constraints: Mapping[str, Any]) -> list[dict[str, Any]]:
        result = await self.call("maniphest.search", {"constraints": constraints})
        data: list[dict[str, Any]] = result["data"]
        return data

    async def maniphest_edit(self, transactions: Sequence[Mapping[str, Any]]) -> PhorgeTask:
        result = await self.call("maniphest.edit", {"transactions": transactions})
        return PhorgeTask(id=int(result["object"]["id"]), phid=result["object"]["phid"])


def conduit_client() -> ConduitClient:
    return ConduitClient(settings.phorge_url, settings.phorge_api_token)


def task_url(task_id: int) -> str:
    return f"{settings.phorge_url.rstrip('/')}/T{task_id}"


def website_host(url: str) -> str:
    """Host of *url*, lowercase, without ``www.``."""
    host = (urlsplit(url.strip()).hostname or "").lower()
    return host.removeprefix("www.")


async def find_tasks(client: ConduitClient, host: str) -> list[PhorgeTask]:
    """Tasks of the scraper project whose title or description contains *host*."""
    found = await client.maniphest_search(
        {"projects": [settings.phorge_project_phid], "query": host}
    )
    matches = []
    for task in found:
        fields = task.get("fields", {})
        title = fields.get("name") or ""
        description = (fields.get("description") or {}).get("raw") or ""
        if host in title.lower() or host in description.lower():
            matches.append(PhorgeTask(id=int(task["id"]), phid=task["phid"]))
    return matches


async def create_task(client: ConduitClient, title: str, description: str) -> PhorgeTask:
    return await client.maniphest_edit(
        [
            {"type": "title", "value": title},
            {"type": "description", "value": description},
            {"type": "projects.add", "value": [settings.phorge_project_phid]},
            {"type": "custom.repository", "value": settings.phorge_repository},
        ]
    )


# --- Task content --------------------------------------------------------------


def _main_city(cities: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The linked city with the largest population."""
    return max(cities, key=lambda c: c["population"], default=None)


def task_title(website: Mapping[str, Any], cities: Sequence[Mapping[str, Any]]) -> str:
    name = website.get("label") or website_host(website["url"])
    city = _main_city(cities)
    if city is None:
        return f"Scrape {name}"
    return f"Scrape {name} ({city['label']}, {city['country_label']})"


def _has_value(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def task_description(
    website: Mapping[str, Any],
    cities: Sequence[Mapping[str, Any]],
    dashboard_url: str,
) -> str:
    """The Remarkup description: warnings, website facts, cities, score, audit results."""
    ad: Mapping[str, Any] = website.get("audit_data") or {}
    blocks: list[str] = []

    if ad.get("robots_txt_allows") is False:
        blocks.append("WARNING: robots.txt disallows scraping.")
    if ad.get("has_anti_scraping"):
        details = ", ".join(ad.get("anti_scraping_details") or [])
        blocks.append(f"WARNING: Anti-scraping detected{f': {details}' if details else ''}.")

    def section(heading: str, items: list[tuple[str, Any]]) -> None:
        lines = [f"- {label}: {value}" for label, value in items if _has_value(value)]
        if lines:
            blocks.append(f"== {heading} ==\n\n" + "\n".join(lines))

    place_id = website.get("maps_place_id")
    section(
        "Website",
        [
            ("URL", website.get("url")),
            ("Dashboard", dashboard_url),
            ("Address", website.get("address")),
            ("Phone", website.get("phone")),
            (
                "Google Maps",
                f"https://www.google.com/maps/place/?q=place_id:{place_id}" if place_id else None,
            ),
        ],
    )

    city_lines = [f"- {c['label']}, {c['country_label']}" for c in cities]
    if city_lines:
        blocks.append("== Cities ==\n\n" + "\n".join(city_lines))

    last_audited: datetime | None = website.get("last_audited_at")
    section(
        "Audit",
        [
            ("Score", website.get("score")),
            ("Last audit", last_audited.strftime("%Y-%m-%d") if last_audited else None),
        ],
    )

    def flag(key: str, yes: str, no: str) -> str | None:
        value = ad.get(key)
        if value is None:
            return None
        return yes if value else no

    api = flag("has_api", "yes", "no")
    if ad.get("has_api") and ad.get("api_url"):
        api = f"yes ({ad['api_url']})"
    response_time = ad.get("response_time_ms")
    section(
        "Audit results",
        [
            ("robots.txt", flag("robots_txt_allows", "allows scraping", "disallows scraping")),
            ("Anti-scraping", flag("has_anti_scraping", "detected", "not detected")),
            ("API", api),
            ("Property count", ad.get("property_count")),
            ("Property map", flag("has_property_map", "yes", "no")),
            ("Listings with prices", flag("listings_have_prices", "yes", "no")),
            ("Listings with locations", flag("listings_have_locations", "yes", "no")),
            ("Listings with images", flag("listings_have_images", "yes", "no")),
            ("SSL", flag("ssl_valid", "valid", "invalid")),
            ("Response time", f"{response_time:.0f} ms" if response_time is not None else None),
            ("Language", ad.get("language")),
            ("Listing quality score", ad.get("listing_quality_score")),
            ("Technology stack", ", ".join(ad.get("technology_stack") or [])),
            ("Notes", ad.get("notes")),
        ],
    )

    return "\n\n".join(blocks)
