"""End-to-end integration tests for the agency-audit pipeline.

Exercises the full flow from URL ingestion through all 7 audit checks
to final JSON report generation. All external HTTP calls are mocked so
these tests run offline and reliably in CI.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx

from agency_audit.audit.auditor import audit_website
from agency_audit.audit.models import AuditData
from agency_audit.audit.scoring import load_scoring_config

# ---------------------------------------------------------------------------
# Mock HTTP transport builders
# ---------------------------------------------------------------------------


def _make_good_site_handler(base_url: str = "https://good-realestate.example.com") -> Callable:
    """Mock handler for a high-quality real estate site with all features."""

    homepage = """\
<html lang="en">
<head>
    <script type="application/ld+json">
    {
      "@type": "Product",
      "name": "Luxury Villa in Sofia",
      "offers": {"price": "250000"}
    }
    </script>
    <script src="/wp-content/themes/realestate-theme/app.js"></script>
</head>
<body>
    <nav>
        <a href="/properties">Properties</a>
        <a href="/about">About</a>
    </nav>
    <div class="property-item">
        <span class="price">€250,000</span>
        <span class="location">Sofia, Bulgaria</span>
        <img src="/img/prop1.jpg" alt="Villa">
        <p class="description">Beautiful villa with pool and garden</p>
    </div>
    <div class="property-item">
        <span class="price">€180,000</span>
        <span class="location">Plovdiv, Bulgaria</span>
        <img src="/img/prop2.jpg" alt="Apartment">
        <p class="description">Spacious apartment in city center</p>
    </div>
    <p>2,500 properties found</p>
    <iframe src="https://maps.googleapis.com/map/embed?pb=..."></iframe>
</body>
</html>
"""

    robots = f"""\
User-agent: *
Allow: /
Sitemap: {base_url}/sitemap.xml
"""

    sitemap = f"""\
<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <url><loc>{base_url}/properties/1</loc></url>
    <url><loc>{base_url}/properties/2</loc></url>
    <url><loc>{base_url}/properties/3</loc></url>
</urlset>
"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/robots.txt" in url:
            return httpx.Response(
                200, text=robots, request=request, headers={"content-type": "text/plain"}
            )
        if "/sitemap.xml" in url:
            return httpx.Response(
                200, text=sitemap, request=request, headers={"content-type": "application/xml"}
            )
        if "/properties" in url and "/sitemap" not in url:
            return httpx.Response(
                200,
                text="<html><body>2,500 properties found</body></html>",
                headers={"server": "nginx"},
                request=request,
            )
        return httpx.Response(
            200,
            text=homepage,
            request=request,
            headers={
                "content-type": "text/html",
                "server": "nginx",
                "content-language": "en",
                "x-powered-by": "WordPress",
            },
        )

    return handler


def _make_bad_site_handler(base_url: str = "https://bad-agency.example.com") -> Callable:
    """Mock handler for a site with anti-scraping and blocked robots.txt."""

    homepage = """\
<html>
<head>
    <script src="https://www.google.com/recaptcha/api.js"></script>
</head>
<body>
    <div class="cf-browser-verification">
    Just a moment... Checking your browser before accessing the site.
    </div>
    <noscript>Please enable JavaScript to view this page.</noscript>
    <script src="/app.js"></script>
    <script src="/vendor.js"></script>
    <script src="/main.js"></script>
    <script src="/chunk.js"></script>
</body>
</html>
"""

    robots = "User-agent: *\nDisallow: /\n"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/robots.txt" in url:
            return httpx.Response(
                200, text=robots, request=request, headers={"content-type": "text/plain"}
            )
        return httpx.Response(
            200,
            text=homepage,
            request=request,
            headers={
                "cf-ray": "abc123def456",
                "server": "cloudflare",
                "content-type": "text/html",
            },
        )

    return handler


def _make_minimal_site_handler(base_url: str = "https://minimal-agency.example.com") -> Callable:
    """Mock handler for a minimal site with few features."""

    homepage = """\
<html lang="bg">
<head><title>Минимална Агенция</title></head>
<body>
    <nav><a href="/imoti">Имоти</a></nav>
    <p>Добре дошли в нашата агенция за недвижими имоти.</p>
    <div class="listing-item">
        <span class="price">€50,000</span>
        <span class="location">Варна</span>
    </div>
    <div class="listing-item">
        <span class="price">€75,000</span>
        <span class="location">Бургас</span>
    </div>
    <div class="listing-item">
        <span class="price">€90,000</span>
    </div>
    <p>150 имота намерени</p>
</body>
</html>
"""

    robots = "User-agent: *\nAllow: /\nCrawl-delay: 5\n"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/robots.txt" in url:
            return httpx.Response(
                200, text=robots, request=request, headers={"content-type": "text/plain"}
            )
        if "/imoti" in url:
            listing_html = "<html><body>150 имота намерени</body></html>"
            return httpx.Response(
                200, text=listing_html, request=request, headers={"server": "Apache/2.4"}
            )
        return httpx.Response(
            200,
            text=homepage,
            request=request,
            headers={
                "server": "Apache/2.4",
                "content-type": "text/html",
                "content-language": "bg",
                "x-powered-by": "PHP/8.1",
            },
        )

    return handler


