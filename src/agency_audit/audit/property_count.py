"""Property count estimation module.

Attempts to count listed properties from:
  1. Listing pages (parse HTML for listing count indicators)
  2. Sitemap URLs (count <url> entries for listing-like paths)
  3. API responses (if API detected, query for count)
  4. JSON-LD structured data items
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlsplit

import httpx

from agency_audit.audit.models import PropertyCountResult
from agency_audit.config import settings

logger = logging.getLogger(__name__)

# Patterns for counting properties on listing pages
LISTING_COUNT_PATTERNS = [
    # Text patterns with comma-formatted numbers (e.g. "1,250 properties")
    r"([\d,]+)\s+(?:properties|listings|imoti|annunci|annonces|wohnungen|häuser|immobili)",
    r"([\d,]+)\s+results",
    r"([\d,]+)\s+annonces",
    r"showing\s+([\d,]+)\s+(?:of|results)",
    r"([\d,]+)\s+(?:objekt|objekte)",
    r"([\d,]+)\s+(?:nieruchomości|property)",
    # JSON patterns (no commas in JSON numbers)
    r'"total(?:Count|Results|Items|Properties|Listings)"\s*:\s*(\d+)',
    r'"count"\s*:\s*(\d+)',
    r'"totalCount"\s*:\s*(\d+)',
    r'"total"\s*:\s*(\d+)',
]

# CSS selectors for property listing items
LISTING_ITEM_SELECTORS = [
    ".property",
    ".listing",
    ".listing-item",
    ".property-item",
    ".property-card",
    ".listing-card",
    ".estate-item",
    ".imot",
    ".imoti",
    "[data-property-id]",
    "[data-listing-id]",
    ".offer-item",
    ".result-item",
    ".search-result",
]

# URL path patterns for listing pages
LISTING_PATH_PATTERNS = [
    r"/listings?",
    r"/properties?",
    r"/imoti",
    r"/estate",
    r"/real-estate",
    r"/offers?",
    r"/search",
    r"/annonces",
    r"/annunci",
    r"/wohnungen",
    r"/haeuser",
    r"/immobilien",
    r"/nieruchomosci",
]


def _count_from_html(html_text: str) -> tuple[int, float]:
    """Try to count properties from HTML listing page.

    Returns:
        (count, confidence) — confidence is 0.0-1.0.
    """
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html_text)

    # Try to find a count in visible text (not script tags)
    from selectolax.parser import HTMLParser as _HP

    tree = HTMLParser(html_text)

    # Get visible text (remove script/style content)
    for script_tag in tree.css("script"):
        script_tag.decompose()
    for style_tag in tree.css("style"):
        style_tag.decompose()
    visible_text = tree.body.text(separator=" ") if tree.body else html_text

    # Text patterns (high confidence)
    text_patterns = [p for p in LISTING_COUNT_PATTERNS if not p.startswith('"')]
    for pattern in text_patterns:
        match = re.search(pattern, visible_text, re.IGNORECASE)
        if match:
            raw = match.group(1)
            count = int(raw.replace(",", ""))
            if count > 0:
                return count, 0.7

    # Count listing item elements
    max_count = 0
    for selector in LISTING_ITEM_SELECTORS:
        items = tree.css(selector)
        if len(items) > max_count:
            max_count = len(items)

    if max_count > 0:
        # This is a page count, not total — low confidence
        return max_count, 0.3

    # Check JSON data embedded in page (lower confidence)
    json_patterns = [p for p in LISTING_COUNT_PATTERNS if p.startswith('"')]
    # Re-parse original HTML to get script tags (we decomposed them above)
    orig_tree = _HP(html_text)
    scripts = orig_tree.css("script")
    for script in scripts:
        text = script.text()
        if not text:
            continue
        for pattern in json_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                raw = match.group(1)
                count = int(raw.replace(",", ""))
                if count > 0:
                    return count, 0.5

    return 0, 0.0


def _find_listing_page_url(base_url: str, html_text: str) -> str | None:
    """Find a listing page URL from the homepage HTML."""
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html_text)

    # Check nav links for listing page patterns
    for pattern in LISTING_PATH_PATTERNS:
        for link in tree.css("a"):
            href = link.attributes.get("href", "")
            if href and re.search(pattern, href, re.IGNORECASE):
                if href.startswith("http"):
                    return href
                return urljoin(base_url, href)

    return None


async def _count_from_sitemap(
    sitemap_url: str,
    client: httpx.AsyncClient,
) -> tuple[int, float]:
    """Count property URLs from a sitemap.

    Returns:
        (count, confidence)
    """
    try:
        resp = await client.get(sitemap_url, timeout=settings.sitemap_timeout)
        if resp.status_code >= 400:
            return 0, 0.0

        content = resp.text

        # Check if it's a sitemap index (contains <sitemap> entries)
        if "<sitemapindex" in content:
            # Parse sub-sitemaps
            from selectolax.parser import HTMLParser

            tree = HTMLParser(content)
            sub_sitemaps = []
            for loc in tree.css("sitemap > loc"):
                url = loc.text(strip=True)
                if url:
                    sub_sitemaps.append(url)

            total = 0
            for sub_url in sub_sitemaps[:10]:  # limit to first 10 sub-sitemaps
                count, _ = await _count_from_sitemap(sub_url, client)
                total += count
            return total, 0.8 if total > 0 else 0.0

        # Regular sitemap — count <url> entries
        url_count = content.count("<url>") + content.count("<url ")
        if url_count > 0:
            # Try to filter for property-like URLs
            property_urls = 0
            for pattern in LISTING_PATH_PATTERNS:
                property_urls += len(re.findall(pattern, content, re.IGNORECASE))

            if property_urls > 0:
                return property_urls, 0.8

            # If we can't filter, return total URL count as low-confidence
            return url_count, 0.4

    except Exception as exc:
        logger.debug("Sitemap count failed for %s: %s", sitemap_url, exc)

    return 0, 0.0


async def count_properties(
    base_url: str,
    homepage_response: httpx.Response | None = None,
    sitemap_urls: list[str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> PropertyCountResult:
    """Attempt to count listed properties on a website.

    Args:
        base_url: Website URL.
        homepage_response: Optional pre-fetched homepage response.
        sitemap_urls: Optional list of sitemap URLs (from robots.txt).
        client: Optional httpx.AsyncClient.

    Returns:
        PropertyCountResult with count, source, and confidence.
    """
    result = PropertyCountResult()
    own_client = client is None and homepage_response is None
    if own_client:
        client = httpx.AsyncClient(timeout=settings.audit_http_timeout, follow_redirects=True)

    try:
        if homepage_response is None:
            if client is None:
                raise ValueError("Need client or response")
            homepage_response = await client.get(base_url)

        # Strategy 1: Count from homepage HTML directly
        count, conf = _count_from_html(homepage_response.text)
        if count > 0:
            result.count = count
            result.source = "listing_page"
            result.confidence = conf
            return result

        # Strategy 2: Find and fetch a listing page
        listing_url = _find_listing_page_url(base_url, homepage_response.text)
        if listing_url and client:
            try:
                listing_resp = await client.get(listing_url, timeout=settings.audit_http_timeout)
                if listing_resp.status_code < 400:
                    count, conf = _count_from_html(listing_resp.text)
                    if count > 0:
                        result.count = count
                        result.source = "listing_page"
                        result.confidence = conf
                        return result
            except Exception:
                pass

        # Strategy 3: Count from sitemaps
        if sitemap_urls and client:
            for sitemap_url in sitemap_urls[:5]:  # limit
                count, conf = await _count_from_sitemap(sitemap_url, client)
                if count > 0:
                    result.count = count
                    result.source = "sitemap"
                    result.confidence = conf
                    return result

        # Strategy 4: Try common sitemap URL if none found
        if client and not sitemap_urls:
            parts = urlsplit(base_url)
            default_sitemap = f"{parts.scheme}://{parts.netloc}/sitemap.xml"
            count, conf = await _count_from_sitemap(default_sitemap, client)
            if count > 0:
                result.count = count
                result.source = "sitemap"
                result.confidence = conf
                return result

    except Exception as exc:
        logger.warning("Property count failed for %s: %s", base_url, exc)
    finally:
        if own_client and client:
            await client.aclose()

    return result
