"""Tests for the audit pipeline — robots, anti-scraping, API detection,
property count, listing quality, tech stack, scoring, and full auditor.

These tests use synthetic HTML/headers (no network) for unit tests.
The scoring and full-pipeline tests can run offline.
"""

from __future__ import annotations

import json

import httpx
import pytest

from agency_audit.audit.anti_scraping import (
    _check_bot_detection_headers,
    _check_cloudflare_body,
    _check_cloudflare_headers,
    _check_js_only_rendering,
    _check_recaptcha,
    detect_anti_scraping,
)
from agency_audit.audit.api_detection import (
    _check_jsonld_structured_data,
    _find_api_endpoints_in_html,
)
from agency_audit.audit.auditor import audit_website
from agency_audit.audit.listing_quality import (
    _check_map,
    _check_structured_data,
)
from agency_audit.audit.models import (
    AntiScrapingResult,
    ApiDetectionResult,
    AuditData,
    ListingQualityResult,
    PropertyCountResult,
    RobotsResult,
    TechStackResult,
)
from agency_audit.audit.robots import _extract_crawl_delay, _extract_sitemaps, parse_robots_txt
from agency_audit.audit.scoring import compute_score, load_scoring_config
from agency_audit.audit.tech_stack import (
    _detect_cdn,
    _detect_framework_from_headers,
    _detect_framework_from_html,
    _detect_hosting,
    _detect_technologies,
)

# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------


class TestRobotsTxt:
    def test_parse_allows_all(self):
        content = "User-agent: *\nAllow: /\n"
        result = parse_robots_txt(content, "https://example.com")
        assert result.fetched is True
        assert result.allows_scraping is True

    def test_parse_disallows(self):
        content = "User-agent: *\nDisallow: /\n"
        result = parse_robots_txt(content, "https://example.com")
        assert result.allows_scraping is False

    def test_parse_crawl_delay(self):
        content = "User-agent: *\nCrawl-delay: 5\n"
        result = parse_robots_txt(content, "https://example.com")
        assert result.crawl_delay == 5.0

    def test_parse_crawl_delay_specific_agent(self):
        content = "User-agent: AgencyAuditBot\nCrawl-delay: 2\nUser-agent: *\nCrawl-delay: 10\n"
        result = parse_robots_txt(content, "https://example.com", "AgencyAuditBot")
        assert result.crawl_delay == 2.0

    def test_parse_sitemaps(self):
        content = (
            "User-agent: *\nAllow: /\n"
            "Sitemap: https://example.com/sitemap.xml\n"
            "Sitemap: https://example.com/sitemap2.xml\n"
        )
        result = parse_robots_txt(content, "https://example.com")
        assert len(result.sitemap_urls) == 2
        assert "https://example.com/sitemap.xml" in result.sitemap_urls

    def test_extract_crawl_delay_star(self):
        content = "User-agent: *\nCrawl-delay: 3\n"
        assert _extract_crawl_delay(content, "*") == 3.0

    def test_extract_crawl_delay_none(self):
        content = "User-agent: *\nAllow: /\n"
        assert _extract_crawl_delay(content, "*") is None

    def test_extract_sitemaps_empty(self):
        assert _extract_sitemaps("User-agent: *\nAllow: /\n") == []

    def test_empty_robots_allows_by_default(self):
        result = parse_robots_txt("", "https://example.com")
        assert result.allows_scraping is True

    def test_disallow_specific_path(self):
        content = "User-agent: *\nDisallow: /admin/\nAllow: /\n"
        result = parse_robots_txt(content, "https://example.com")
        # The base URL should still be allowed
        assert result.allows_scraping is True


class TestRobotsFetch:
    """Tests for fetch_robots_txt with mocked httpx — covers 404, 200,
    crawl-delay, sitemaps, and default-allow-when-absent."""

    async def test_fetch_200_disallow(self):
        """Fetch a robots.txt that disallows everything."""
        from agency_audit.audit.robots import fetch_robots_txt

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: /\n",
                request=request,
            )

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport, follow_redirects=True)
        try:
            result = await fetch_robots_txt("https://example.com", client=client)
            assert result.fetched is True
            assert result.allows_scraping is False
            assert result.raw_content == "User-agent: *\nDisallow: /\n"
        finally:
            await client.aclose()

    async def test_fetch_200_allow(self):
        """Fetch a robots.txt that allows everything."""
        from agency_audit.audit.robots import fetch_robots_txt

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="User-agent: *\nAllow: /\n",
                request=request,
            )

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport, follow_redirects=True)
        try:
            result = await fetch_robots_txt("https://example.com", client=client)
            assert result.fetched is True
            assert result.allows_scraping is True
        finally:
            await client.aclose()

    async def test_fetch_404_default_allow(self):
        """404/missing robots.txt → default allow, not fetched."""
        from agency_audit.audit.robots import fetch_robots_txt

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, request=request)

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport, follow_redirects=True)
        try:
            result = await fetch_robots_txt("https://example.com", client=client)
            assert result.fetched is False
            assert result.allows_scraping is True
            assert result.error is None  # 404 is not an error, just missing
        finally:
            await client.aclose()

    async def test_fetch_500_error_default_allow(self):
        """Non-404 HTTP error → default allow with error string."""
        from agency_audit.audit.robots import fetch_robots_txt

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, request=request)

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport, follow_redirects=True)
        try:
            result = await fetch_robots_txt("https://example.com", client=client)
            assert result.fetched is False
            assert result.allows_scraping is True
            assert result.error is not None
            assert "HTTP 503" in result.error
        finally:
            await client.aclose()

    async def test_fetch_connect_error_default_allow(self):
        """httpx.ConnectError → default allow with error message."""
        from agency_audit.audit.robots import fetch_robots_txt

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport, follow_redirects=True)
        try:
            result = await fetch_robots_txt("https://example.com", client=client)
            assert result.fetched is False
            assert result.allows_scraping is True
            assert result.error is not None
            assert "Connection refused" in result.error
        finally:
            await client.aclose()

    async def test_fetch_crawl_delay(self):
        """Fetch robots.txt with crawl-delay."""
        from agency_audit.audit.robots import fetch_robots_txt

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="User-agent: *\nCrawl-delay: 5\n",
                request=request,
            )

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport, follow_redirects=True)
        try:
            result = await fetch_robots_txt("https://example.com", client=client)
            assert result.crawl_delay == 5.0
        finally:
            await client.aclose()

    async def test_fetch_sitemap_urls(self):
        """Fetch robots.txt with multiple sitemap declarations."""
        from agency_audit.audit.robots import fetch_robots_txt

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=(
                    "User-agent: *\nAllow: /\n"
                    "Sitemap: https://example.com/sitemap.xml\n"
                    "Sitemap: https://example.com/sitemap2.xml\n"
                ),
                request=request,
            )

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport, follow_redirects=True)
        try:
            result = await fetch_robots_txt("https://example.com", client=client)
            assert len(result.sitemap_urls) == 2
            assert "https://example.com/sitemap.xml" in result.sitemap_urls
            assert "https://example.com/sitemap2.xml" in result.sitemap_urls
        finally:
            await client.aclose()

    async def test_fetch_empty_content(self):
        """Empty robots.txt should be allowed by default."""
        from agency_audit.audit.robots import fetch_robots_txt

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="", request=request)

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport, follow_redirects=True)
        try:
            result = await fetch_robots_txt("https://example.com", client=client)
            assert result.fetched is True
            assert result.allows_scraping is True
        finally:
            await client.aclose()


class TestRobotsHelpers:
    """Tests for robots.py helper functions."""

    def test_robots_url_https(self):
        from agency_audit.audit.robots import _robots_url

        assert _robots_url("https://example.com") == "https://example.com/robots.txt"

    def test_robots_url_with_path(self):
        from agency_audit.audit.robots import _robots_url

        assert _robots_url("https://example.com/some/path") == "https://example.com/robots.txt"

    def test_robots_url_http(self):
        from agency_audit.audit.robots import _robots_url

        assert _robots_url("http://example.com") == "http://example.com/robots.txt"

    def test_parse_robots_sync(self):
        from agency_audit.audit.robots import parse_robots_sync

        content = "User-agent: *\nDisallow: /\nCrawl-delay: 3\n"
        result = parse_robots_sync(content, "https://example.com")
        assert result.fetched is True
        assert result.allows_scraping is False
        assert result.crawl_delay == 3.0


# ---------------------------------------------------------------------------
# anti-scraping
# ---------------------------------------------------------------------------