def _make_csv_url_list() -> list[str]:
    """Simulate reading URLs from a CSV file of agency websites.

    Returns a list of URLs that would be read from a CSV like:
        url,label,country
        https://good-realestate.example.com,Good RE,BG
        https://bad-agency.example.com,Bad Agency,BG
        https://minimal-agency.example.com,Minimal Agency,BG
    """
    return [
        "https://good-realestate.example.com",
        "https://bad-agency.example.com",
        "https://minimal-agency.example.com",
    ]


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestFullPipelineGoodSite:
    """End-to-end pipeline test: good real estate site."""

    async def test_audit_good_site_complete(self):
        """Full pipeline on a well-featured real estate site."""
        transport = httpx.MockTransport(_make_good_site_handler())
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            result = await audit_website("https://good-realestate.example.com", client=client)

        # URL
        assert result.url == "https://good-realestate.example.com"

        # robots.txt
        assert result.robots.fetched is True
        assert result.robots.allows_scraping is True
        assert len(result.robots.sitemap_urls) >= 1

        # anti-scraping
        assert result.anti_scraping.detected is False
        assert result.anti_scraping.cloudflare is False

        # API detection
        assert result.api_detection.detected is True
        assert result.api_detection.api_type in ("json-ld", "rest", "graphql")

        # property count
        assert result.property_count.count >= 1000  # high count tier
        assert result.property_count.source != "unknown"

        # listing quality
        assert result.listing_quality.has_prices is True
        assert result.listing_quality.has_locations is True
        assert result.listing_quality.has_images is True
        assert result.listing_quality.has_descriptions is True
        assert result.listing_quality.has_property_map is True
        assert result.listing_quality.has_structured_data is True
        assert result.listing_quality.quality_score > 0.5

        # tech stack
        assert result.tech_stack.framework is not None

        # metadata
        assert result.language is not None
        assert result.response_time_ms is not None
        assert result.response_time_ms > 0
        # SSL check makes real socket connection — skip assertion for mocked test

        # score
        assert result.score > 60, f"Expected >60, got {result.score}"
        assert len(result.score_breakdown) >= 5  # multiple scoring factors

    async def test_good_site_json_report(self):
        """Verify the generated JSON report for a good site matches expected structure."""
        transport = httpx.MockTransport(_make_good_site_handler())
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            result = await audit_website("https://good-realestate.example.com", client=client)

        report = result.to_dict()

        # Required top-level fields
        required_fields = [
            "url",
            "robots_txt_allows",
            "robots_txt_fetched",
            "has_anti_scraping",
            "has_api",
            "api_type",
            "property_count",
            "property_count_source",
            "has_structured_data",
            "listings_have_prices",
            "listings_have_locations",
            "listings_have_images",
            "listings_have_descriptions",
            "has_property_map",
            "framework",
            "technology_stack",
            "response_time_ms",
            "ssl_valid",
            "language",
            "score",
            "score_breakdown",
        ]
        for field in required_fields:
            assert field in report, f"Missing field: {field}"

        # Type checks
        assert isinstance(report["url"], str)
        assert isinstance(report["robots_txt_allows"], bool)
        assert isinstance(report["score"], int)
        assert isinstance(report["score_breakdown"], dict)

        # JSON serializable
        json_str = json.dumps(report)
        assert json_str is not None
        parsed = json.loads(json_str)
        assert parsed["url"] == "https://good-realestate.example.com"


