"""Main auditor module — orchestrates all audit checks for a website.

Runs all 7 checks in sequence:
  1. robots.txt fetch & parse
  2. anti-scraping detection
  3. API detection
  4. property count
  5. listing quality
  6. tech stack detection
  7. scoring (combines all into 0-100)

Uses httpx for HTTP requests, selectolax for HTML parsing.
Playwright is used on-demand only for JS-heavy sites (when anti-scraping
detection indicates JS-only rendering).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from urllib.parse import urlsplit

import httpx

from agency_audit.audit.anti_scraping import detect_anti_scraping
from agency_audit.audit.api_detection import detect_api
from agency_audit.audit.listing_quality import assess_listing_quality
from agency_audit.audit.models import AuditData
from agency_audit.audit.property_count import _find_listing_page_url, count_properties
from agency_audit.audit.robots import DEFAULT_USER_AGENT, fetch_robots_txt
from agency_audit.audit.scoring import compute_score, load_scoring_config
from agency_audit.audit.tech_stack import detect_tech_stack

logger = logging.getLogger(__name__)

AUDIT_TIMEOUT = 30


def _check_ssl_valid(url: str) -> bool:
    """Check if the SSL certificate for a URL is valid."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        return False
    host = parts.hostname
    port = parts.port or 443
    if not host:
        return False
    try:
        ctx = ssl.create_default_context()
        with (
            socket.create_connection((host, port), timeout=10) as sock,
            ctx.wrap_socket(sock, server_hostname=host) as ssock,
        ):
            ssock.getpeercert()
        return True
    except Exception:
        return False


def _detect_language(html_text: str, headers: httpx.Headers) -> str | None:
    """Detect primary language of the page."""
    # Check Content-Language header
    content_lang: str = headers.get("content-language", "")
    if content_lang:
        return content_lang.split(",")[0].strip().lower()

    # Check <html lang="...">
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html_text)
    html_el = tree.css_first("html")
    if html_el:
        lang = html_el.attributes.get("lang", "")
        if lang:
            return lang.strip().lower()[:2]

    return None


async def audit_website(
    url: str,
    client: httpx.AsyncClient | None = None,
    scoring_config: dict | None = None,
    use_playwright: bool = False,
) -> AuditData:
    """Run a full audit on a website.

    Args:
        url: Website URL to audit.
        client: Optional httpx.AsyncClient. If not provided, one is created.
        scoring_config: Optional scoring config dict. If not provided, loads from file.
        use_playwright: If True, use Playwright for JS-heavy sites.

    Returns:
        AuditData with all check results and computed score.
    """
    # Normalize URL
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    audit = AuditData(url=url)
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(
            timeout=AUDIT_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
    assert client is not None

    try:
        import time

        # 1. Fetch robots.txt
        logger.info("Auditing %s — robots.txt...", url)
        audit.robots = await fetch_robots_txt(url, client=client)

        # 2. Fetch homepage (with timing)
        logger.info("Auditing %s — fetching homepage...", url)
        start = time.monotonic()
        response = await client.get(url)
        audit.response_time_ms = round((time.monotonic() - start) * 1000, 1)

        # 3. SSL validity
        audit.ssl_valid = _check_ssl_valid(url)

        # 4. Detect language
        audit.language = _detect_language(response.text, response.headers)

        # 5. Anti-scraping detection
        logger.info("Auditing %s — anti-scraping detection...", url)
        audit.anti_scraping = await detect_anti_scraping(url, response=response, client=client)

        # 5b. If JS-only rendering detected and Playwright is available, re-fetch with it
        if audit.anti_scraping.js_only_rendering and use_playwright:
            try:
                from agency_audit.audit.playwright_fetch import fetch_with_playwright

                logger.info("Auditing %s — using Playwright for JS rendering...", url)
                pw_html, pw_status = await fetch_with_playwright(url)
                if pw_html:
                    response = httpx.Response(
                        status_code=pw_status,
                        content=pw_html.encode(),
                        request=response.request,
                        headers=response.headers,
                    )
                    # Re-check anti-scraping with rendered content
                    audit.anti_scraping.js_only_rendering = False
            except Exception as exc:
                logger.warning("Playwright fetch failed for %s: %s", url, exc)

        # 6. API detection
        logger.info("Auditing %s — API detection...", url)
        audit.api_detection = await detect_api(url, response=response, client=client)

        # 7. Tech stack detection
        logger.info("Auditing %s — tech stack detection...", url)
        audit.tech_stack = await detect_tech_stack(url, response=response, client=client)

        # 8. Property count
        logger.info("Auditing %s — property count...", url)
        sitemap_urls = audit.robots.sitemap_urls
        audit.property_count = await count_properties(
            url,
            homepage_response=response,
            sitemap_urls=sitemap_urls,
            client=client,
        )

        # 9. Listing quality
        logger.info("Auditing %s — listing quality...", url)
        listing_url = _find_listing_page_url(url, response.text)
        audit.listing_quality = await assess_listing_quality(
            url,
            homepage_response=response,
            listing_url=listing_url,
            client=client,
        )

        # 10. Compute score
        logger.info("Auditing %s — computing score...", url)
        if scoring_config is None:
            scoring_config = load_scoring_config()
        audit.score, audit.score_breakdown = compute_score(audit, scoring_config)

        logger.info("Audit complete for %s — score: %d", url, audit.score)

    except httpx.HTTPError as exc:
        logger.error("HTTP error auditing %s: %s", url, exc)
        audit.notes = f"HTTP error: {exc}"
    except Exception as exc:
        logger.error("Error auditing %s: %s", url, exc, exc_info=True)
        audit.notes = f"Error: {exc}"
    finally:
        if own_client and client:
            await client.aclose()

    return audit


async def audit_websites(
    urls: list[str],
    concurrency: int = 5,
    scoring_config: dict | None = None,
) -> list[AuditData]:
    """Audit multiple websites concurrently.

    Args:
        urls: List of website URLs to audit.
        concurrency: Maximum concurrent audits.
        scoring_config: Optional scoring config.

    Returns:
        List of AuditData results in the same order as input URLs.
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def _audit_with_semaphore(url: str) -> AuditData:
        async with semaphore:
            return await audit_website(url, scoring_config=scoring_config)

    tasks = [_audit_with_semaphore(url) for url in urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Convert exceptions to AuditData with error notes
    final = []
    for url, result in zip(urls, results, strict=False):
        if isinstance(result, Exception):
            audit = AuditData(url=url, notes=f"Exception: {result}")
            final.append(audit)
        else:
            final.append(result)

    return final