class TestAntiScraping:
    def test_cloudflare_headers(self):
        headers = httpx.Headers({"server": "cloudflare", "cf-ray": "abc123"})
        assert _check_cloudflare_headers(headers) is True

    def test_no_cloudflare_headers(self):
        headers = httpx.Headers({"server": "nginx"})
        assert _check_cloudflare_headers(headers) is False

    def test_cloudflare_body(self):
        html = "<html><body>Just a moment...</body></html>"
        assert _check_cloudflare_body(html) is True

    def test_no_cloudflare_body(self):
        html = "<html><body>Hello World</body></html>"
        assert _check_cloudflare_body(html) is False

    def test_recaptcha_detected(self):
        html = '<html><body><script src="https://www.google.com/recaptcha/api.js"></script></body></html>'
        assert _check_recaptcha(html) is True

    def test_no_recaptcha(self):
        html = "<html><body>Hello World</body></html>"
        assert _check_recaptcha(html) is False

    def test_bot_detection_headers_found(self):
        headers = httpx.Headers({"x-sucuri-id": "12345"})
        found = _check_bot_detection_headers(headers)
        assert "sucuri" in found

    def test_no_bot_detection_headers(self):
        headers = httpx.Headers({"server": "nginx"})
        assert _check_bot_detection_headers(headers) == []

    def test_js_only_rendering_short_body(self):
        html = (
            "<html><body><noscript>Please enable JavaScript</noscript>"
            '<script src="app.js"></script></body></html>'
        )
        assert _check_js_only_rendering(html) is True

    def test_js_only_rendering_normal_page(self):
        html = "<html><body>" + "x" * 500 + "</body></html>"
        assert _check_js_only_rendering(html) is False

    def test_js_only_rendering_many_scripts_short_text(self):
        html = "<html><body>" + '<script src="app.js"></script>' * 10 + "</body></html>"
        assert _check_js_only_rendering(html) is True

    def test_no_body_is_js_only(self):
        html = "<html></html>"
        assert _check_js_only_rendering(html) is True


# ---------------------------------------------------------------------------
# anti-scraping integration (detect_anti_scraping via httpx.MockTransport)
# ---------------------------------------------------------------------------


class TestAntiScrapingIntegration:
    """Integration tests for detect_anti_scraping using mocked httpx responses."""

    # -- helpers --

    @staticmethod
    def _make_response(html: str, url: str = "https://example.com", **headers) -> httpx.Response:
        return httpx.Response(
            200,
            text=html,
            headers=headers,
            request=httpx.Request("GET", url),
        )

    @staticmethod
    async def _run(url: str, html: str = "<html><body>OK</body></html>", **headers):
        response = TestAntiScrapingIntegration._make_response(html, url, **headers)
        return await detect_anti_scraping(url, response=response)

    # -- Cloudflare tests --

    async def test_cloudflare_via_server_header(self):
        result = await self._run("https://example.com", server="cloudflare")
        assert result.cloudflare is True
        assert result.detected is True
        assert "cloudflare" in result.details

    async def test_cloudflare_via_cf_ray_header(self):
        result = await self._run("https://example.com", **{"cf-ray": "abc123"})
        assert result.cloudflare is True
        assert result.detected is True
        assert "cloudflare" in result.details

    async def test_cloudflare_via_challenge_body(self):
        html = "<html><body>__cf_bm=test; Just a moment...</body></html>"
        result = await self._run("https://example.com", html=html)
        assert result.cloudflare is True
        assert result.detected is True
        assert "cloudflare" in result.details

    async def test_no_cloudflare_integration(self):
        result = await self._run("https://example.com", server="nginx")
        assert result.cloudflare is False

    # -- reCAPTCHA tests --

    async def test_recaptcha_detected_integration(self):
        html = '<script src="https://www.google.com/recaptcha/api.js"></script>'
        result = await self._run("https://example.com", html=html)
        assert result.recaptcha is True
        assert result.detected is True
        assert "recaptcha" in result.details

    async def test_recaptcha_via_grecaptcha(self):
        html = '<script>grecaptcha.render("captcha")</script>'
        result = await self._run("https://example.com", html=html)
        assert result.recaptcha is True
        assert result.detected is True
        assert "recaptcha" in result.details

    async def test_no_recaptcha_integration(self):
        result = await self._run("https://example.com")
        assert result.recaptcha is False

    # -- Bot detection header tests --

    async def test_bot_detection_headers_found_integration(self):
        result = await self._run("https://example.com", **{"x-sucuri-id": "12345"})
        assert result.bot_detection_headers is True
        assert result.detected is True
        assert any("sucuri" in d for d in result.details)

    async def test_no_bot_detection_headers_integration(self):
        result = await self._run("https://example.com")
        assert result.bot_detection_headers is False

    # -- JS-only rendering tests --

    async def test_js_only_rendering_detected_integration(self):
        html = (
            "<html><body><noscript>Please enable JavaScript</noscript>"
            '<script src="app.js"></script></body></html>'
        )
        result = await self._run("https://example.com", html=html)
        assert result.js_only_rendering is True
        assert result.detected is True
        assert "js_only_rendering" in result.details

    async def test_normal_page_not_js_only(self):
        html = "<html><body>" + "x" * 500 + "</body></html>"
        result = await self._run("https://example.com", html=html)
        assert result.js_only_rendering is False

    # -- Combined detection --

    async def test_combined_detection(self):
        html = "<html><body>Just a moment... __cf_bm=test</body></html>"
        result = await self._run(
            "https://example.com",
            html=html,
            server="cloudflare",
            **{"cf-ray": "abc123", "x-sucuri-id": "12345"},
        )
        assert result.cloudflare is True
        assert result.recaptcha is False  # no recaptcha in html
        assert result.bot_detection_headers is True
        assert result.detected is True
        assert "cloudflare" in result.details
        assert any("sucuri" in d for d in result.details)

    async def test_combined_cloudflare_recaptcha_bot(self):
        html = (
            '<script src="https://www.google.com/recaptcha/api.js"></script>'
            "<body>Just a moment... __cf_bm=test</body>"
        )
        result = await self._run(
            "https://example.com", html=html, server="cloudflare", **{"x-sucuri-id": "12345"}
        )
        assert result.cloudflare is True
        assert result.recaptcha is True
        assert result.bot_detection_headers is True
        assert result.detected is True

    # -- Error handling --

    async def test_connect_error_graceful_degradation(self):
        """Connection errors should produce a result with error detail, not crash."""

        def error_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        transport = httpx.MockTransport(error_handler)
        client = httpx.AsyncClient(transport=transport)
        try:
            result = await detect_anti_scraping("https://unreachable.test", client=client)
            assert result.detected is False
            assert any("error" in d for d in result.details)
        finally:
            await client.aclose()

    # -- Pre-fetched response --

    async def test_pre_fetched_response_no_second_call(self):
        """Passing response= should use it without making a second HTTP call."""
        html = '<script src="https://www.google.com/recaptcha/api.js"></script>'
        response = httpx.Response(
            200,
            text=html,
            headers={"server": "cloudflare"},
            request=httpx.Request("GET", "https://example.com"),
        )
        result = await detect_anti_scraping("https://example.com", response=response)
        assert result.cloudflare is True
        assert result.recaptcha is True
        assert result.detected is True

    # -- Normal page: nothing detected --

    async def test_normal_page_nothing_detected(self):
        html = (
            "<html><head><title>Real Estate Agency</title></head>"
            "<body><p>Welcome to our agency.</p></body></html>"
        )
        result = await self._run("https://example.com", html=html, server="nginx")
        assert result.detected is False
        assert result.cloudflare is False
        assert result.recaptcha is False
        assert result.bot_detection_headers is False
        assert result.js_only_rendering is False
        assert result.details == []


# ---------------------------------------------------------------------------
# API detection
# ---------------------------------------------------------------------------