class TestFullPipelineBadSite:
    """End-to-end pipeline test: site with anti-scraping, blocked robots."""

    async def test_audit_bad_site_negative_score(self):
        """Full pipeline on a blocked/anti-scraping site should produce negative score."""
        transport = httpx.MockTransport(_make_bad_site_handler())
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            result = await audit_website("https://bad-agency.example.com", client=client)

        assert result.url == "https://bad-agency.example.com"
        assert result.robots.allows_scraping is False
        assert result.anti_scraping.detected is True
        assert result.anti_scraping.cloudflare is True
        assert result.anti_scraping.recaptcha is True
        assert result.api_detection.detected is False
        assert result.property_count.count == 0
        assert result.score < 0, f"Expected negative score, got {result.score}"

    async def test_bad_site_report_has_anti_scraping_details(self):
        """Report for bad site should include anti-scraping details."""
        transport = httpx.MockTransport(_make_bad_site_handler())
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            result = await audit_website("https://bad-agency.example.com", client=client)

        report = result.to_dict()
        assert report["has_anti_scraping"] is True
        assert report["cloudflare"] is True
        assert report["recaptcha"] is True
        assert report["robots_txt_allows"] is False
        assert report["property_count"] == 0


class TestFullPipelineMinimalSite:
    """End-to-end pipeline test: minimal site with partial features."""

    async def test_audit_minimal_site(self):
        """Full pipeline on a minimal site with some features."""
        transport = httpx.MockTransport(_make_minimal_site_handler())
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            result = await audit_website("https://minimal-agency.example.com", client=client)

        assert result.url == "https://minimal-agency.example.com"
        assert result.robots.allows_scraping is True
        assert result.robots.crawl_delay == 5.0
        assert result.anti_scraping.detected is False
        assert result.property_count.count >= 3  # counts listing divs
        assert result.listing_quality.has_prices is True
        assert result.language == "bg"
        # Should score moderately — decent features but low property count
        assert 0 < result.score < 80, f"Expected moderate score, got {result.score}"

    async def test_minimal_site_json_report_language(self):
        """Report should capture detected language (bg for Bulgarian)."""
        transport = httpx.MockTransport(_make_minimal_site_handler())
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            result = await audit_website("https://minimal-agency.example.com", client=client)

        report = result.to_dict()
        assert report["language"] == "bg"
        assert report["robots_crawl_delay"] == 5.0


