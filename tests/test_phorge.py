"""Tests for agency_audit.phorge: Conduit client, task search and creation, title, description."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from agency_audit import phorge
from agency_audit.config import settings
from agency_audit.phorge import ConduitClient, ConduitError, PhorgeTask

SOFIA = {"label": "Sofia", "country_label": "Bulgaria", "population": 1_200_000}
PLOVDIV = {"label": "Plovdiv", "country_label": "Bulgaria", "population": 340_000}
VARNA = {"label": "Varna", "country_label": "Bulgaria", "population": 330_000}

FULL_AUDIT = {
    "robots_txt_allows": False,
    "has_anti_scraping": True,
    "anti_scraping_details": ["Cloudflare challenge"],
    "has_api": True,
    "api_url": "https://agency.bg/wp-json",
    "property_count": 120,
    "has_property_map": False,
    "listings_have_prices": True,
    "listings_have_locations": True,
    "listings_have_images": True,
    "ssl_valid": True,
    "response_time_ms": 412.6,
    "language": "bg",
    "listing_quality_score": 0.85,
    "technology_stack": ["WordPress", "PHP"],
    "notes": "Listings behind a search form",
}


def _website(**overrides: Any) -> dict[str, Any]:
    website: dict[str, Any] = {
        "id": 7,
        "url": "https://www.agency.bg/?utm_source=gmb",
        "label": "Agency BG",
        "score": 64,
        "audit_status": "audited",
        "last_audited_at": datetime(2026, 11, 18, 9, 30),
        "maps_place_id": "ChIJplace",
        "address": "1 Vitosha Blvd, Sofia",
        "phone": "+359 2 123 456",
        "audit_data": FULL_AUDIT,
    }
    website.update(overrides)
    return website


# --- Title ---------------------------------------------------------------------


def test_title_with_label() -> None:
    assert phorge.task_title(_website(), [SOFIA]) == "Scrape Agency BG (Sofia, Bulgaria)"


def test_title_without_label_uses_host() -> None:
    assert phorge.task_title(_website(label=None), [SOFIA]) == "Scrape agency.bg (Sofia, Bulgaria)"


def test_title_with_several_cities_uses_largest_population() -> None:
    assert phorge.task_title(_website(), [VARNA, SOFIA, PLOVDIV]) == (
        "Scrape Agency BG (Sofia, Bulgaria)"
    )


def test_title_without_city() -> None:
    assert phorge.task_title(_website(), []) == "Scrape Agency BG"


# --- Description ---------------------------------------------------------------


def test_description_puts_warnings_first() -> None:
    text = phorge.task_description(_website(), [SOFIA, PLOVDIV], "http://dash/website/7")
    blocks = text.split("\n\n")
    assert blocks[0] == "WARNING: robots.txt disallows scraping."
    assert blocks[1] == "WARNING: Anti-scraping detected: Cloudflare challenge."
    assert blocks[2] == "== Website =="


def test_description_contains_all_facts() -> None:
    text = phorge.task_description(_website(), [SOFIA, PLOVDIV], "http://dash/website/7")
    for line in [
        "- URL: https://www.agency.bg/?utm_source=gmb",
        "- Dashboard: http://dash/website/7",
        "- Address: 1 Vitosha Blvd, Sofia",
        "- Phone: +359 2 123 456",
        "- Google Maps: https://www.google.com/maps/place/?q=place_id:ChIJplace",
        "- Sofia, Bulgaria",
        "- Plovdiv, Bulgaria",
        "- Score: 64",
        "- Last audit: 2026-11-18",
        "- robots.txt: disallows scraping",
        "- Anti-scraping: detected",
        "- API: yes (https://agency.bg/wp-json)",
        "- Property count: 120",
        "- Property map: no",
        "- Listings with prices: yes",
        "- Listings with locations: yes",
        "- Listings with images: yes",
        "- SSL: valid",
        "- Response time: 413 ms",
        "- Language: bg",
        "- Listing quality score: 0.85",
        "- Technology stack: WordPress, PHP",
        "- Notes: Listings behind a search form",
    ]:
        assert line in text.splitlines(), line


def test_description_leaves_out_empty_fields() -> None:
    website = _website(
        address=None,
        phone="",
        maps_place_id=None,
        last_audited_at=None,
        audit_data={
            "robots_txt_allows": True,
            "has_anti_scraping": False,
            "has_api": False,
            "api_url": None,
            "language": None,
            "technology_stack": [],
            "notes": "",
            "response_time_ms": None,
        },
    )
    text = phorge.task_description(website, [], "http://dash/website/7")
    assert "WARNING" not in text
    for absent in [
        "Address",
        "Phone",
        "Google Maps",
        "Cities",
        "Last audit",
        "Language",
        "Technology stack",
        "Notes",
        "Response time",
        "Property count",
    ]:
        assert absent not in text, absent
    assert text.startswith("== Website ==")
    assert "- robots.txt: allows scraping" in text
    assert "- API: no" in text


# --- Conduit client ------------------------------------------------------------


class FakeConduit:
    """Answers Conduit requests with queued responses and records the requests."""

    def __init__(self) -> None:
        self.responses: list[httpx.Response] = []
        self.requests: list[httpx.Request] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    def client(self, base_url: str = "https://phorge.test") -> ConduitClient:
        return ConduitClient(base_url, "tok", transport=httpx.MockTransport(self._handle))

    def form(self, index: int = 0) -> httpx.QueryParams:
        return httpx.QueryParams(self.requests[index].content.decode())


@pytest.fixture
def fake_conduit() -> FakeConduit:
    return FakeConduit()


async def test_call_posts_form_encoded_token_and_parameters(fake_conduit: FakeConduit) -> None:
    fake_conduit.responses.append(
        httpx.Response(200, json={"result": {"data": []}, "error_code": None})
    )

    await fake_conduit.client("https://phorge.test/").call(
        "maniphest.search", {"constraints": {"projects": ["PHID-PROJ-x"], "query": "a.bg"}}
    )

    assert str(fake_conduit.requests[0].url) == "https://phorge.test/api/maniphest.search"
    form = fake_conduit.form()
    assert form["api.token"] == "tok"
    assert form["constraints[projects][0]"] == "PHID-PROJ-x"
    assert form["constraints[query]"] == "a.bg"


async def test_call_raises_on_error_code(fake_conduit: FakeConduit) -> None:
    fake_conduit.responses.append(
        httpx.Response(
            200,
            json={"result": None, "error_code": "ERR-INVALID-AUTH", "error_info": "bad token"},
        )
    )
    with pytest.raises(ConduitError, match="ERR-INVALID-AUTH"):
        await fake_conduit.client().call("maniphest.search", {})


async def test_call_raises_on_non_json(fake_conduit: FakeConduit) -> None:
    fake_conduit.responses.append(httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(ConduitError, match="ERR-NOT-JSON"):
        await fake_conduit.client().call("maniphest.search", {})


async def test_maniphest_edit_returns_task(fake_conduit: FakeConduit) -> None:
    fake_conduit.responses.append(
        httpx.Response(
            200,
            json={"result": {"object": {"id": 1500, "phid": "PHID-TASK-n"}}, "error_code": None},
        )
    )
    task = await fake_conduit.client().maniphest_edit(
        [{"type": "projects.add", "value": ["PHID-PROJ-x"]}]
    )
    assert task == PhorgeTask(1500, "PHID-TASK-n")
    form = fake_conduit.form()
    assert form["transactions[0][type]"] == "projects.add"
    assert form["transactions[0][value][0]"] == "PHID-PROJ-x"


# --- Search and creation -------------------------------------------------------


def _search_hit(task_id: int, name: str, description: str = "") -> dict[str, Any]:
    return {
        "id": task_id,
        "phid": f"PHID-TASK-{task_id}",
        "fields": {"name": name, "description": {"raw": description}},
    }


def test_website_host_strips_www() -> None:
    assert phorge.website_host("https://WWW.Agency.bg/x?y=1") == "agency.bg"


async def test_find_tasks_keeps_hits_mentioning_host() -> None:
    client = MagicMock()
    client.maniphest_search = AsyncMock(
        return_value=[
            _search_hit(1, "Scrape https://www.agency.bg"),
            _search_hit(2, "Scrape other", "Site: https://agency.bg/listings"),
            _search_hit(3, "Scrape agency-bg.com"),
        ]
    )
    found = await phorge.find_tasks(client, "agency.bg")
    assert found == [PhorgeTask(1, "PHID-TASK-1"), PhorgeTask(2, "PHID-TASK-2")]
    client.maniphest_search.assert_awaited_once_with(
        {"projects": [settings.phorge_project_phid], "query": "agency.bg"}
    )


async def test_create_task_sends_four_transactions() -> None:
    client = MagicMock()
    client.maniphest_edit = AsyncMock(return_value=PhorgeTask(9, "PHID-TASK-9"))
    task = await phorge.create_task(client, "Scrape X", "desc")
    assert task == PhorgeTask(9, "PHID-TASK-9")
    client.maniphest_edit.assert_awaited_once_with(
        [
            {"type": "title", "value": "Scrape X"},
            {"type": "description", "value": "desc"},
            {"type": "projects.add", "value": [settings.phorge_project_phid]},
            {"type": "custom.repository", "value": settings.phorge_repository},
        ]
    )
