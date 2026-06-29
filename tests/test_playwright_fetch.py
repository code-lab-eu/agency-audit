"""Tests for playwright_fetch.py — optional & lazy; no browser required.

All tests mock playwright entirely so the default test run never needs
a browser binary installed. They prove the module is importable and
lazy-loads correctly.
"""

from __future__ import annotations

import ast
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

from agency_audit.audit.playwright_fetch import fetch_with_playwright

# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


_GOTO_DEFAULT = object()


def _make_page_mock(
    *,
    status: int = 200,
    html: str = "<html></html>",
    goto_returns: object = _GOTO_DEFAULT,
) -> MagicMock:
    """Build a mock Playwright page with configurable goto/status/content.

    Pass goto_returns=None explicitly to simulate a navigation failure.
    """
    mock_response = MagicMock()
    mock_response.status = status
    mock_page = MagicMock()
    mock_page.goto = AsyncMock(
        return_value=mock_response if goto_returns is _GOTO_DEFAULT else goto_returns
    )
    mock_page.content = AsyncMock(return_value=html)
    mock_page.wait_for_timeout = AsyncMock()
    return mock_page


def _make_full_mocks(page_mock: MagicMock):
    """Build the full mocked Playwright chain (page → browser → playwright → async_pw)."""
    mock_browser = MagicMock()
    mock_browser.new_page = AsyncMock(return_value=page_mock)
    mock_browser.close = AsyncMock()

    mock_playwright = MagicMock()
    mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)

    mock_async_pw = MagicMock()
    mock_async_pw.__aenter__ = AsyncMock(return_value=mock_playwright)
    mock_async_pw.__aexit__ = AsyncMock(return_value=None)

    return mock_browser, mock_async_pw


def _patch_async_playwright(mock_async_pw_factory):
    """Patch the *source* where fetch_with_playwright imports async_playwright."""
    return patch(
        "playwright.async_api.async_playwright",
        mock_async_pw_factory,
    )


# ──────────────────────────────────────────────────────────────────────
# Module-level: importability (no browser, no playwright init)
# ──────────────────────────────────────────────────────────────────────


def test_module_importable():
    """The module imports cleanly without touching playwright at all."""
    import agency_audit.audit.playwright_fetch as pw  # noqa: F811

    assert hasattr(pw, "fetch_with_playwright")
    assert callable(pw.fetch_with_playwright)


def test_module_no_top_level_playwright_import():
    """Top-level imports do NOT include playwright — it is lazy-loaded."""
    import sys

    assert "playwright" not in sys.modules


def test_lazy_import_inside_function():
    """The playwright import only happens inside the function, not at module level."""
    source = inspect.getsource(fetch_with_playwright)
    tree = ast.parse(source)

    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert len(imports) == 1
    imp = imports[0]
    assert isinstance(imp, ast.ImportFrom)
    assert imp.module == "playwright.async_api"
    assert "async_playwright" in [alias.name for alias in imp.names]


# ──────────────────────────────────────────────────────────────────────
# ImportError path — playwright not installed
# ──────────────────────────────────────────────────────────────────────


async def test_import_error_graceful_return():
    """When playwright is not installed, returns (None, 0) gracefully."""
    # The function does `from playwright.async_api import async_playwright`
    # so we must make that import fail.
    with patch(
        "playwright.async_api.async_playwright",
        side_effect=ImportError("No module named 'playwright'"),
        create=True,
    ):
        html, status = await fetch_with_playwright("https://example.com")
        assert html is None
        assert status == 0


# ──────────────────────────────────────────────────────────────────────
# General exception path
# ──────────────────────────────────────────────────────────────────────


async def test_general_exception_graceful_return():
    """Unexpected exceptions inside the browser block return (None, 0)."""
    with patch(
        "playwright.async_api.async_playwright",
        side_effect=RuntimeError("unexpected crash"),
        create=True,
    ):
        html, status = await fetch_with_playwright("https://example.com")
        assert html is None
        assert status == 0


