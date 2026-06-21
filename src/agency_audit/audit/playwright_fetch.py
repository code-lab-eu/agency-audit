"""Playwright on-demand fetcher for JS-heavy sites.

Only used when the main auditor detects JS-only rendering and
elects to re-fetch the page with a real browser.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def fetch_with_playwright(url: str, wait_seconds: float = 3.0) -> tuple[str | None, int]:
    """Fetch a URL using Playwright (headless Chromium).

    Args:
        url: URL to fetch.
        wait_seconds: Extra seconds to wait for JS rendering after load.

    Returns:
        (html_content, status_code). On error returns (None, 0).
    """
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                response = await page.goto(url, wait_until="networkidle", timeout=30000)
                if response is None:
                    return None, 0
                status = response.status

                # Extra wait for dynamic content
                if wait_seconds > 0:
                    await page.wait_for_timeout(int(wait_seconds * 1000))

                html = await page.content()
                return html, status
            finally:
                await browser.close()

    except ImportError:
        logger.warning("Playwright not installed — skipping JS rendering")
        return None, 0
    except Exception as exc:
        logger.warning("Playwright fetch failed for %s: %s", url, exc)
        return None, 0