class TestApiDetection:
    def test_jsonld_structured_data_found(self):
        html = (
            "<html><head>"
            '<script type="application/ld+json">'
            '{"@type": "Product", "name": "Test Property"}'
            "</script>"
            "</head><body></body></html>"
        )
        found, types = _check_jsonld_structured_data(html)
        assert found is True
        assert "Product" in types

    def test_jsonld_no_realestate_type(self):
        html = (
            "<html><head>"
            '<script type="application/ld+json">'
            '{"@type": "WebPage", "name": "Home"}'
            "</script>"
            "</head><body></body></html>"
        )
        found, types = _check_jsonld_structured_data(html)
        assert found is False

    def test_jsonld_with_graph(self):
        html = (
            "<html><head>"
            '<script type="application/ld+json">'
            '{"@graph": [{"@type": "Place"}, {"@type": "WebPage"}]}'
            "</script>"
            "</head><body></body></html>"
        )
        found, types = _check_jsonld_structured_data(html)
        assert found is True
        assert "Place" in types

    def test_find_api_endpoints_rest(self):
        html = '<html><script>fetch("/api/v1/listings").then(...)</script></html>'
        endpoints = _find_api_endpoints_in_html(html)
        assert len(endpoints) > 0

    def test_find_api_endpoints_graphql(self):
        html = '<html><script>fetch("/graphql", {method: "POST"})</script></html>'
        endpoints = _find_api_endpoints_in_html(html)
        assert "/graphql" in endpoints

    def test_find_api_endpoints_none(self):
        html = "<html><body>Hello World</body></html>"
        endpoints = _find_api_endpoints_in_html(html)
        assert len(endpoints) == 0


# ---------------------------------------------------------------------------
# Property count
# ---------------------------------------------------------------------------


class TestPropertyCount:
    # ------------------------------------------------------------------
    # Fixtures — synthetic HTML / sitemap data for each source branch
    # ------------------------------------------------------------------

    @pytest.fixture
    def homepage_with_text_count(self) -> str:
        """HTML homepage with a text pattern count (high confidence)."""
        return "<html><body><p>1,250 properties found</p></body></html>"

    @pytest.fixture
    def homepage_with_listing_items(self) -> str:
        """HTML homepage with listing-item div elements (medium confidence)."""
        return "<html><body>" + '<div class="property-item">A</div>' * 20 + "</body></html>"

    @pytest.fixture
    def homepage_with_json_data(self) -> str:
        """HTML homepage with JSON embedded data (low-medium confidence)."""
        return '<html><body><script>var data = {"totalCount": 850};</script></body></html>'

    @pytest.fixture
    def homepage_empty(self) -> str:
        """HTML homepage with no property count indicators."""
        return "<html><body><p>Welcome to our agency</p></body></html>"

    @pytest.fixture
    def homepage_with_listing_link(self) -> str:
        """HTML homepage that links to a listing page but has no count inline."""
        return (
            '<html><body><nav><a href="/properties">Properties</a></nav>'
            "<p>Welcome to our agency</p></body></html>"
        )

    @pytest.fixture
    def listing_page_with_count(self) -> str:
        """HTML listing page with a text pattern count."""
        return "<html><body><p>500 results found</p></body></html>"

    @pytest.fixture
    def listing_page_no_count(self) -> str:
        """HTML listing page with no count (to force sitemap fallback)."""
        return "<html><body><p>Browse our properties</p></body></html>"

    @pytest.fixture
    def sitemap_with_property_urls(self) -> str:
        """XML sitemap containing property-like URLs."""
        return (
            '<?xml version="1.0"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            "<url><loc>https://example.com/properties/1</loc></url>\n"
            "<url><loc>https://example.com/properties/2</loc></url>\n"
            "<url><loc>https://example.com/properties/3</loc></url>\n"
            "<url><loc>https://example.com/about</loc></url>\n"
            "</urlset>"
        )

    @pytest.fixture
    def sitemap_no_property_urls(self) -> str:
        """XML sitemap with no property-like URLs (low confidence fallback)."""
        return (
            '<?xml version="1.0"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            "<url><loc>https://example.com/about</loc></url>\n"
            "<url><loc>https://example.com/contact</loc></url>\n"
            "</urlset>"
        )

    @pytest.fixture
    def sitemap_index(self) -> str:
        """Sitemap index referencing sub-sitemaps."""
        return (
            '<?xml version="1.0"?>\n'
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            "<sitemap><loc>https://example.com/sitemap-properties.xml</loc></sitemap>\n"
            "</sitemapindex>"
        )

    # --------------------------------------------------------------
    # count_properties — source="listing_page" (homepage text count)
    # --------------------------------------------------------------

    async def test_count_properties_listing_page_text(self, homepage_with_text_count):
        """Homepage with '1,250 properties' → source=listing_page, conf=0.7."""
        from agency_audit.audit.property_count import count_properties

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, text=homepage_with_text_count, request=req)
            )
        ) as client:
            result = await count_properties("https://example.com", client=client)

        assert result.count == 1250
        assert result.source == "listing_page"
        assert result.confidence == 0.7

    async def test_count_properties_listing_page_items(self, homepage_with_listing_items):
        """Homepage with 20 listing divs → source=listing_page, conf=0.3."""
        from agency_audit.audit.property_count import count_properties

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, text=homepage_with_listing_items, request=req)
            )
        ) as client:
            result = await count_properties("https://example.com", client=client)

        assert result.count == 20
        assert result.source == "listing_page"
        assert result.confidence == 0.3

    async def test_count_properties_listing_page_json(self, homepage_with_json_data):
        """Homepage with JSON totalCount → source=listing_page, conf=0.5."""
        from agency_audit.audit.property_count import count_properties

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, text=homepage_with_json_data, request=req)
            )
        ) as client:
            result = await count_properties("https://example.com", client=client)

        assert result.count == 850
        assert result.source == "listing_page"
        assert result.confidence == 0.5

    # ------------------------------------------------------------------
    # count_properties — source="listing_page" (via listing URL fallback)
    # ------------------------------------------------------------------

    async def test_count_properties_listing_page_fallback(
        self, homepage_with_listing_link, listing_page_with_count
    ):
        """No count on homepage → follow listing link → source=listing_page."""
        from agency_audit.audit.property_count import count_properties

        async def handler(req: httpx.Request) -> httpx.Response:
            if "/properties" in str(req.url):
                return httpx.Response(200, text=listing_page_with_count, request=req)
            return httpx.Response(200, text=homepage_with_listing_link, request=req)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await count_properties("https://example.com", client=client)

        assert result.count == 500
        assert result.source == "listing_page"
        assert result.confidence == 0.7  # text pattern on listing page

    # ------------------------------------------------------------------
    # count_properties — source="sitemap" (provided URLs)
    # ------------------------------------------------------------------

    async def test_count_properties_sitemap_provided(
        self, homepage_empty, sitemap_with_property_urls
    ):
        """No HTML count → falls back to sitemap → source=sitemap, conf=0.8."""
        from agency_audit.audit.property_count import count_properties

        async def handler(req: httpx.Request) -> httpx.Response:
            if "sitemap" in str(req.url):
                return httpx.Response(200, text=sitemap_with_property_urls, request=req)
            return httpx.Response(200, text=homepage_empty, request=req)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await count_properties(
                "https://example.com",
                sitemap_urls=["https://example.com/sitemap.xml"],
                client=client,
            )

        assert result.count == 3
        assert result.source == "sitemap"
        assert result.confidence == 0.8

    async def test_count_properties_sitemap_provided_low_confidence(
        self, homepage_empty, sitemap_no_property_urls
    ):
        """Sitemap without property URLs → falls back to total URL count, conf=0.4."""
        from agency_audit.audit.property_count import count_properties

        async def handler(req: httpx.Request) -> httpx.Response:
            if "sitemap" in str(req.url):
                return httpx.Response(200, text=sitemap_no_property_urls, request=req)
            return httpx.Response(200, text=homepage_empty, request=req)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await count_properties(
                "https://example.com",
                sitemap_urls=["https://example.com/sitemap.xml"],
                client=client,
            )

        assert result.count == 2
        assert result.source == "sitemap"
        assert result.confidence == 0.4

    # ------------------------------------------------------------------
    # count_properties — source="sitemap" (default sitemap URL)
    # ------------------------------------------------------------------

    async def test_count_properties_sitemap_default(
        self, homepage_empty, sitemap_with_property_urls
    ):
        """No sitemap URLs provided → tries /sitemap.xml → source=sitemap."""
        from agency_audit.audit.property_count import count_properties

        async def handler(req: httpx.Request) -> httpx.Response:
            if "sitemap" in str(req.url):
                return httpx.Response(200, text=sitemap_with_property_urls, request=req)
            return httpx.Response(200, text=homepage_empty, request=req)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await count_properties("https://example.com", client=client)

        assert result.count == 3
        assert result.source == "sitemap"
        assert result.confidence == 0.8

    # ------------------------------------------------------------------
    # count_properties — source="unknown" (nothing found)
    # ------------------------------------------------------------------

    async def test_count_properties_none(self, homepage_empty):
        """No count anywhere → source stays default 'unknown', conf=0.0."""
        from agency_audit.audit.property_count import count_properties

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, text=homepage_empty, request=req)
            )
        ) as client:
            result = await count_properties("https://example.com", client=client)

        assert result.count == 0
        assert result.source == "unknown"
        assert result.confidence == 0.0

    async def test_count_properties_none_sitemap_error(self, homepage_empty):
        """Sitemap returns 500 → fallback exhausted → source=unknown."""
        from agency_audit.audit.property_count import count_properties

        async def handler(req: httpx.Request) -> httpx.Response:
            if "sitemap" in str(req.url):
                return httpx.Response(500, text="Error", request=req)
            return httpx.Response(200, text=homepage_empty, request=req)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await count_properties("https://example.com", client=client)

        assert result.count == 0
        assert result.source == "unknown"
        assert result.confidence == 0.0

    # ------------------------------------------------------------------
    # count_properties — listing-page fallback then sitemap
    # ------------------------------------------------------------------

    async def test_count_properties_listing_then_sitemap(
        self, homepage_with_listing_link, listing_page_no_count, sitemap_with_property_urls
    ):
        """Listing page has no count → falls through to sitemap → source=sitemap."""
        from agency_audit.audit.property_count import count_properties

        async def handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "sitemap" in url:
                return httpx.Response(200, text=sitemap_with_property_urls, request=req)
            if "/properties" in url:
                return httpx.Response(200, text=listing_page_no_count, request=req)
            return httpx.Response(200, text=homepage_with_listing_link, request=req)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await count_properties(
                "https://example.com",
                sitemap_urls=["https://example.com/sitemap.xml"],
                client=client,
            )

        assert result.count == 3
        assert result.source == "sitemap"
        assert result.confidence == 0.8

    # ------------------------------------------------------------------
    # _count_from_html — edge cases & confidence values
    # ------------------------------------------------------------------

    def test_count_from_html_text_pattern(self):
        from agency_audit.audit.property_count import _count_from_html

        html = "<html><body><p>1,250 properties found</p></body></html>"
        count, conf = _count_from_html(html)
        assert count == 1250
        assert conf == 0.7

    def test_count_from_html_listing_items(self):
        from agency_audit.audit.property_count import _count_from_html

        html = "<html><body>" + '<div class="property-item">A</div>' * 20 + "</body></html>"
        count, conf = _count_from_html(html)
        assert count == 20
        assert conf == 0.3

    def test_count_from_html_json_data(self):
        from agency_audit.audit.property_count import _count_from_html

        html = '<html><body><script>var data = {"totalCount": 850};</script></body></html>'
        count, conf = _count_from_html(html)
        assert count == 850
        assert conf == 0.5

    def test_count_from_html_none(self):
        from agency_audit.audit.property_count import _count_from_html

        html = "<html><body>Hello World</body></html>"
        count, conf = _count_from_html(html)
        assert count == 0

    # ------------------------------------------------------------------
    # _find_listing_page_url — URL discovery edge cases
    # ------------------------------------------------------------------

    def test_find_listing_page_url(self):
        from agency_audit.audit.property_count import _find_listing_page_url

        html = '<html><body><nav><a href="/properties">Properties</a></nav></body></html>'
        url = _find_listing_page_url("https://example.com", html)
        assert url is not None
        assert "properties" in url

    def test_find_listing_page_url_absolute(self):
        from agency_audit.audit.property_count import _find_listing_page_url

        html = (
            "<html><body>"
            '<nav><a href="https://example.com/listings">Listings</a></nav>'
            "</body></html>"
        )
        url = _find_listing_page_url("https://example.com", html)
        assert url == "https://example.com/listings"

    def test_find_listing_page_url_none(self):
        from agency_audit.audit.property_count import _find_listing_page_url

        html = '<html><body><a href="/about">About</a></body></html>'
        url = _find_listing_page_url("https://example.com", html)
        assert url is None

    def test_find_listing_page_url_imoti(self):
        """Bulgarian 'imoti' path is recognised as a listing page."""
        from agency_audit.audit.property_count import _find_listing_page_url

        html = '<html><body><a href="/imoti">Имоти</a></body></html>'
        url = _find_listing_page_url("https://example.com", html)
        assert url == "https://example.com/imoti"

    def test_find_listing_page_url_multiple_links(self):
        """First matching link wins when multiple candidates exist."""
        from agency_audit.audit.property_count import _find_listing_page_url

        html = (
            "<html><body>"
            '<a href="/offers">Offers</a>'
            '<a href="/search">Search</a>'
            '<a href="/annonces">Annonces</a>'
            "</body></html>"
        )
        url = _find_listing_page_url("https://example.com", html)
        # First match in pattern order: "/offers" matches r"/offers?" first
        assert url == "https://example.com/offers"

    def test_find_listing_page_url_no_links(self):
        """Returns None when there are no links on the page."""
        from agency_audit.audit.property_count import _find_listing_page_url

        html = "<html><body><p>No navigation here</p></body></html>"
        url = _find_listing_page_url("https://example.com", html)
        assert url is None

    def test_find_listing_page_url_empty_href(self):
        """Skips empty href attributes."""
        from agency_audit.audit.property_count import _find_listing_page_url

        html = '<html><body><a href="">Home</a><a href="/search">Search</a></body></html>'
        url = _find_listing_page_url("https://example.com", html)
        assert url == "https://example.com/search"


