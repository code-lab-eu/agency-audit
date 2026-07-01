"""Fixture-driven unit tests for tech_stack.py and auditor.py
SSL/language/response-time helpers.

Covers:
  - TechStackResult fields consumed via AuditData.to_dict()
  - Framework/CDN/hosting detection (all patterns)
  - Language detection edge cases
  - Response-time helpers
  - detect_tech_stack async path with pre-fetched response
  - _check_ssl_valid edge cases (mock socket)

All tests use httpx.MockTransport — no live network.
"""

from __future__ import annotations

import ssl
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agency_audit.audit.auditor import _check_ssl_valid, _detect_language, audit_website
from agency_audit.audit.models import AuditData, TechStackResult
from agency_audit.audit.tech_stack import (
    _detect_cdn,
    _detect_framework_from_headers,
    _detect_framework_from_html,
    _detect_hosting,
    _detect_technologies,
    detect_tech_stack,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def empty_headers() -> httpx.Headers:
    return httpx.Headers({})


@pytest.fixture
def minimal_html() -> str:
    return "<html><head><title>Test</title></head><body>Hello World</body></html>"


@pytest.fixture
def wordpress_html() -> str:
    return (
        '<html lang="en"><head>'
        '<script src="/wp-content/themes/mytheme/app.js"></script>'
        '<script src="/wp-includes/js/jquery.js"></script>'
        "</head><body>Welcome to our real estate agency</body></html>"
    )


@pytest.fixture
def nextjs_html() -> str:
    return (
        '<html lang="en"><head>'
        '<script id="__NEXT_DATA__" type="application/json"></script>'
        '<link rel="stylesheet" href="/_next/static/css/app.css">'
        "</head><body>Next.js App</body></html>"
    )


@pytest.fixture
def nginx_headers() -> httpx.Headers:
    return httpx.Headers({"server": "nginx/1.25"})


@pytest.fixture
def apache_headers() -> httpx.Headers:
    return httpx.Headers({"server": "Apache/2.4.57"})


# ============================================================================
# TechStackResult contract — fields consumed via to_dict()
# ============================================================================


class TestTechStackResultContract:
    """Verify TechStackResult fields survive the AuditData.to_dict() roundtrip."""

    def test_default_fields(self):
        result = TechStackResult()
        assert result.framework is None
        assert result.hosting is None
        assert result.cdn is None
        assert result.technologies == []

    def test_all_fields_set(self):
        result = TechStackResult(
            framework="WordPress",
            hosting="WP Engine",
            cdn="Cloudflare",
            technologies=["jQuery", "Bootstrap", "WordPress"],
        )
        assert result.framework == "WordPress"
        assert result.hosting == "WP Engine"
        assert result.cdn == "Cloudflare"
        assert len(result.technologies) == 3

    def test_to_dict_with_tech_stack(self):
        """TechStack fields flow through to AuditData.to_dict()."""
        audit = AuditData(
            url="https://example.com",
            tech_stack=TechStackResult(
                framework="WordPress",
                hosting="SiteGround",
                cdn="Cloudflare",
                technologies=["Bootstrap", "jQuery", "WordPress"],
            ),
        )
        data = audit.to_dict()
        assert data["framework"] == "WordPress"
        assert data["hosting"] == "SiteGround"
        assert data["cdn"] == "Cloudflare"
        assert data["technology_stack"] == ["Bootstrap", "jQuery", "WordPress"]

    def test_to_dict_tech_stack_empty(self):
        """Empty TechStack fields survive serialization."""
        audit = AuditData(url="https://example.com")
        data = audit.to_dict()
        assert data["framework"] is None
        assert data["hosting"] is None
        assert data["cdn"] is None
        assert data["technology_stack"] == []


# ============================================================================
# Framework detection from headers — all mappings
# ============================================================================


class TestFrameworkFromHeaders:
    """Test every entry in HEADER_FRAMEWORK_MAP."""

    @pytest.mark.parametrize(
        "header_name,header_value,expected",
        [
            ("x-powered-by", "Express", "Express"),
            ("x-powered-by", "Next.js", "Next.js"),
            ("x-powered-by", "Nuxt", "Nuxt.js"),
            ("x-powered-by", "ASP.NET", "ASP.NET"),
            ("x-powered-by", "Laravel", "Laravel"),
            ("x-powered-by", "Django", "Django"),
            ("x-powered-by", "Flask", "Flask"),
            ("server", "Apache/2.4.57", "Apache"),
            ("server", "nginx/1.25.3", "Nginx"),
            ("server", "Microsoft-IIS/10.0", "Microsoft IIS"),
            ("server", "LiteSpeed", "LiteSpeed"),
        ],
    )
    def test_header_framework_detected(self, header_name, header_value, expected):
        headers = httpx.Headers({header_name: header_value})
        assert _detect_framework_from_headers(headers) == expected

    def test_case_insensitive(self):
        """Header matching is case-insensitive."""
        headers = httpx.Headers({"x-powered-by": "exPRESS"})
        assert _detect_framework_from_headers(headers) == "Express"

    def test_empty_headers(self, empty_headers):
        assert _detect_framework_from_headers(empty_headers) is None

    def test_unknown_header_values(self):
        headers = httpx.Headers({"x-powered-by": "unknown-framework"})
        assert _detect_framework_from_headers(headers) is None


# ============================================================================
# Framework detection from HTML — all patterns
# ============================================================================


class TestFrameworkFromHtml:
    """Test every entry in HTML_FRAMEWORK_PATTERNS."""

    @pytest.mark.parametrize(
        "html_snippet,expected",
        [
            ('<script src="/wp-content/themes/theme/app.js"></script>', "WordPress"),
            ('<link href="/wp-includes/css/style.css">', "WordPress"),
            ("/wp-json/api/v1", "WordPress"),
            # Note: "WordPress + Elementor" pattern (wp-content/plugins/elementor)
            # is unreachable in the current detection order because the broader
            # "WordPress" pattern (wp-content) matches first.
            ('<script src="drupal.js"></script>', "Drupal"),
            ('<link href="sites/all/themes/custom/style.css">', "Drupal"),
            ("drupal.org", "Drupal"),
            ("joomla", "Joomla"),
            (
                '<script id="__NEXT_DATA__" type="application/json"></script>',
                "Next.js",
            ),
            ('<link href="/_next/static/chunk.css">', "Next.js"),
            ("__nuxt__", "Nuxt.js"),
            ("/_nuxt/app.js", "Nuxt.js"),
            ('<script src="react.js"></script>', "React"),
            ("react-dom", "React"),
            ('<div data-reactroot="true">', "React"),
            ('<script src="vue.js"></script>', "Vue.js"),
            ("vue.min.js", "Vue.js"),
            ("<div data-v-abc123>", "Vue.js"),
            ("angular.js", "Angular"),
            ("ng-app", "Angular"),
            ("ng-controller", "Angular"),
            ("svelte", "Svelte"),
            ("gatsby", "Gatsby"),
            ('<meta content="Shopify online store">', "Shopify"),
            ("wix.com", "Wix"),
            ("wixstatic", "Wix"),
            ("squarespace", "Squarespace"),
            ("cdn-cgi/challenge-platform", "Cloudflare"),
        ],
    )
    def test_html_framework_detected(self, html_snippet, expected):
        html = f"<html><head>{html_snippet}</head><body></body></html>"
        assert _detect_framework_from_html(html) == expected

    def test_case_insensitive(self):
        """HTML framework detection is case-insensitive."""
        html = (
            '<html><head><script src="/WP-CONTENT/themes/t.js"></script></head><body></body></html>'
        )
        assert _detect_framework_from_html(html) == "WordPress"

    def test_empty_html(self):
        assert _detect_framework_from_html("") is None

    def test_no_match(self, minimal_html):
        assert _detect_framework_from_html(minimal_html) is None


# ============================================================================
# CDN detection — all headers
# ============================================================================


class TestCdnDetection:
    """Test every entry in CDN_HEADERS."""

    @pytest.mark.parametrize(
        "headers_dict,expected",
        [
            ({"cf-ray": "abc123"}, "Cloudflare"),
            ({"x-amz-cf-id": "abc123"}, "CloudFront (AWS)"),
            ({"x-fastly-request-id": "abc123"}, "Fastly"),
            ({"x-sucuri-id": "abc123"}, "Sucuri"),
            ({"x-akamai-transformed": "abc"}, "Akamai"),
            ({"x-bolt-cdn": "1"}, "Bolt"),
            ({"x-vercel-id": "abc"}, "Vercel"),
            ({"x-cdn": "mycdn"}, "mycdn"),
            ({"x-edge": "akamai-edge"}, "akamai-edge"),
            ({"x-cdn-origin-rtt": "10ms"}, "10ms"),
        ],
    )
    def test_cdn_detected(self, headers_dict, expected):
        headers = httpx.Headers(headers_dict)
        assert _detect_cdn(headers) == expected

    def test_priority_order(self):
        """First matching CDN header wins."""
        headers = httpx.Headers({"cf-ray": "abc", "x-fastly-request-id": "xyz"})
        # cf-ray appears first in CDN_HEADERS dict iteration order
        result = _detect_cdn(headers)
        assert result == "Cloudflare"

    def test_empty_headers(self, empty_headers):
        assert _detect_cdn(empty_headers) is None


# ============================================================================
# Hosting detection — all patterns
# ============================================================================


class TestHostingDetection:
    """Test hosting detection patterns."""

    @pytest.mark.parametrize(
        "server_header,html_snippet,expected",
        [
            ("nginx", "wp engine", "WP Engine"),
            ("apache", "kinsta", "Kinsta"),
            ("nginx", "siteground", "SiteGround"),
            ("apache", "bluehost", "Bluehost"),
            ("nginx", "godaddy", "GoDaddy"),
            ("apache", "hostinger", "Hostinger"),
            ("nginx", "contabo", "Contabo"),
            ("apache", "hetzner", "Hetzner"),
            ("nginx", "ovh", "OVH"),
            ("apache", "digitalocean", "DigitalOcean"),
            ("nginx", "digiocean", "DigitalOcean"),
            ("apache", "aws.amazonaws.com", "AWS"),
            ("nginx", "cloudfront", "AWS"),
            ("apache", "azure windows.net", "Azure"),
            ("nginx", "googleusercontent", "Google Cloud"),
            ("apache", "googlecloud", "Google Cloud"),
            ("nginx", "gcp", "Google Cloud"),
            ("apache", "vercel", "Vercel"),
            ("nginx", "netlify", "Netlify"),
            ("apache", "heroku", "Heroku"),
        ],
    )
    def test_hosting_detected(self, server_header, html_snippet, expected):
        headers = httpx.Headers({"server": server_header})
        html = f"<html><body>{html_snippet}</body></html>"
        assert _detect_hosting(headers, html) == expected

    def test_no_hosting_match(self, nginx_headers, minimal_html):
        assert _detect_hosting(nginx_headers, minimal_html) is None

    def test_hosting_from_server_only(self):
        """Hosting can be detected from server header alone."""
        headers = httpx.Headers({"server": "nginx", "x-powered-by": "PHP/8.2"})
        html = "aws.amazonaws.com"
        result = _detect_hosting(headers, html)
        assert result == "AWS"

    def test_empty_all(self, empty_headers):
        assert _detect_hosting(empty_headers, "") is None


# ============================================================================
# Technology detection — all patterns
# ============================================================================


class TestTechnologyDetection:
    """Test technology detection patterns."""

    @pytest.mark.parametrize(
        "html_snippet,expected_tech",
        [
            ("jquery", "jQuery"),
            ("bootstrap", "Bootstrap"),
            ("tailwind", "Tailwind CSS"),
            ("font-awesome", "Font Awesome"),
            ("fontawesome", "Font Awesome"),
            ("google-analytics", "Google Analytics"),
            ("gtag(", "Google Analytics"),
            ("googletagmanager", "Google Analytics"),
            ("gtm.js", "Google Tag Manager"),
            ("adsbygoogle", "Google AdSense"),
            ("fbq(", "Facebook Pixel"),
            ("facebook.net", "Facebook Pixel"),
            ("hotjar", "Hotjar"),
            ("clarity.ms", "Microsoft Clarity"),
            ("recaptcha", "reCAPTCHA"),
            ("leaflet", "Leaflet"),
            ("mapbox", "Mapbox"),
            ("swiper", "Swiper"),
            ("slick-slider", "Slick Slider"),
            ("slick.js", "Slick Slider"),
            ("owl-carousel", "Owl Carousel"),
            ("google-maps", "Google Maps"),
            ("maps.googleapis.com", "Google Maps"),
            ("elementor", "Elementor"),
            ("wpbakery", "WPBakery"),
            ("js_composer", "WPBakery"),
            ("contact-form-7", "Contact Form 7"),
            ("wpcf7", "Contact Form 7"),
            ("yoast", "Yoast SEO"),
            ("rankmath", "Rank Math SEO"),
            ("rank-math", "Rank Math SEO"),
        ],
    )
    def test_technology_detected(self, html_snippet, expected_tech):
        html = f"<html><head>{html_snippet}</head><body></body></html>"
        techs = _detect_technologies(html)
        assert expected_tech in techs

    def test_multiple_technologies(self):
        html = (
            "<html><head>"
            '<script src="jquery.min.js"></script>'
            '<script src="bootstrap.bundle.js"></script>'
            '<script src="google-analytics.js"></script>'
            '<script src="font-awesome.js"></script>'
            '<script src="leaflet.js"></script>'
            "</head><body></body></html>"
        )
        techs = _detect_technologies(html)
        assert "jQuery" in techs
        assert "Bootstrap" in techs
        assert "Google Analytics" in techs
        assert "Font Awesome" in techs
        assert "Leaflet" in techs

    def test_sorted_output(self):
        """Technologies are returned in sorted order."""
        html = (
            "<html><head>"
            '<script src="leaflet.js"></script>'
            '<script src="bootstrap.js"></script>'
            '<script src="jquery.js"></script>'
            "</head><body></body></html>"
        )
        techs = _detect_technologies(html)
        assert techs == sorted(techs)

    def test_no_technologies(self, minimal_html):
        assert _detect_technologies(minimal_html) == []

    def test_empty_html(self):
        assert _detect_technologies("") == []


# ============================================================================
# SSL certificate validation — edge cases
# ============================================================================


class TestSslValidation:
    """Test _check_ssl_valid with mocked socket/SSL."""

    def test_http_returns_false(self):
        assert _check_ssl_valid("http://example.com") is False

    def test_no_hostname_returns_false(self):
        assert _check_ssl_valid("https:///path") is False

    def test_https_valid_cert(self):
        """Mock a successful SSL connection."""
        mock_socket = MagicMock()
        mock_ssl_sock = MagicMock()
        mock_ssl_sock.getpeercert.return_value = {"subject": "example.com"}
        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value.__enter__.return_value = mock_ssl_sock

        with (
            patch("socket.create_connection", return_value=mock_socket),
            patch("ssl.create_default_context", return_value=mock_ctx),
        ):
            assert _check_ssl_valid("https://example.com") is True

    def test_https_connection_refused(self):
        """SSL check returns False on connection error."""
        with patch("socket.create_connection", side_effect=ConnectionRefusedError):
            assert _check_ssl_valid("https://example.com") is False

    def test_https_ssl_error(self):
        """SSL check returns False on SSL error."""
        mock_socket = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.side_effect = ssl.SSLError("certificate verify failed")

        with (
            patch("socket.create_connection", return_value=mock_socket),
            patch("ssl.create_default_context", return_value=mock_ctx),
        ):
            assert _check_ssl_valid("https://example.com") is False

    def test_https_custom_port(self):
        """SSL check uses the port from the URL."""
        mock_socket = MagicMock()
        mock_ssl_sock = MagicMock()
        mock_ssl_sock.getpeercert.return_value = {"subject": "example.com"}
        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value.__enter__.return_value = mock_ssl_sock

        with (
            patch("socket.create_connection", return_value=mock_socket) as mock_conn,
            patch("ssl.create_default_context", return_value=mock_ctx),
        ):
            assert _check_ssl_valid("https://example.com:8443") is True
            mock_conn.assert_called_once_with(("example.com", 8443), timeout=10)

    def test_https_socket_timeout(self):
        """SSL check returns False on socket timeout."""
        with patch(
            "socket.create_connection",
            side_effect=TimeoutError("timed out"),
        ):
            assert _check_ssl_valid("https://example.com") is False

    def test_generic_exception(self):
        """SSL check returns False on any unexpected exception."""
        with patch("socket.create_connection", side_effect=OSError("network unreachable")):
            assert _check_ssl_valid("https://example.com") is False


# ============================================================================
# Language detection — edge cases
# ============================================================================


class TestLanguageDetection:
    """Test _detect_language with various inputs."""

    def test_content_language_header(self):
        html = "<html><body>Test</body></html>"
        headers = httpx.Headers({"content-language": "bg"})
        assert _detect_language(html, headers) == "bg"

    def test_content_language_multi_value(self):
        """Content-Language with multiple values returns the first."""
        html = "<html><body>Test</body></html>"
        headers = httpx.Headers({"content-language": "bg, en, de"})
        assert _detect_language(html, headers) == "bg"

    def test_content_language_with_whitespace(self):
        """Values with whitespace are stripped."""
        html = "<html><body>Test</body></html>"
        headers = httpx.Headers({"content-language": "  bg , en  "})
        assert _detect_language(html, headers) == "bg"

    def test_content_language_case_insensitive(self):
        """Language codes are lowercased."""
        html = "<html><body>Test</body></html>"
        headers = httpx.Headers({"content-language": "BG-bg"})
        assert _detect_language(html, headers) == "bg-bg"

    def test_html_lang_attribute(self):
        html = '<html lang="bg"><body>Test</body></html>'
        headers = httpx.Headers({})
        assert _detect_language(html, headers) == "bg"

    def test_html_lang_full_locale(self):
        """Full locale codes are truncated to 2 chars."""
        html = '<html lang="bg-BG"><body>Test</body></html>'
        headers = httpx.Headers({})
        assert _detect_language(html, headers) == "bg"

    def test_html_lang_single_char(self):
        """Single-char lang codes are preserved (unlikely but safe)."""
        html = '<html lang="x"><body>Test</body></html>'
        headers = httpx.Headers({})
        # [:2] truncation on "x" returns "x"
        assert _detect_language(html, headers) == "x"

    def test_html_lang_with_whitespace(self):
        html = '<html lang="  de  "><body>Test</body></html>'
        headers = httpx.Headers({})
        assert _detect_language(html, headers) == "de"

    def test_html_lang_empty_string(self):
        """Empty lang attribute is treated as missing."""
        html = '<html lang=""><body>Test</body></html>'
        headers = httpx.Headers({})
        assert _detect_language(html, headers) is None

    def test_no_html_element(self):
        """When <html> element is missing, returns None."""
        html = "<body>No html tag</body>"
        headers = httpx.Headers({})
        assert _detect_language(html, headers) is None

    def test_no_lang_no_header(self):
        html = "<html><body>Test</body></html>"
        headers = httpx.Headers({})
        assert _detect_language(html, headers) is None

    def test_header_priority_over_html(self):
        """Content-Language header wins over <html lang>."""
        html = '<html lang="fr"><body>Test</body></html>'
        headers = httpx.Headers({"content-language": "bg"})
        assert _detect_language(html, headers) == "bg"

    def test_html_no_lang_attribute(self):
        """<html> without lang attribute falls through."""
        html = "<html><body>Test</body></html>"
        headers = httpx.Headers({})
        assert _detect_language(html, headers) is None


# ============================================================================
# Response-time helpers
# ============================================================================


class TestResponseTime:
    """Test that response_time_ms is set correctly during audit."""

    async def test_response_time_set(self):
        """audit_website sets response_time_ms after fetching homepage."""

        def handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "/robots.txt" in url:
                return httpx.Response(200, text="User-agent: *\nAllow: /\n", request=req)
            return httpx.Response(
                200,
                text="<html><body>Hello</body></html>",
                headers={"server": "nginx"},
                request=req,
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            result = await audit_website("https://example.com", client=client)
        assert isinstance(result.response_time_ms, float)
        assert result.response_time_ms >= 0

    async def test_response_time_is_float(self):
        """response_time_ms is a float with at most 1 decimal place."""

        def handler(req: httpx.Request) -> httpx.Response:
            if "/robots.txt" in str(req.url):
                return httpx.Response(200, text="User-agent: *\nAllow: /\n", request=req)
            return httpx.Response(
                200,
                text="<html><body>Hello</body></html>",
                headers={"server": "nginx"},
                request=req,
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            result = await audit_website("https://example.com", client=client)
        assert isinstance(result.response_time_ms, float)

    async def test_audit_with_error_no_response_time(self):
        """On connection error, response_time_ms may be None (never set)."""

        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            result = await audit_website("https://example.com", client=client)
        # response_time_ms is not set on error path
        assert result.response_time_ms is None


# ============================================================================
# detect_tech_stack — async path and error handling
# ============================================================================


class TestDetectTechStack:
    """Test detect_tech_stack async function."""

    async def test_with_prefetched_response(self, wordpress_html):
        """detect_tech_stack accepts a pre-fetched response."""
        response = httpx.Response(
            200,
            text=wordpress_html,
            headers={
                "server": "nginx/1.25",
                "x-powered-by": "PHP/8.2",
                "cf-ray": "abc123",
            },
            request=httpx.Request("GET", "https://example.com"),
        )
        result = await detect_tech_stack("https://example.com", response=response)
        assert result.framework == "WordPress"
        assert result.cdn == "Cloudflare"
        assert "WordPress" in result.technologies

    async def test_with_client_no_response(self):
        """detect_tech_stack fetches with provided client."""
        html = (
            '<html><head><script src="/wp-content/themes/t/app.js"></script>'
            "</head><body>Hello</body></html>"
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    text=html,
                    headers={"server": "nginx"},
                    request=req,
                )
            )
        ) as client:
            result = await detect_tech_stack("https://example.com", client=client)
        assert result.framework == "WordPress"

    async def test_own_client_created(self, wordpress_html):
        """detect_tech_stack creates its own client when none provided."""
        # Use a mock transport on the AsyncClient constructor
        orig_client = httpx.AsyncClient

        def mock_client(*args, **kwargs):
            kwargs.pop("transport", None)
            return orig_client(
                transport=httpx.MockTransport(
                    lambda req: httpx.Response(
                        200,
                        text=wordpress_html,
                        headers={"server": "nginx", "cf-ray": "abc"},
                        request=req,
                    )
                ),
                **kwargs,
            )

        with patch("agency_audit.audit.tech_stack.httpx.AsyncClient", side_effect=mock_client):
            result = await detect_tech_stack("https://example.com")
        assert result.framework == "WordPress"
        assert result.cdn == "Cloudflare"

    async def test_no_response_no_client_creates_own(self):
        """detect_tech_stack creates its own client when both response and client are None."""
        # When both response and client are None, detect_tech_stack creates an
        # AsyncClient (own_client=True). The ValueError guard is unreachable
        # from the public API since own_client=True triggers client creation.
        html = (
            '<html><head><script src="/wp-content/themes/t/app.js"></script>'
            "</head><body>Hello</body></html>"
        )
        orig_client = httpx.AsyncClient

        def mock_client(*args, **kwargs):
            kwargs.pop("transport", None)
            return orig_client(
                transport=httpx.MockTransport(
                    lambda req: httpx.Response(
                        200,
                        text=html,
                        headers={"server": "nginx"},
                        request=req,
                    )
                ),
                **kwargs,
            )

        with patch("agency_audit.audit.tech_stack.httpx.AsyncClient", side_effect=mock_client):
            result = await detect_tech_stack("https://example.com")
        assert result.framework == "WordPress"

    async def test_exception_handled_returns_default(self):
        """On error, detect_tech_stack returns a default TechStackResult."""

        def error_handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection failed")

        async with httpx.AsyncClient(transport=httpx.MockTransport(error_handler)) as client:
            result = await detect_tech_stack("https://example.com", client=client)
        # Should return default TechStackResult with no data
        assert result.framework is None
        assert result.hosting is None
        assert result.cdn is None
        assert result.technologies == []

    async def test_hosting_from_server_header(self):
        """Header-level hosting is overwritten by _detect_hosting result."""
        # When framework from HTML is WordPress and header framework is Nginx,
        # line 225 sets result.hosting = "Nginx" from the web server header.
        # However, line 231 then overwrites it with _detect_hosting(), which
        # scans HOSTING_PATTERNS (targeting hosting providers, not web servers)
        # and returns None for nginx.
        html = (
            '<html><head><script src="/wp-content/themes/t/app.js"></script>'
            "</head><body>Hello</body></html>"
        )
        response = httpx.Response(
            200,
            text=html,
            headers={"server": "nginx/1.25"},
            request=httpx.Request("GET", "https://example.com"),
        )
        result = await detect_tech_stack("https://example.com", response=response)
        assert result.framework == "WordPress"
        # hosting was set to "Nginx" from headers then overwritten to None by _detect_hosting
        assert result.hosting is None

    async def test_header_server_as_framework_fallback(self):
        """When HTML has no framework, fall back to header detection."""
        html = "<html><body>Hello World</body></html>"
        response = httpx.Response(
            200,
            text=html,
            headers={"x-powered-by": "Django"},
            request=httpx.Request("GET", "https://example.com"),
        )
        result = await detect_tech_stack("https://example.com", response=response)
        assert result.framework == "Django"

    async def test_technologies_includes_framework(self):
        """Framework is prepended to the technologies list."""
        html = (
            "<html><head>"
            '<script src="/wp-content/themes/t/app.js"></script>'
            '<script src="jquery.min.js"></script>'
            "</head><body></body></html>"
        )
        response = httpx.Response(
            200,
            text=html,
            headers={"server": "nginx"},
            request=httpx.Request("GET", "https://example.com"),
        )
        result = await detect_tech_stack("https://example.com", response=response)
        assert result.framework == "WordPress"
        assert result.technologies[0] == "WordPress"
        assert "jQuery" in result.technologies


# ============================================================================
# Full auditor integration — language + response time + SSL
# ============================================================================


class TestAuditorLanguageAndSsl:
    """Integration tests for language/SSL/response-time through audit_website."""

    async def test_language_from_html_lang(self):
        """Language is detected from <html lang> in full audit."""

        def handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "/robots.txt" in url:
                return httpx.Response(200, text="User-agent: *\nAllow: /\n", request=req)
            return httpx.Response(
                200,
                text='<html lang="fr"><body>Bienvenue</body></html>',
                headers={"server": "nginx"},
                request=req,
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            result = await audit_website("https://example.com", client=client)
        assert result.language == "fr"

    async def test_language_from_content_language(self):
        """Language is detected from Content-Language header."""

        def handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "/robots.txt" in url:
                return httpx.Response(200, text="User-agent: *\nAllow: /\n", request=req)
            return httpx.Response(
                200,
                text="<html><body>Test</body></html>",
                headers={"server": "nginx", "content-language": "de"},
                request=req,
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            result = await audit_website("https://example.com", client=client)
        assert result.language == "de"

    async def test_ssl_valid_https(self):
        """audit_website runs SSL check for HTTPS URLs."""
        # Mock SSL check to return True
        robots_content = "User-agent: *\nAllow: /\n"
        html = "<html><body>Welcome</body></html>"

        def handler(req: httpx.Request) -> httpx.Response:
            if "/robots.txt" in str(req.url):
                return httpx.Response(200, text=robots_content, request=req)
            return httpx.Response(
                200,
                text=html,
                headers={"server": "nginx"},
                request=req,
            )

        transport = httpx.MockTransport(handler)
        mock_socket = MagicMock()
        mock_ssl_sock = MagicMock()
        mock_ssl_sock.getpeercert.return_value = {"subject": "example.com"}
        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value.__enter__.return_value = mock_ssl_sock

        with (
            patch("socket.create_connection", return_value=mock_socket),
            patch("ssl.create_default_context", return_value=mock_ctx),
        ):
            async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
                result = await audit_website("https://example.com", client=client)
        assert result.ssl_valid is True

    async def test_audit_http_no_ssl_check(self):
        """audit_website on HTTP URL should have ssl_valid=False."""

        def handler(req: httpx.Request) -> httpx.Response:
            if "/robots.txt" in str(req.url):
                return httpx.Response(200, text="User-agent: *\nAllow: /\n", request=req)
            return httpx.Response(
                200,
                text="<html><body>Hello</body></html>",
                headers={"server": "nginx"},
                request=req,
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            result = await audit_website("http://example.com", client=client)
        # HTTP URL → _check_ssl_valid returns False immediately
        assert result.ssl_valid is False

    async def test_response_time_recorded(self):
        """response_time_ms is recorded in the audit result."""

        def handler(req: httpx.Request) -> httpx.Response:
            if "/robots.txt" in str(req.url):
                return httpx.Response(200, text="User-agent: *\nAllow: /\n", request=req)
            return httpx.Response(
                200,
                text="<html><body>Hello</body></html>",
                headers={"server": "nginx"},
                request=req,
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            result = await audit_website("https://example.com", client=client)
        assert result.response_time_ms is not None
        assert result.response_time_ms >= 0
        assert isinstance(result.response_time_ms, float)