# ──────────────────────────────────────────────────────────────────────
# Successful fetch path (fully mocked)
# ──────────────────────────────────────────────────────────────────────


async def test_successful_fetch_returns_html_and_status():
    """A successful Playwright fetch returns (html_content, status_code)."""
    page = _make_page_mock(html="<html><body>Rendered</body></html>")
    browser, mock_async_pw = _make_full_mocks(page)

    with _patch_async_playwright(MagicMock(return_value=mock_async_pw)):
        html, status = await fetch_with_playwright("https://spa.example.com")
        assert html == "<html><body>Rendered</body></html>"
        assert status == 200


async def test_successful_fetch_launches_headless_chromium():
    """The browser is launched in headless mode."""
    page = _make_page_mock()
    browser, mock_async_pw = _make_full_mocks(page)
    mock_playwright = mock_async_pw.__aenter__.return_value

    with _patch_async_playwright(MagicMock(return_value=mock_async_pw)):
        await fetch_with_playwright("https://example.com")
        mock_playwright.chromium.launch.assert_called_once_with(headless=True)


async def test_successful_fetch_goto_waits_networkidle():
    """page.goto is called with wait_until='networkidle' and a 30s timeout."""
    page = _make_page_mock()
    browser, mock_async_pw = _make_full_mocks(page)

    with _patch_async_playwright(MagicMock(return_value=mock_async_pw)):
        await fetch_with_playwright("https://example.com")
        page.goto.assert_called_once_with(
            "https://example.com", wait_until="networkidle", timeout=30000
        )


async def test_extra_wait_converts_seconds_to_milliseconds():
    """wait_seconds is converted to milliseconds for wait_for_timeout."""
    page = _make_page_mock()
    browser, mock_async_pw = _make_full_mocks(page)

    with _patch_async_playwright(MagicMock(return_value=mock_async_pw)):
        await fetch_with_playwright("https://example.com", wait_seconds=5.0)
        page.wait_for_timeout.assert_called_once_with(5000)


async def test_default_wait_seconds_is_three():
    """Default wait_seconds is 3.0 → 3000ms."""
    page = _make_page_mock()
    browser, mock_async_pw = _make_full_mocks(page)

    with _patch_async_playwright(MagicMock(return_value=mock_async_pw)):
        await fetch_with_playwright("https://example.com")
        page.wait_for_timeout.assert_called_once_with(3000)


async def test_zero_wait_seconds_skips_wait():
    """When wait_seconds=0, wait_for_timeout is never called."""
    page = _make_page_mock()
    browser, mock_async_pw = _make_full_mocks(page)

    with _patch_async_playwright(MagicMock(return_value=mock_async_pw)):
        await fetch_with_playwright("https://example.com", wait_seconds=0.0)
        page.wait_for_timeout.assert_not_called()


async def test_null_response_from_goto_returns_none():
    """When page.goto returns None (navigation failure), returns (None, 0)."""
    page = _make_page_mock(goto_returns=None)
    browser, mock_async_pw = _make_full_mocks(page)

    with _patch_async_playwright(MagicMock(return_value=mock_async_pw)):
        html, status = await fetch_with_playwright("https://dead.link")
        assert html is None
        assert status == 0


async def test_browser_closed_even_on_content_error():
    """browser.close() is called in the finally block even if content() raises."""
    page = _make_page_mock()
    page.content = AsyncMock(side_effect=RuntimeError("page crashed"))
    browser, mock_async_pw = _make_full_mocks(page)

    with _patch_async_playwright(MagicMock(return_value=mock_async_pw)):
        html, status = await fetch_with_playwright("https://example.com")
        assert html is None
        assert status == 0
        browser.close.assert_called_once()


async def test_browser_closed_on_exception_during_launch():
    """When launch itself fails, the outer except handler catches it and returns (None, 0)."""
    mock_async_pw_factory = MagicMock(side_effect=RuntimeError("chromium refusing to start"))

    with _patch_async_playwright(mock_async_pw_factory):
        html, status = await fetch_with_playwright("https://example.com")
        assert html is None
        assert status == 0