# ---------------------------------------------------------------------------
# Listing quality
# ---------------------------------------------------------------------------


class TestListingQuality:
    def test_structured_data_jsonld(self):
        html = (
            "<html><head>"
            '<script type="application/ld+json">'
            '{"@type": "Product", "name": "Villa"}'
            "</script>"
            "</head><body></body></html>"
        )
        assert _check_structured_data(html) is True

    def test_structured_data_microdata(self):
        html = (
            "<html><body>"
            '<div itemtype="https://schema.org/Product">'
            '<span itemprop="name">Villa</span>'
            "</div>"
            "</body></html>"
        )
        assert _check_structured_data(html) is True

    def test_no_structured_data(self):
        html = "<html><body>Hello World</body></html>"
        assert _check_structured_data(html) is False

    def test_map_detected_google(self):
        html = (
            "<html><body>"
            '<iframe src="https://maps.googleapis.com/map/embed"></iframe>'
            "</body></html>"
        )
        assert _check_map(html) is True

    def test_map_detected_leaflet(self):
        html = '<html><body><div class="leaflet-container"></div></body></html>'
        assert _check_map(html) is True

    def test_no_map(self):
        html = "<html><body><p>No map here</p></body></html>"
        assert _check_map(html) is False

    def test_map_not_sitemap(self):
        html = '<html><body><div class="sitemap"><a href="/page1">Page 1</a></div></body></html>'
        # "sitemap" should not trigger map detection
        assert _check_map(html) is False


# ---------------------------------------------------------------------------
# Tech stack
# ---------------------------------------------------------------------------


class TestTechStack:
    def test_framework_from_headers_express(self):
        headers = httpx.Headers({"x-powered-by": "Express"})
        assert _detect_framework_from_headers(headers) == "Express"

    def test_framework_from_headers_nginx(self):
        headers = httpx.Headers({"server": "nginx/1.25"})
        assert _detect_framework_from_headers(headers) == "Nginx"

    def test_framework_from_headers_none(self):
        headers = httpx.Headers({})
        assert _detect_framework_from_headers(headers) is None

    def test_framework_from_html_wordpress(self):
        html = '<html><head><script src="/wp-content/themes/mytheme/app.js"></script></head></html>'
        assert _detect_framework_from_html(html) == "WordPress"

    def test_framework_from_html_nextjs(self):
        html = (
            '<html><head><script id="__NEXT_DATA__" type="application/json"></script></head></html>'
        )
        assert _detect_framework_from_html(html) == "Next.js"

    def test_framework_from_html_react(self):
        html = '<html><body><div data-reactroot="true">App</div></body></html>'
        assert _detect_framework_from_html(html) == "React"

    def test_framework_from_html_none(self):
        html = "<html><body>Hello</body></html>"
        assert _detect_framework_from_html(html) is None

    def test_cdn_cloudflare(self):
        headers = httpx.Headers({"cf-ray": "abc123"})
        assert _detect_cdn(headers) == "Cloudflare"

    def test_cdn_cloudfront(self):
        headers = httpx.Headers({"x-amz-cf-id": "abc123"})
        assert _detect_cdn(headers) == "CloudFront (AWS)"

    def test_cdn_none(self):
        headers = httpx.Headers({})
        assert _detect_cdn(headers) is None

    def test_hosting_detection(self):
        headers = httpx.Headers({"server": "nginx"})
        html = "<html><body><!-- hosted by WP Engine --></body></html>"
        assert _detect_hosting(headers, html) == "WP Engine"

    def test_hosting_none(self):
        headers = httpx.Headers({"server": "nginx"})
        html = "<html><body>Nothing here</body></html>"
        assert _detect_hosting(headers, html) is None

    def test_technologies_detected(self):
        html = (
            "<html><head>"
            '<script src="jquery.min.js"></script>'
            '<script src="bootstrap.bundle.js"></script>'
            '<script src="google-analytics.js"></script>'
            "</head><body></body></html>"
        )
        techs = _detect_technologies(html)
        assert "jQuery" in techs
        assert "Bootstrap" in techs
        assert "Google Analytics" in techs

    def test_technologies_none(self):
        html = "<html><body>Hello</body></html>"
        assert _detect_technologies(html) == []


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


