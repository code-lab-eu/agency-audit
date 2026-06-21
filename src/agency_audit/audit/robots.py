"""Robots.txt fetch & parse module.

Fetches /robots.txt, parses rules using urllib.robotparser, and records:
  - whether scraping is allowed for our user-agent
  - crawl-delay if specified
  - sitemap URLs declared in the file
"""

from __future__ import annotations

import asyncio
import logging
from io import StringIO
from urllib.robotparser import RobotFileParser
from urllib.parse import urlsplit

import httpx

from agency_audit.audit.models import RobotsResult

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "AgencyAuditBot/1.0"
ROBOTS_TIMEOUT = 10


def _robots_url(base_url: str) -> str:
    """Build the /robots.txt URL from a base URL."""
    parts = urlsplit(base_url)
    return f"{parts.scheme}://{parts.netloc}/robots.txt"


def parse_robots_txt(content: str, base_url: str, user_agent: str = "*") -> RobotsResult:
    """Parse robots.txt content and extract rules.

    Args:
        content: Raw robots.txt text.
        base_url: Base URL of the site (for resolving sitemap refs).
        user_agent: User-agent to check rules for (default "*" = any bot).

    Returns:
        RobotsResult with allows_scraping, crawl_delay, sitemap_urls.
    """
    result = RobotsResult(fetched=True, raw_content=content)

    rp = RobotFileParser()
    rp.parse(content.splitlines())

    try:
        result.allows_scraping = rp.can_fetch(user_agent, base_url)
    except Exception:
        # If parser fails, default to allow
        result.allows_scraping = True

    # Extract crawl-delay (not exposed by RobotFileParser, parse manually)
    crawl_delay = _extract_crawl_delay(content, user_agent)
    if crawl_delay is not None:
        result.crawl_delay = crawl_delay

    # Extract sitemap URLs
    result.sitemap_urls = _extract_sitemaps(content)

    return result


def _extract_crawl_delay(content: str, user_agent: str) -> float | None:
    """Extract crawl-delay from robots.txt for the given user-agent."""
    lines = content.splitlines()
    in_our_section = False
    in_star_section = False
    star_delay = None
    our_delay = None

    for line in lines:
        stripped = line.strip().lower()
        if stripped.startswith("user-agent:"):
            ua = stripped[len("user-agent:"):].strip()
            in_our_section = ua.lower() == user_agent.lower()
            in_star_section = ua == "*"
        elif in_our_section and stripped.startswith("crawl-delay:"):
            try:
                our_delay = float(stripped[len("crawl-delay:"):].strip())
            except ValueError:
                pass
        elif in_star_section and stripped.startswith("crawl-delay:"):
            try:
                star_delay = float(stripped[len("crawl-delay:"):].strip())
            except ValueError:
                pass

    return our_delay if our_delay is not None else star_delay


def _extract_sitemaps(content: str) -> list[str]:
    """Extract sitemap URLs from robots.txt."""
    sitemaps = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("sitemap:"):
            url = stripped[len("sitemap:"):].strip()
            if url:
                sitemaps.append(url)
    return sitemaps


async def fetch_robots_txt(
    base_url: str,
    client: httpx.AsyncClient | None = None,
) -> RobotsResult:
    """Fetch and parse robots.txt for a website.

    Args:
        base_url: Website URL to check (e.g. "https://example.com").
        client: Optional httpx.AsyncClient. If not provided, one is created.

    Returns:
        RobotsResult with parsed rules.
    """
    robots_url = _robots_url(base_url)
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(
            timeout=ROBOTS_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )

    try:
        resp = await client.get(robots_url)
        if resp.status_code == 404:
            # No robots.txt — everything allowed by default
            return RobotsResult(fetched=False, allows_scraping=True)
        if resp.status_code >= 400:
            return RobotsResult(
                fetched=False,
                allows_scraping=True,
                error=f"HTTP {resp.status_code}",
            )

        content = resp.text
        return parse_robots_txt(content, base_url)

    except httpx.HTTPError as exc:
        logger.warning("Failed to fetch robots.txt for %s: %s", base_url, exc)
        return RobotsResult(fetched=False, allows_scraping=True, error=str(exc))
    except Exception as exc:
        logger.warning("Error parsing robots.txt for %s: %s", base_url, exc)
        return RobotsResult(fetched=False, allows_scraping=True, error=str(exc))
    finally:
        if own_client and client:
            await client.aclose()


async def check_robots_allows(
    base_url: str,
    user_agent: str = DEFAULT_USER_AGENT,
) -> bool:
    """Quick check whether robots.txt allows scraping for a URL."""
    result = await fetch_robots_txt(base_url)
    return result.allows_scraping


# Convenience for synchronous code paths
def parse_robots_sync(content: str, base_url: str, user_agent: str = "*") -> RobotsResult:
    """Synchronous wrapper for parse_robots_txt."""
    return parse_robots_txt(content, base_url, user_agent)