def _make_multi_site_transport() -> httpx.MockTransport:
    """Transport that serves different responses per URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)

        if "good-realestate" in url:
            return _make_good_site_handler("https://good-realestate.example.com")(request)
        if "bad-agency" in url:
            return _make_bad_site_handler("https://bad-agency.example.com")(request)
        if "minimal-agency" in url:
            return _make_minimal_site_handler("https://minimal-agency.example.com")(request)

        return httpx.Response(404, text="Not found", request=request)

    return httpx.MockTransport(handler)


class TestBatchPipeline:
    """End-to-end batch pipeline: audit multiple websites concurrently."""

    async def test_batch_audit_from_csv_urls(self):
        """Simulate CSV input ingestion: audit all URLs from a list."""
        urls = _make_csv_url_list()
        assert len(urls) == 3

        transport = _make_multi_site_transport()
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            # Override audit_websites to use our client
            import asyncio

            from agency_audit.audit.auditor import audit_website as _audit

            semaphore = asyncio.Semaphore(2)

            async def _audit_url(url: str) -> AuditData:
                async with semaphore:
                    return await _audit(url, client=client)

            tasks = [_audit_url(url) for url in urls]
            results = await asyncio.gather(*tasks)

        assert len(results) == 3

        # Get scores and classify
        good_result = next(r for r in results if "good-realestate" in r.url)
        bad_result = next(r for r in results if "bad-agency" in r.url)
        minimal_result = next(r for r in results if "minimal-agency" in r.url)

        assert good_result.score > 60, f"Good site score too low: {good_result.score}"
        assert bad_result.score < 0, f"Bad site score not negative: {bad_result.score}"
        assert minimal_result.score > -20, f"Minimal site score too low: {minimal_result.score}"

        # All should have reports
        for r in results:
            report = r.to_dict()
            assert report["url"] in urls
            assert isinstance(report["score"], int)

    async def test_batch_report_generation(self):
        """Generate a combined JSON report from batch audit results."""
        urls = _make_csv_url_list()

        transport = _make_multi_site_transport()
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            import asyncio

            from agency_audit.audit.auditor import audit_website as _audit

            semaphore = asyncio.Semaphore(2)

            async def _audit_url(url: str) -> AuditData:
                async with semaphore:
                    return await _audit(url, client=client)

            tasks = [_audit_url(url) for url in urls]
            results = await asyncio.gather(*tasks)

        # Generate combined report
        combined_report = {
            "total_sites": len(results),
            "sites": [r.to_dict() for r in results],
            "summary": {
                "average_score": round(sum(r.score for r in results) / len(results), 1),
                "max_score": max(r.score for r in results),
                "min_score": min(r.score for r in results),
                "scrapable": sum(1 for r in results if r.robots.allows_scraping),
                "with_api": sum(1 for r in results if r.api_detection.detected),
                "total_properties": sum(r.property_count.count for r in results),
            },
        }

        # Verify report structure
        assert combined_report["total_sites"] == 3
        assert len(combined_report["sites"]) == 3
        assert "summary" in combined_report

        summary = combined_report["summary"]
        assert summary["scrapable"] >= 2  # good and minimal
        assert summary["with_api"] >= 1  # good site
        assert summary["total_properties"] > 0

        # JSON serializable
        json_str = json.dumps(combined_report)
        assert json_str is not None
        parsed = json.loads(json_str)
        assert parsed["total_sites"] == 3
        assert len(parsed["sites"]) == 3


class TestPipelineEdgeCases:
    """End-to-end pipeline edge cases."""

    async def test_audit_url_without_scheme(self):
        """Pipeline should handle URLs without http/https prefix."""

        def handler(request: httpx.Request) -> httpx.Response:
            if "/robots.txt" in str(request.url):
                return httpx.Response(200, text="User-agent: *\nAllow: /\n", request=request)
            return httpx.Response(
                200,
                text="<html><body>100 properties</body></html>",
                headers={"server": "nginx"},
                request=request,
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            result = await audit_website("example.com", client=client)

        assert result.url.startswith("https://")
        assert result.robots.allows_scraping is True
        assert result.score > 0

    async def test_audit_http_url(self):
        """Pipeline should handle http:// URLs (SSL will be invalid)."""

        def handler(request: httpx.Request) -> httpx.Response:
            if "/robots.txt" in str(request.url):
                return httpx.Response(200, text="User-agent: *\nAllow: /\n", request=request)
            return httpx.Response(
                200,
                text="<html><body>500 properties</body></html>",
                headers={"server": "nginx"},
                request=request,
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            result = await audit_website("http://plain-http.example.com", client=client)

        assert result.url == "http://plain-http.example.com"
        # http:// URLs are not SSL
        assert result.ssl_valid is False

    async def test_audit_error_handling(self):
        """Pipeline should handle connection errors gracefully."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            result = await audit_website("https://offline.example.com", client=client)

        assert result.url == "https://offline.example.com"
        assert result.score == 0
        assert "Error" in result.notes or "error" in result.notes.lower()

    async def test_audit_scoring_config_passed_through(self):
        """Custom scoring config should propagage through the pipeline."""
        custom_config = load_scoring_config().copy()
        custom_config["robots_allows"] = 999
        custom_config["min_score"] = -100
        custom_config["max_score"] = 100

        def handler(request: httpx.Request) -> httpx.Response:
            if "/robots.txt" in str(request.url):
                return httpx.Response(200, text="User-agent: *\nAllow: /\n", request=request)
            return httpx.Response(
                200,
                text="<html><body>10 properties</body></html>",
                headers={"server": "nginx"},
                request=request,
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            result = await audit_website(
                "https://custom-score.example.com",
                client=client,
                scoring_config=custom_config,
            )

        # With robots_allows=999, score should be clamped to 100
        assert result.score == 100
        assert result.score_breakdown["robots_allows"] == 999  # raw value in breakdown


class TestReportConsistency:
    """Verify report consistency across the pipeline."""

    async def test_score_breakdown_sum_consistent(self):
        """Score breakdown values should sum to score (or clamped)."""
        transport = httpx.MockTransport(_make_good_site_handler())
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            result = await audit_website("https://good-realestate.example.com", client=client)

        breakdown_sum = sum(result.score_breakdown.values())
        # Either matches exactly or clamped at boundaries
        assert breakdown_sum == result.score or result.score in (100, -100), (
            f"Breakdown sum {breakdown_sum} != score {result.score}"
        )

    async def test_to_dict_fields_count(self):
        """to_dict() should produce a consistent set of fields across audits."""
        urls = ["https://good-realestate.example.com", "https://minimal-agency.example.com"]

        good_handler = _make_good_site_handler()
        minimal_handler = _make_minimal_site_handler()

        reports = []
        for url, handler_fn in zip(urls, [good_handler, minimal_handler], strict=False):
            transport = httpx.MockTransport(handler_fn)
            async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
                result = await audit_website(url, client=client)
            reports.append(result.to_dict())

        # Both reports should have the same set of fields
        assert set(reports[0].keys()) == set(reports[1].keys()), (
            f"Field mismatch: {set(reports[0].keys()) ^ set(reports[1].keys())}"
        )