class TestScoring:
    # ------------------------------------------------------------------
    # pytest fixtures — reusable AuditData scenarios
    # ------------------------------------------------------------------

    @pytest.fixture
    def default_config(self) -> dict:
        """Load default scoring config once per test."""
        return load_scoring_config()

    @pytest.fixture
    def base_audit(self) -> AuditData:
        """Minimal AuditData with neutral defaults (robots allows, SSL valid)."""
        return AuditData(
            url="https://example.com",
            robots=RobotsResult(fetched=True, allows_scraping=True),
            ssl_valid=True,
        )

    @pytest.fixture
    def perfect_audit(self) -> AuditData:
        """AuditData with every positive trait — maxes out the scoring config."""
        return AuditData(
            url="https://perfect.example.com",
            robots=RobotsResult(fetched=True, allows_scraping=True),
            anti_scraping=AntiScrapingResult(detected=False),
            api_detection=ApiDetectionResult(detected=True, api_type="graphql"),
            property_count=PropertyCountResult(count=2000, confidence=0.9),
            listing_quality=ListingQualityResult(
                has_structured_data=True,
                has_images=True,
                has_descriptions=True,
                has_prices=True,
                has_locations=True,
                has_property_map=True,
                quality_score=1.0,
            ),
            tech_stack=TechStackResult(framework="WordPress"),
            response_time_ms=200,
            ssl_valid=True,
        )

    @pytest.fixture
    def negative_audit(self) -> AuditData:
        """AuditData with every negative trait — worst possible scoring."""
        return AuditData(
            url="https://bad.example.com",
            robots=RobotsResult(fetched=True, allows_scraping=False),
            anti_scraping=AntiScrapingResult(detected=True, cloudflare=True),
            api_detection=ApiDetectionResult(detected=False),
            property_count=PropertyCountResult(count=0),
            listing_quality=ListingQualityResult(quality_score=0.0),
            tech_stack=TechStackResult(),
            response_time_ms=5000,
            ssl_valid=False,
        )

    @pytest.fixture
    def empty_audit(self) -> AuditData:
        """AuditData with all defaults — simulates pre-check state."""
        return AuditData()

    # ------------------------------------------------------------------
    # existing tests
    # ------------------------------------------------------------------

    def test_default_config_loaded(self):
        config = load_scoring_config()
        assert "robots_allows" in config
        assert "robots_disallows" in config
        assert "property_count_tiers" in config

    def test_score_all_positive(self):
        """A site with all good qualities should score high."""
        audit = AuditData(
            url="https://example.com",
            robots=RobotsResult(fetched=True, allows_scraping=True),
            anti_scraping=AntiScrapingResult(detected=False),
            api_detection=ApiDetectionResult(detected=True, api_type="rest"),
            property_count=PropertyCountResult(count=1500, confidence=0.8),
            listing_quality=ListingQualityResult(
                has_structured_data=True,
                has_images=True,
                has_descriptions=True,
                has_prices=True,
                has_locations=True,
                has_property_map=True,
                quality_score=1.0,
            ),
            tech_stack=TechStackResult(framework="WordPress"),
            response_time_ms=300,
            ssl_valid=True,
        )
        score, breakdown = compute_score(audit)
        assert score > 80, f"Expected high score, got {score}"
        assert "robots_allows" in breakdown
        assert "has_api" in breakdown
        assert "property_count_1000+" in breakdown

    def test_score_all_negative(self):
        """A bad site should have a negative score."""
        audit = AuditData(
            url="https://example.com",
            robots=RobotsResult(fetched=True, allows_scraping=False),
            anti_scraping=AntiScrapingResult(detected=True, cloudflare=True),
            api_detection=ApiDetectionResult(detected=False),
            property_count=PropertyCountResult(count=0),
            listing_quality=ListingQualityResult(quality_score=0.0),
            tech_stack=TechStackResult(),
            response_time_ms=5000,
            ssl_valid=False,
        )
        score, breakdown = compute_score(audit)
        assert score < 0, f"Expected negative score, got {score}"
        assert "robots_disallows" in breakdown
        assert "has_anti_scraping" in breakdown
        assert "ssl_invalid" in breakdown

    def test_score_property_count_tiers(self):
        """Property count should apply tiered scoring."""
        # 1000+ tier
        audit = AuditData(
            property_count=PropertyCountResult(count=1500),
            robots=RobotsResult(allows_scraping=True),
            listing_quality=ListingQualityResult(quality_score=0.5),
            ssl_valid=True,
        )
        score, _ = compute_score(audit)
        assert score > 0

        # 500-999 tier
        audit2 = AuditData(
            property_count=PropertyCountResult(count=600),
            robots=RobotsResult(allows_scraping=True),
            listing_quality=ListingQualityResult(quality_score=0.5),
            ssl_valid=True,
        )
        score2, _ = compute_score(audit2)
        assert score2 < score, "More properties should yield higher score"

        # 100-499 tier
        audit3 = AuditData(
            property_count=PropertyCountResult(count=200),
            robots=RobotsResult(allows_scraping=True),
            listing_quality=ListingQualityResult(quality_score=0.5),
            ssl_valid=True,
        )
        score3, _ = compute_score(audit3)
        assert score3 < score2, "More properties should yield higher score"

    def test_score_graphql_bonus(self):
        """GraphQL API should score higher than REST."""
        rest_audit = AuditData(
            api_detection=ApiDetectionResult(detected=True, api_type="rest"),
            robots=RobotsResult(allows_scraping=True),
            listing_quality=ListingQualityResult(quality_score=0.5),
            ssl_valid=True,
        )
        graphql_audit = AuditData(
            api_detection=ApiDetectionResult(detected=True, api_type="graphql"),
            robots=RobotsResult(allows_scraping=True),
            listing_quality=ListingQualityResult(quality_score=0.5),
            ssl_valid=True,
        )
        rest_score, _ = compute_score(rest_audit)
        graphql_score, _ = compute_score(graphql_audit)
        assert graphql_score > rest_score

    def test_score_clamped(self):
        """Score should be clamped to min/max."""
        config = load_scoring_config()
        assert config["max_score"] == 100
        assert config["min_score"] == -100

    def test_score_custom_config(self):
        """Custom config should override defaults."""
        custom_config = load_scoring_config().copy()
        custom_config["robots_allows"] = 50
        audit = AuditData(
            robots=RobotsResult(allows_scraping=True),
            listing_quality=ListingQualityResult(quality_score=0.5),
            ssl_valid=True,
        )
        score, breakdown = compute_score(audit, custom_config)
        assert breakdown["robots_allows"] == 50

    def test_score_breakdown_sum_matches(self):
        """Sum of breakdown values should equal the score (before clamping)."""
        audit = AuditData(
            url="https://example.com",
            robots=RobotsResult(allows_scraping=True),
            anti_scraping=AntiScrapingResult(detected=False),
            api_detection=ApiDetectionResult(detected=True, api_type="rest"),
            property_count=PropertyCountResult(count=500),
            listing_quality=ListingQualityResult(
                has_prices=True,
                has_locations=True,
                has_images=True,
                has_descriptions=True,
                has_structured_data=True,
                has_property_map=True,
                quality_score=1.0,
            ),
            response_time_ms=300,
            ssl_valid=True,
        )
        score, breakdown = compute_score(audit)
        assert sum(breakdown.values()) == score or score == 100  # might be clamped

    # ------------------------------------------------------------------
    # fixture-driven tests — perfect score, zero score, partial data,
    # breakdown→to_dict integration, response time boundaries, tier
    # boundaries
    # ------------------------------------------------------------------

    def test_perfect_score_is_100(self, perfect_audit, default_config):
        """A site with every positive trait should score 100 (clamped)."""
        score, breakdown = compute_score(perfect_audit, default_config)
        assert score == 100, f"Expected 100, got {score}"
        # All positive breakdown keys should be present
        assert "robots_allows" in breakdown
        assert "has_graphql_api" in breakdown
        assert "property_count_1000+" in breakdown
        assert "has_structured_data" in breakdown
        assert "listings_have_prices" in breakdown
        assert "listings_have_locations" in breakdown
        assert "listings_have_images" in breakdown
        assert "listings_have_descriptions" in breakdown
        assert "has_property_map" in breakdown
        assert "response_time_fast" in breakdown
        assert "ssl_valid" in breakdown

    def test_zero_score_with_neutral_config(self, base_audit, default_config):
        """With all config weights zeroed, any audit should score 0."""
        zero_config = {k: 0 for k in default_config}
        zero_config["property_count_tiers"] = []
        zero_config["min_score"] = -100
        zero_config["max_score"] = 100
        score, breakdown = compute_score(base_audit, zero_config)
        assert score == 0, f"Expected 0, got {score}"

    def test_empty_audit_scores_positive(self, empty_audit, default_config):
        """Default AuditData (robots allows + SSL valid) scores 25."""
        score, breakdown = compute_score(empty_audit, default_config)
        assert score == 25, f"Expected 25, got {score} (breakdown={breakdown})"
        assert "robots_allows" in breakdown
        assert "ssl_valid" in breakdown

    def test_breakdown_propagates_to_to_dict(self, perfect_audit, default_config):
        """score_breakdown in AuditData.to_dict() matches compute_score output."""
        score, breakdown = compute_score(perfect_audit, default_config)
        perfect_audit.score = score
        perfect_audit.score_breakdown = breakdown
        data = perfect_audit.to_dict()
        assert data["score"] == score
        assert data["score_breakdown"] == breakdown
        # Verify breakdown is JSON-serializable
        import json

        json.dumps(data)

    def test_breakdown_keys_are_stable(self, default_config):
        """Breakdown keys should match the config keys they draw from."""
        valid_keys = {
            "robots_allows",
            "robots_disallows",
            "has_anti_scraping",
            "has_api",
            "has_graphql_api",
            "has_structured_data",
            "listings_have_prices",
            "listings_have_locations",
            "listings_have_images",
            "listings_have_descriptions",
            "has_property_map",
            "response_time_fast",
            "response_time_slow",
            "ssl_valid",
            "ssl_invalid",
        }
        # property_count_tiers produce dynamic keys like "property_count_1000+"
        audit = AuditData(
            robots=RobotsResult(allows_scraping=True),
            anti_scraping=AntiScrapingResult(detected=True),
            api_detection=ApiDetectionResult(detected=True, api_type="rest"),
            property_count=PropertyCountResult(count=1000),
            listing_quality=ListingQualityResult(
                has_structured_data=True,
                has_prices=True,
                has_locations=True,
                has_images=True,
                has_descriptions=True,
                has_property_map=True,
            ),
            response_time_ms=200,
            ssl_valid=True,
        )
        _, breakdown = compute_score(audit, default_config)
        for key in breakdown:
            if not key.startswith("property_count_"):
                assert key in valid_keys, f"Unexpected breakdown key: {key}"

    def test_response_time_fast_boundary(self, base_audit, default_config):
        """Response times strictly under 500ms earn 'fast' points."""
        # 499ms — fast
        audit_fast = AuditData(**{**base_audit.__dict__, "response_time_ms": 499})
        _, bd = compute_score(audit_fast, default_config)
        assert "response_time_fast" in bd

        # 500ms — neutral (not fast, not slow)
        audit_neutral = AuditData(**{**base_audit.__dict__, "response_time_ms": 500})
        _, bd = compute_score(audit_neutral, default_config)
        assert "response_time_fast" not in bd
        assert "response_time_slow" not in bd

    def test_response_time_slow_boundary(self, base_audit, default_config):
        """Response times strictly over 3000ms earn 'slow' penalty."""
        # 3000ms — neutral (not slow)
        audit_neutral = AuditData(**{**base_audit.__dict__, "response_time_ms": 3000})
        _, bd = compute_score(audit_neutral, default_config)
        assert "response_time_slow" not in bd

        # 3001ms — slow
        audit_slow = AuditData(**{**base_audit.__dict__, "response_time_ms": 3001})
        _, bd = compute_score(audit_slow, default_config)
        assert "response_time_slow" in bd

    def test_response_time_none_no_effect(self, base_audit, default_config):
        """None response_time_ms should produce no performance points."""
        audit = AuditData(**{**base_audit.__dict__, "response_time_ms": None})
        _, breakdown = compute_score(audit, default_config)
        assert "response_time_fast" not in breakdown
        assert "response_time_slow" not in breakdown

    def test_property_count_tier_boundaries(self, base_audit, default_config):
        """Property counts exactly at tier minimums should earn those points."""

        def score_for_count(count):
            a = AuditData(
                **{**base_audit.__dict__, "property_count": PropertyCountResult(count=count)},
            )
            s, bd = compute_score(a, default_config)
            return s, bd

        # 1000+ tier
        _, bd = score_for_count(1000)
        assert "property_count_1000+" in bd
        assert bd["property_count_1000+"] == default_config["property_count_tiers"][0]["points"]

        # 500+ tier
        _, bd = score_for_count(500)
        assert "property_count_500+" in bd

        # 100+ tier
        _, bd = score_for_count(100)
        assert "property_count_100+" in bd

        # 10+ tier
        _, bd = score_for_count(10)
        assert "property_count_10+" in bd

        # Below lowest tier — no property_count key
        _, bd = score_for_count(5)
        pc_keys = [k for k in bd if k.startswith("property_count_")]
        assert pc_keys == [], f"Unexpected property_count keys: {pc_keys}"

    def test_ssl_invalid_penalty(self, base_audit, default_config):
        """Invalid SSL should apply the ssl_invalid penalty."""
        audit = AuditData(**{**base_audit.__dict__, "ssl_valid": False})
        _, breakdown = compute_score(audit, default_config)
        assert "ssl_invalid" in breakdown
        assert "ssl_valid" not in breakdown

    def test_robots_default_allow(self, default_config):
        """Default RobotsResult (allows_scraping=True) earns robots_allows."""
        audit = AuditData(robots=RobotsResult())  # default allow
        _, breakdown = compute_score(audit, default_config)
        assert "robots_allows" in breakdown
        assert "robots_disallows" not in breakdown

    def test_partial_data_only_api(self, default_config):
        """When only API detection is populated, see API keys in breakdown."""
        audit = AuditData(
            api_detection=ApiDetectionResult(detected=True, api_type="rest"),
        )
        score, breakdown = compute_score(audit, default_config)
        assert "has_api" in breakdown
        # Default AuditData also has robots_allows (20) + ssl_valid (5) = 45
        assert score == 45, f"Expected 45, got {score} (breakdown={breakdown})"

    def test_partial_data_only_property_count(self, base_audit, default_config):
        """Only property_count populated — only tier key appears."""
        audit = AuditData(
            **{**base_audit.__dict__, "property_count": PropertyCountResult(count=800)},
        )
        _, breakdown = compute_score(audit, default_config)
        pc_keys = [k for k in breakdown if k.startswith("property_count_")]
        assert len(pc_keys) == 1
        assert pc_keys[0] == "property_count_500+"


