"""Fixture-driven unit tests for AuditData.to_dict() byte-contract.

Asserts every key consumed by cli.py:207-286 and orchestrator.py:373-379.
No network, no database — pure unit tests.
"""

from __future__ import annotations

import json

import pytest

from agency_audit.audit.models import (
    AntiScrapingResult,
    ApiDetectionResult,
    AuditData,
    ListingQualityResult,
    PropertyCountResult,
    RobotsResult,
    TechStackResult,
)


class TestAuditDataToDictContract:
    """Fixture-driven unit tests for AuditData.to_dict() byte-contract.

    Asserts every key consumed by cli.py:207-286 and orchestrator.py:373-379.
    No network, no database — pure unit tests.
    """

    # -- fixtures ------------------------------------------------------------

    @pytest.fixture
    def populated_audit(self) -> AuditData:
        """Fully populated AuditData with known values for every field."""
        return AuditData(
            url="https://realestate.example.com",
            robots=RobotsResult(
                fetched=True,
                allows_scraping=True,
                crawl_delay=2.5,
                sitemap_urls=[
                    "https://example.com/sitemap.xml",
                    "https://example.com/sitemap2.xml",
                ],
                raw_content="User-agent: *\nAllow: /\n",
                error=None,
            ),
            anti_scraping=AntiScrapingResult(
                detected=True,
                cloudflare=True,
                recaptcha=True,
                bot_detection_headers=True,
                js_only_rendering=False,
                details=["cloudflare", "recaptcha", "sucuri"],
            ),
            api_detection=ApiDetectionResult(
                detected=True,
                api_type="graphql",
                api_url="https://example.com/graphql",
                endpoints_found=["/graphql", "/api/v1/listings"],
            ),
            property_count=PropertyCountResult(
                count=2500,
                source="listing_page",
                confidence=0.85,
            ),
            listing_quality=ListingQualityResult(
                has_structured_data=True,
                has_images=True,
                has_descriptions=True,
                has_prices=True,
                has_locations=True,
                has_property_map=True,
                quality_score=1.0,
            ),
            tech_stack=TechStackResult(
                framework="WordPress",
                hosting="WP Engine",
                cdn="Cloudflare",
                technologies=["WordPress", "jQuery", "Bootstrap"],
            ),
            response_time_ms=350.0,
            ssl_valid=True,
            language="en",
            notes="All checks passed",
            score=88,
            score_breakdown={
                "robots_allows": 10,
                "has_api": 15,
                "property_count_1000+": 30,
            },
        )

    @pytest.fixture
    def default_audit(self) -> AuditData:
        """Minimal AuditData with default field values."""
        return AuditData()

    # -- CLI field contract --------------------------------------------------
    # Each test asserts a field consumed by cli.py:207-286.

    def test_cli_robots_allows_scraping(self, populated_audit):
        """cli.py:207 reads result.robots.allows_scraping → robots_txt_allows."""
        data = populated_audit.to_dict()
        assert data["robots_txt_allows"] is True
        assert isinstance(data["robots_txt_allows"], bool)

    def test_cli_robots_crawl_delay(self, populated_audit):
        """cli.py:209-210 reads result.robots.crawl_delay → robots_crawl_delay."""
        data = populated_audit.to_dict()
        assert data["robots_crawl_delay"] == 2.5

    def test_cli_robots_sitemap_urls(self, populated_audit):
        """cli.py:211-212 reads result.robots.sitemap_urls → robots_sitemap_urls."""
        data = populated_audit.to_dict()
        assert len(data["robots_sitemap_urls"]) == 2
        assert "https://example.com/sitemap.xml" in data["robots_sitemap_urls"]

    def test_cli_anti_scraping_detected(self, populated_audit):
        """cli.py:214-218 reads result.anti_scraping.detected → has_anti_scraping."""
        data = populated_audit.to_dict()
        assert data["has_anti_scraping"] is True
        assert isinstance(data["has_anti_scraping"], bool)

    def test_cli_anti_scraping_cloudflare(self, populated_audit):
        """cli.py:215-216 reads result.anti_scraping.cloudflare → cloudflare."""
        data = populated_audit.to_dict()
        assert data["cloudflare"] is True

    def test_cli_anti_scraping_recaptcha(self, populated_audit):
        """cli.py:217-218 reads result.anti_scraping.recaptcha → recaptcha."""
        data = populated_audit.to_dict()
        assert data["recaptcha"] is True

    def test_cli_api_detection_api_type(self, populated_audit):
        """cli.py:222 reads result.api_detection.api_type → api_type."""
        data = populated_audit.to_dict()
        assert data["api_type"] == "graphql"

    def test_cli_api_detection_api_url(self, populated_audit):
        """cli.py:223 reads result.api_detection.api_url → api_url."""
        data = populated_audit.to_dict()
        assert data["api_url"] == "https://example.com/graphql"

    def test_cli_property_count_value(self, populated_audit):
        """cli.py:227 reads result.property_count.count → property_count."""
        data = populated_audit.to_dict()
        assert data["property_count"] == 2500
        assert isinstance(data["property_count"], int)

    def test_cli_property_count_source(self, populated_audit):
        """cli.py:227 reads result.property_count.source → property_count_source."""
        data = populated_audit.to_dict()
        assert data["property_count_source"] == "listing_page"
        assert isinstance(data["property_count_source"], str)

    def test_cli_property_count_confidence(self, populated_audit):
        """cli.py:228 reads result.property_count.confidence → property_count_confidence."""
        data = populated_audit.to_dict()
        assert data["property_count_confidence"] == pytest.approx(0.85)
        assert isinstance(data["property_count_confidence"], float)

    def test_cli_listing_quality_fields(self, populated_audit):
        """cli.py:231-236 reads listing_quality bools → to_dict keys."""
        data = populated_audit.to_dict()
        assert data["has_structured_data"] is True
        assert data["listings_have_prices"] is True
        assert data["listings_have_locations"] is True
        assert data["listings_have_images"] is True
        assert data["has_property_map"] is True

    def test_cli_tech_stack_framework(self, populated_audit):
        """cli.py:237 reads result.tech_stack.framework → framework."""
        data = populated_audit.to_dict()
        assert data["framework"] == "WordPress"

    def test_cli_tech_stack_cdn(self, populated_audit):
        """cli.py:238 reads result.tech_stack.cdn → cdn."""
        data = populated_audit.to_dict()
        assert data["cdn"] == "Cloudflare"

    def test_cli_tech_stack_hosting(self, populated_audit):
        """cli.py:239 reads result.tech_stack.hosting → hosting."""
        data = populated_audit.to_dict()
        assert data["hosting"] == "WP Engine"

    def test_cli_tech_stack_technologies(self, populated_audit):
        """cli.py:242-244 reads result.tech_stack.technologies → technology_stack."""
        data = populated_audit.to_dict()
        assert data["technology_stack"] == ["WordPress", "jQuery", "Bootstrap"]
        assert isinstance(data["technology_stack"], list)

    def test_cli_response_time_ms(self, populated_audit):
        """cli.py:248 reads result.response_time_ms → response_time_ms."""
        data = populated_audit.to_dict()
        assert data["response_time_ms"] == 350.0

    def test_cli_ssl_valid(self, populated_audit):
        """cli.py:250 reads result.ssl_valid → ssl_valid."""
        data = populated_audit.to_dict()
        assert data["ssl_valid"] is True
        assert isinstance(data["ssl_valid"], bool)

    def test_cli_language(self, populated_audit):
        """cli.py:251 reads result.language → language."""
        data = populated_audit.to_dict()
        assert data["language"] == "en"

    def test_cli_score(self, populated_audit):
        """cli.py:255 reads result.score → score."""
        data = populated_audit.to_dict()
        assert data["score"] == 88
        assert isinstance(data["score"], int)

    def test_cli_score_breakdown(self, populated_audit):
        """cli.py:256 reads result.score_breakdown → score_breakdown."""
        data = populated_audit.to_dict()
        assert data["score_breakdown"] == {
            "robots_allows": 10,
            "has_api": 15,
            "property_count_1000+": 30,
        }
        assert isinstance(data["score_breakdown"], dict)

    # -- orchestrator contract (orchestrator.py:373-379) --------------------

    def test_orchestrator_to_dict_is_jsonb_serializable(self, populated_audit):
        """orchestrator.py:374 persists json.dumps(result.to_dict()) as JSONB."""
        data = populated_audit.to_dict()
        serialized = json.dumps(data)
        assert serialized is not None
        recovered = json.loads(serialized)
        assert recovered["url"] == "https://realestate.example.com"
        assert recovered["score"] == 88
        assert recovered["robots_txt_allows"] is True

    def test_orchestrator_score_persisted_separately(self, populated_audit):
        """orchestrator.py:375 persists result.score alongside to_dict() JSONB."""
        data = populated_audit.to_dict()
        assert data["score"] == populated_audit.score

    def test_orchestrator_all_keys_present(self, populated_audit):
        """orchestrator.py:374 persists full to_dict() — all keys must exist."""
        data = populated_audit.to_dict()
        expected_keys = {
            "url", "robots_txt_allows", "robots_txt_fetched",
            "robots_crawl_delay", "robots_sitemap_urls",
            "has_anti_scraping", "anti_scraping_details",
            "cloudflare", "recaptcha", "has_api", "api_type", "api_url",
            "api_endpoints", "property_count", "property_count_source",
            "property_count_confidence", "has_structured_data",
            "listings_have_images", "listings_have_descriptions",
            "listings_have_prices", "listings_have_locations",
            "has_property_map", "listing_quality_score",
            "technology_stack", "framework", "hosting", "cdn",
            "response_time_ms", "ssl_valid", "language", "notes",
            "score", "score_breakdown",
        }
        assert set(data.keys()) == expected_keys

    # -- default/null handling ----------------------------------------------

    def test_default_none_fields(self, default_audit):
        """Default AuditData fields with None values emit None in to_dict()."""
        data = default_audit.to_dict()
        assert data["framework"] is None
        assert data["hosting"] is None
        assert data["cdn"] is None
        assert data["api_type"] is None
        assert data["api_url"] is None
        assert data["response_time_ms"] is None
        assert data["language"] is None

    def test_default_empty_collections(self, default_audit):
        """Default AuditData fields with empty lists emit [] in to_dict()."""
        data = default_audit.to_dict()
        assert data["robots_sitemap_urls"] == []
        assert data["anti_scraping_details"] == []
        assert data["api_endpoints"] == []
        assert data["technology_stack"] == []

    def test_default_score_and_score_breakdown(self, default_audit):
        """Default AuditData has score=0 and empty score_breakdown dict."""
        data = default_audit.to_dict()
        assert data["score"] == 0
        assert data["score_breakdown"] == {}

    def test_default_bool_fields(self, default_audit):
        """Default AuditData bool fields emit their default values."""
        data = default_audit.to_dict()
        assert data["robots_txt_allows"] is True
        assert data["robots_txt_fetched"] is False
        assert data["has_anti_scraping"] is False
        assert data["cloudflare"] is False
        assert data["recaptcha"] is False
        assert data["has_api"] is False
        assert data["ssl_valid"] is True
        assert data["has_structured_data"] is False
        assert data["has_property_map"] is False

    def test_default_empty_url_and_notes(self, default_audit):
        """Default AuditData has empty string for url and notes."""
        data = default_audit.to_dict()
        assert data["url"] == ""
        assert data["notes"] == ""

    # -- JSON serialization edge cases --------------------------------------

    def test_to_dict_with_special_characters(self):
        """to_dict() should handle URLs/notes with special characters."""
        audit = AuditData(
            url="https://example.com/path?q=foo&bar=baz",
            notes='Has "quotes" and\nnewlines',
            language="bg",
        )
        data = audit.to_dict()
        serialized = json.dumps(data)
        recovered = json.loads(serialized)
        assert recovered["url"] == "https://example.com/path?q=foo&bar=baz"
        assert recovered["notes"] == 'Has "quotes" and\nnewlines'
        assert recovered["language"] == "bg"

    def test_to_dict_with_unicode(self):
        """to_dict() should handle Unicode in URL and language fields."""
        audit = AuditData(
            url="https://example.com/имот/продажба",
            language="bg",
            notes="Български",
        )
        data = audit.to_dict()
        serialized = json.dumps(data)
        recovered = json.loads(serialized)
        assert recovered["url"] == "https://example.com/имот/продажба"
        assert recovered["language"] == "bg"
        assert recovered["notes"] == "Български"

    def test_to_dict_response_time_ms_null(self):
        """response_time_ms=None should serialize as null (JSON null)."""
        audit = AuditData(response_time_ms=None)
        data = audit.to_dict()
        serialized = json.dumps(data)
        recovered = json.loads(serialized)
        assert recovered["response_time_ms"] is None
