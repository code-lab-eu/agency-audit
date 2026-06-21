"""Anti-scraping detection module.

Detects:
  - Cloudflare protection (via headers, challenge pages)
  - reCAPTCHA presence
  - Bot detection headers (cf-ray, x-akamai, etc.)
  - JS-only rendering (empty body without JS)
"""

from __future__ import annotations

import logging
import re

import httpx

from agency_audit.audit.models import AntiScrapingResult

logger = logging.getLogger(__name__)

# Cloudflare indicators
CLOUDFLARE_SERVER_VALUES = {"cloudflare", "cloudflare-ng"}
CLOUDFLARE_BODY_PATTERNS = [
    r"__cf_bm",
    r"cdn-cgi/challenge-platform",
    r"cf-browser-verification",
    r"cf-challenge-running",
    r"just a moment",
]

# reCAPTCHA indicators
RECAPTCHA_PATTERNS = [
    r"google\.com/recaptcha",
    r"grecaptcha",
    r"recaptcha/api",
    r"data-sitekey",
]

# Bot detection headers
BOT_DETECTION_HEADERS = {
    "x-akamai-transformed",
    "x-sucuri-id",
    "x-cdn",
    "x-bot-protection",
    "x-anti-bot",
    "x-perimeter",
}

# JS-only rendering indicators
JS_ONLY_PATTERNS = [
    r"noscript",
    r"enable\s+javascript",
    r"requires\s+javascript",
    r"please\s+enable\s+javascript",
]


def _check_cloudflare_headers(headers: httpx.Headers) -> bool:
    """Check response headers for Cloudflare indicators."""
    server = headers.get("server", "").lower()
    if server in CLOUDFLARE_SERVER_VALUES:
        return True
    # cf-ray header is a strong signal
    if "cf-ray" in headers:
        return True
    return False


def _check_bot_detection_headers(headers: httpx.Headers) -> list[str]:
    """Check for bot detection / WAF headers."""
    found = []
    lower_headers = {k.lower(): v for k, v in headers.items()}
    for header_name in BOT_DETECTION_HEADERS:
        if header_name in lower_headers:
            found.append(header_name)
    # Also check for known WAF servers
    server = lower_headers.get("server", "")
    if server in ("sucuri", "sucuri/cloudproxy"):
        found.append("sucuri")
    if "x-sucuri-id" in lower_headers:
        found.append("sucuri")
    return found


def _check_recaptcha(html_text: str) -> bool:
    """Check HTML for reCAPTCHA presence."""
    html_lower = html_text.lower()
    for pattern in RECAPTCHA_PATTERNS:
        if re.search(pattern, html_lower):
            return True
    return False


def _check_cloudflare_body(html_text: str) -> bool:
    """Check HTML body for Cloudflare challenge page indicators."""
    html_lower = html_text.lower()
    for pattern in CLOUDFLARE_BODY_PATTERNS:
        if re.search(pattern, html_lower):
            return True
    return False


def _check_js_only_rendering(html_text: str) -> bool:
    """Detect JS-only rendering: page has minimal content and JS requirements."""
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html_text)
    body = tree.css_first("body")
    if body is None:
        # selectolax may auto-add body to incomplete HTML, so also check
        # if the html tag has no children at all
        html_el = tree.css_first("html")
        if html_el is None or len(html_el.attributes) == 0 and not tree.css("body") and not tree.css("head"):
            return True  # No body = likely JS-only
        return True  # No body = likely JS-only

    # Get text content
    text = body.text(strip=True)
    if not text or len(text) < 200:
        # Empty body with no text content at all = JS-only
        if not text:
            return True
        # Check for JS requirement messages
        html_lower = html_text.lower()
        for pattern in JS_ONLY_PATTERNS:
            if re.search(pattern, html_lower):
                return True
        # Very short body with lots of script tags
        scripts = tree.css("script")
        if len(scripts) > 5 and len(text) < 100:
            return True

    return False


async def detect_anti_scraping(
    url: str,
    response: httpx.Response | None = None,
    client: httpx.AsyncClient | None = None,
) -> AntiScrapingResult:
    """Detect anti-scraping measures on a website.

    Args:
        url: Website URL to check.
        response: Optional pre-fetched response. If not provided, one is fetched.
        client: Optional httpx.AsyncClient for fetching.

    Returns:
        AntiScrapingResult with detection details.
    """
    result = AntiScrapingResult()
    own_client = client is None and response is None
    if own_client:
        client = httpx.AsyncClient(timeout=15, follow_redirects=True)

    try:
        if response is None:
            if client is None:
                raise ValueError("Either response or client must be provided")
            response = await client.get(url)

        html_text = response.text
        headers = response.headers

        # Cloudflare
        cf_header = _check_cloudflare_headers(headers)
        cf_body = _check_cloudflare_body(html_text)
        if cf_header or cf_body:
            result.cloudflare = True
            result.details.append("cloudflare")

        # reCAPTCHA
        result.recaptcha = _check_recaptcha(html_text)
        if result.recaptcha:
            result.details.append("recaptcha")

        # Bot detection headers
        bot_headers = _check_bot_detection_headers(headers)
        if bot_headers:
            result.bot_detection_headers = True
            result.details.extend(bot_headers)

        # JS-only rendering
        result.js_only_rendering = _check_js_only_rendering(html_text)
        if result.js_only_rendering:
            result.details.append("js_only_rendering")

        result.detected = (
            result.cloudflare
            or result.recaptcha
            or result.bot_detection_headers
            or result.js_only_rendering
        )

    except Exception as exc:
        logger.warning("Anti-scraping detection failed for %s: %s", url, exc)
        result.details.append(f"error: {exc}")
    finally:
        if own_client and client:
            await client.aclose()

    return result