# ---------------------------------------------------------------------------
# AuditData serialization
# ---------------------------------------------------------------------------


class TestAuditDataSerialization:
    def test_to_dict_has_all_fields(self):
        audit = AuditData(
            url="https://example.com",
            robots=RobotsResult(allows_scraping=True),
            listing_quality=ListingQualityResult(quality_score=0.5),
            tech_stack=TechStackResult(technologies=["WordPress"]),
            score=55,
        )
        data = audit.to_dict()
        assert "robots_txt_allows" in data
        assert "has_anti_scraping" in data
        assert "has_api" in data
        assert "property_count" in data
        assert "listing_quality_score" in data
        assert "technology_stack" in data
        assert data["technology_stack"] == ["WordPress"]
        assert data["score"] == 55

    def test_to_dict_jsonable(self):
        audit = AuditData(url="https://example.com", score=42)
        data = audit.to_dict()
        # Should be JSON serializable
        json_str = json.dumps(data)
        assert json_str is not None
        parsed = json.loads(json_str)
        assert parsed["score"] == 42


class TestAuditDataToDictContract:
    """Fixture-driven contract tests for AuditData.to_dict().

    These assert the byte-contract consumed by cli.py:208-273 (table/json
    output) and orchestrator.py:373-379 (JSONB persistence).  Every key
    the CLI and orchestrator consume must be present with the correct type.
    """

    @pytest.fixture
    def populated_audit(self) -> AuditData:
        """A fully-populated AuditData with realistic values for every field."""
        return AuditData(
            url="https://example.com",
            robots=RobotsResult(
                fetched=True,
                allows_scraping=False,
                crawl_delay=5.0,
                sitemap_urls=["https://example.com/sitemap.xml"],
                raw_content="User-agent: *\nDisallow: /\n",
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
                api_type="json-ld",
                api_url="https://example.com/api/graphql",
                endpoints_found=["/api/graphql", "/api/v1"],
            ),
            property_count=PropertyCountResult(
                count=1250,
                source="sitemap",
                confidence=0.85,
            ),
            listing_quality=ListingQualityResult(
                has_structured_data=True,
                has_images=True,
                has_descriptions=True,
                has_prices=True,
                has_locations=True,
                has_property_map=True,
                quality_score=0.92,
            ),
            tech_stack=TechStackResult(
                framework="WordPress",
                hosting="AWS",
                cdn="Cloudflare",
                technologies=["WordPress", "PHP", "MySQL", "jQuery"],
            ),
            response_time_ms=345.6,
            ssl_valid=True,
            language="en",
            notes="High-quality real estate site.",
            score=72,
            score_breakdown={
                "robots_allowed": 10,
                "property_count_accurate": 15,
                "listing_quality": 20,
                "no_anti_scraping": -5,
                "has_api": 10,
            },
        )

    @pytest.fixture
    def default_audit(self) -> AuditData:
        """An AuditData with all defaults — no values explicitly set."""
        return AuditData(url="https://default.test")

    # ── CLI-consumed fields (cli.py:208-273) ──────────────────────────────

    def test_cli_robots_allows_scraping(self, populated_audit: AuditData):
        """cli.py:213 reads result.robots.allows_scraping → robots_txt_allows."""
        data = populated_audit.to_dict()
        assert data["robots_txt_allows"] is False
        assert isinstance(data["robots_txt_allows"], bool)

    def test_cli_framework(self, populated_audit: AuditData):
        """cli.py:232 reads result.tech_stack.framework → framework."""
        data = populated_audit.to_dict()
        assert data["framework"] == "WordPress"
        assert isinstance(data["framework"], str)

    def test_cli_framework_none(self, default_audit: AuditData):
        """framework can be None when undetected."""
        data = default_audit.to_dict()
        assert data["framework"] is None

    def test_cli_property_count_source(self, populated_audit: AuditData):
        """cli.py:226 reads result.property_count.source → property_count_source."""
        data = populated_audit.to_dict()
        assert data["property_count_source"] == "sitemap"
        assert isinstance(data["property_count_source"], str)

    def test_cli_property_count_confidence(self, populated_audit: AuditData):
        """cli.py:226 reads result.property_count.confidence → property_count_confidence."""
        data = populated_audit.to_dict()
        assert data["property_count_confidence"] == 0.85
        assert isinstance(data["property_count_confidence"], float)

    def test_cli_property_count_defaults(self, default_audit: AuditData):
        """property_count defaults: count=0, source='unknown', confidence=0.0."""
        data = default_audit.to_dict()
        assert data["property_count"] == 0
        assert data["property_count_source"] == "unknown"
        assert data["property_count_confidence"] == 0.0

    def test_cli_score_breakdown(self, populated_audit: AuditData):
        """cli.py:243 iterates result.score_breakdown → score_breakdown."""
        data = populated_audit.to_dict()
        breakdown = data["score_breakdown"]
        assert isinstance(breakdown, dict)
        assert breakdown["robots_allowed"] == 10
        assert breakdown["no_anti_scraping"] == -5
        assert len(breakdown) == 5

    def test_cli_score(self, populated_audit: AuditData):
        """cli.py:242 reads result.score → score."""
        data = populated_audit.to_dict()
        assert data["score"] == 72
        assert isinstance(data["score"], int)

    def test_cli_tech_stack_technologies(self, populated_audit: AuditData):
        """cli.py:235 reads result.tech_stack.technologies → technology_stack."""
        data = populated_audit.to_dict()
        assert data["technology_stack"] == ["WordPress", "PHP", "MySQL", "jQuery"]
        assert isinstance(data["technology_stack"], list)

    def test_cli_hosting(self, populated_audit: AuditData):
        """cli.py:234 reads result.tech_stack.hosting → hosting."""
        data = populated_audit.to_dict()
        assert data["hosting"] == "AWS"
        assert isinstance(data["hosting"], str)

    def test_cli_cdn(self, populated_audit: AuditData):
        """cli.py:233 reads result.tech_stack.cdn → cdn."""
        data = populated_audit.to_dict()
        assert data["cdn"] == "Cloudflare"
        assert isinstance(data["cdn"], str)

    def test_cli_response_time(self, populated_audit: AuditData):
        """cli.py:236 reads result.response_time_ms → response_time_ms."""
        data = populated_audit.to_dict()
        assert data["response_time_ms"] == 345.6
        assert isinstance(data["response_time_ms"], float)

    def test_cli_response_time_none(self, default_audit: AuditData):
        """response_time_ms can be None."""
        data = default_audit.to_dict()
        assert data["response_time_ms"] is None

    def test_cli_ssl_valid(self, populated_audit: AuditData):
        """cli.py:237 reads result.ssl_valid → ssl_valid."""
        data = populated_audit.to_dict()
        assert data["ssl_valid"] is True
        assert isinstance(data["ssl_valid"], bool)

    def test_cli_language(self, populated_audit: AuditData):
        """cli.py:238 reads result.language → language."""
        data = populated_audit.to_dict()
        assert data["language"] == "en"
        assert isinstance(data["language"], str)

    def test_cli_language_none(self, default_audit: AuditData):
        """language can be None."""
        data = default_audit.to_dict()
        assert data["language"] is None

    # ── Orchestrator-consumed fields (orchestrator.py:373-379) ────────────

    def test_orchestrator_score(self, populated_audit: AuditData):
        """orchestrator.py:377 persists result.score → score."""
        data = populated_audit.to_dict()
        assert "score" in data
        assert data["score"] == 72

    def test_orchestrator_to_dict_is_jsonb_safe(self, populated_audit: AuditData):
        """orchestrator.py:376 persists json.dumps(result.to_dict()) as JSONB."""
        data = populated_audit.to_dict()
        json_str = json.dumps(data)
        assert json_str is not None
        roundtripped = json.loads(json_str)
        # spot-check critical fields survive the JSON roundtrip
        assert roundtripped["score"] == 72
        assert roundtripped["robots_txt_allows"] is False
        assert roundtripped["framework"] == "WordPress"
        assert roundtripped["property_count_source"] == "sitemap"
        assert roundtripped["score_breakdown"]["listing_quality"] == 20

    def test_orchestrator_to_dict_default_is_jsonb_safe(self, default_audit: AuditData):
        """Even a default AuditData must be JSONB-serializable."""
        data = default_audit.to_dict()
        json_str = json.dumps(data)
        roundtripped = json.loads(json_str)
        assert roundtripped["score"] == 0
        assert roundtripped["robots_txt_allows"] is True

    # ── Complete key coverage ─────────────────────────────────────────────

    def test_to_dict_exact_keys(self, populated_audit: AuditData):
        """Every key emitted by to_dict() is known — no surprise regressions."""
        data = populated_audit.to_dict()
        expected_keys = {
            "url",
            "robots_txt_allows",
            "robots_txt_fetched",
            "robots_crawl_delay",
            "robots_sitemap_urls",
            "has_anti_scraping",
            "anti_scraping_details",
            "cloudflare",
            "recaptcha",
            "has_api",
            "api_type",
            "api_url",
            "api_endpoints",
            "property_count",
            "property_count_source",
            "property_count_confidence",
            "has_structured_data",
            "listings_have_images",
            "listings_have_descriptions",
            "listings_have_prices",
            "listings_have_locations",
            "has_property_map",
            "listing_quality_score",
            "technology_stack",
            "framework",
            "hosting",
            "cdn",
            "response_time_ms",
            "ssl_valid",
            "language",
            "notes",
            "score",
            "score_breakdown",
        }
        assert set(data.keys()) == expected_keys

    def test_to_dict_all_keys_present_on_default(self, default_audit: AuditData):
        """Even a default AuditData must emit every key."""
        data = default_audit.to_dict()
        assert "robots_txt_allows" in data
        assert "framework" in data
        assert "property_count_source" in data
        assert "score_breakdown" in data
        assert "score" in data
        assert "technology_stack" in data
        assert "listing_quality_score" in data

    def test_to_dict_scores_int(self, default_audit: AuditData):
        """score must always be int, score_breakdown must be dict."""
        data = default_audit.to_dict()
        assert isinstance(data["score"], int)
        assert isinstance(data["score_breakdown"], dict)

    def test_to_dict_lists_are_lists(self, populated_audit: AuditData):
        """List fields (sitemap_urls, details, endpoints, technologies) must be lists."""
        data = populated_audit.to_dict()
        assert isinstance(data["robots_sitemap_urls"], list)
        assert isinstance(data["anti_scraping_details"], list)
        assert isinstance(data["api_endpoints"], list)
        assert isinstance(data["technology_stack"], list)

    def test_to_dict_no_extra_keys_on_default(self, default_audit: AuditData):
        """Default AuditData should not introduce extra keys beyond the known set."""
        data = default_audit.to_dict()
        known = {
            "url",
            "robots_txt_allows",
            "robots_txt_fetched",
            "robots_crawl_delay",
            "robots_sitemap_urls",
            "has_anti_scraping",
            "anti_scraping_details",
            "cloudflare",
            "recaptcha",
            "has_api",
            "api_type",
            "api_url",
            "api_endpoints",
            "property_count",
            "property_count_source",
            "property_count_confidence",
            "has_structured_data",
            "listings_have_images",
            "listings_have_descriptions",
            "listings_have_prices",
            "listings_have_locations",
            "has_property_map",
            "listing_quality_score",
            "technology_stack",
            "framework",
            "hosting",
            "cdn",
            "response_time_ms",
            "ssl_valid",
            "language",
            "notes",
            "score",
            "score_breakdown",
        }
        assert set(data.keys()) == known

    def test_to_dict_cli_scoring_display(self, populated_audit: AuditData):
        """Simulate the CLI scoring display loop (cli.py:243-245)."""
        data = populated_audit.to_dict()
        breakdown = data["score_breakdown"]
        # CLI iterates: for check, points in result.score_breakdown.items()
        for check, points in breakdown.items():
            assert isinstance(check, str)
            assert isinstance(points, int)
            # CLI uses: color = "green" if points > 0 else "red" if points < 0 else "dim"
            _ = "green" if points > 0 else "red" if points < 0 else "dim"

    def test_to_dict_cli_table_formatting(self, populated_audit: AuditData):
        """Simulate the CLI table formatting that reads property_count fields."""
        data = populated_audit.to_dict()
        # cli.py:226 formats:
        #   f"{count:,} ({source}, conf={conf:.1%})"
        count = data["property_count"]
        source = data["property_count_source"]
        conf = data["property_count_confidence"]
        formatted = f"{count:,} ({source}, conf={conf:.1%})"
        assert formatted == "1,250 (sitemap, conf=85.0%)"


# ---------------------------------------------------------------------------
# Full auditor integration (uses a mock HTTP server via httpx MockTransport)
# ---------------------------------------------------------------------------


class TestFullAuditor:
    @pytest.fixture
    def mock_response(self):
        """Create a mock httpx response for a real estate site."""
        html = """
        <html lang="bg">
        <head>
            <script type="application/ld+json">
            {"@type": "Product", "name": "Apartment in Sofia"}
            </script>
            <script src="/wp-content/themes/mytheme/app.js"></script>
            <script src="jquery.min.js"></script>
            <script>gtag('config', 'G-XXXXX');</script>
        </head>
        <body>
            <nav>
                <a href="/properties">Properties</a>
                <a href="/about">About</a>
            </nav>
            <div class="property-item">
                <span class="price">€100,000</span>
                <span class="location">Sofia, Bulgaria</span>
                <img src="/img/prop1.jpg" alt="Property 1">
                <p class="description">Beautiful apartment in the center</p>
            </div>
            <div class="property-item">
                <span class="price">€200,000</span>
                <span class="location">Plovdiv, Bulgaria</span>
                <img src="/img/prop2.jpg" alt="Property 2">
                <p class="description">Great house with garden</p>
            </div>
            <p>1,250 properties found</p>
            <iframe src="https://maps.googleapis.com/map/embed?pb=..."></iframe>
        </body>
        </html>
        """
        return httpx.Response(
            200,
            text=html,
            headers={
                "content-type": "text/html",
                "server": "nginx",
            },
            request=httpx.Request("GET", "https://example.com"),
        )

    async def test_audit_with_mock_client(self, mock_response):
        """Run full audit using a mock transport that returns our test HTML."""

        # Create a mock transport that handles different paths
        def mock_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/robots.txt" in url:
                return httpx.Response(
                    200,
                    text="User-agent: *\nAllow: /\nSitemap: https://example.com/sitemap.xml\n",
                    request=request,
                )
            if "/sitemap.xml" in url:
                return httpx.Response(
                    200,
                    text='<?xml version="1.0"?>\n<urlset>\n'
                    + "<url><loc>https://example.com/properties/1</loc></url>\n" * 10
                    + "</urlset>",
                    request=request,
                )
            if "/properties" in url:
                return httpx.Response(
                    200,
                    text="<html><body>1,250 properties found</body></html>",
                    request=request,
                )
            # Default: homepage
            return mock_response

        transport = httpx.MockTransport(mock_handler)
        client = httpx.AsyncClient(transport=transport, follow_redirects=True)

        try:
            result = await audit_website(
                "https://example.com",
                client=client,
            )

            # Verify all checks ran
            assert result.url == "https://example.com"
            assert result.robots.fetched is True
            assert result.robots.allows_scraping is True
            assert len(result.robots.sitemap_urls) > 0

            assert result.anti_scraping.detected is False

            assert result.api_detection.detected is True
            assert result.api_detection.api_type == "json-ld"

            assert result.property_count.count == 1250

            assert result.listing_quality.has_prices is True
            assert result.listing_quality.has_locations is True
            assert result.listing_quality.has_images is True
            assert result.listing_quality.has_property_map is True
            assert result.listing_quality.has_structured_data is True

            assert result.tech_stack.framework == "WordPress"
            assert "WordPress" in result.tech_stack.technologies

            assert result.score > 0
            assert result.score_breakdown != {}

            # Verify serialization
            data = result.to_dict()
            assert data["robots_txt_allows"] is True
            assert data["has_api"] is True
            assert data["property_count"] == 1250
            assert data["technology_stack"] is not None

        finally:
            await client.aclose()

    async def test_audit_returns_error_notes_on_failure(self):
        """Audit should still return an AuditData with notes on connection error."""

        def error_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        transport = httpx.MockTransport(error_handler)
        client = httpx.AsyncClient(transport=transport, follow_redirects=True)

        try:
            result = await audit_website("https://nonexistent.test", client=client)
            # Should have error notes, not crash
            assert result.url == "https://nonexistent.test"
            assert result.score == 0  # no checks completed
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# Auditor: audit_websites (batch)
# ---------------------------------------------------------------------------


class TestAuditWebsitesBatch:
    async def test_audit_multiple_websites(self):
        """Batch audit should return results for all URLs."""

        def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/robots.txt" in str(request.url):
                return httpx.Response(200, text="User-agent: *\nAllow: /\n", request=request)
            return httpx.Response(
                200,
                text="<html><body>500 properties</body></html>",
                headers={"server": "nginx"},
                request=request,
            )

        transport = httpx.MockTransport(mock_handler)
        client = httpx.AsyncClient(transport=transport, follow_redirects=True)

        try:
            urls = [
                "https://site1.example.com",
                "https://site2.example.com",
                "https://site3.example.com",
            ]

            from agency_audit.audit.auditor import audit_websites

            results = await audit_websites(urls, concurrency=2)
            assert len(results) == 3
            for r in results:
                assert r is not None
        finally:
            await client.aclose()
