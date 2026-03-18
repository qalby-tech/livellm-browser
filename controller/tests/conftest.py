"""
Pytest configuration and fixtures for smoke tests.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def mock_page():
    """Create a mock Page object for testing."""
    page = AsyncMock()
    page.url = "https://example.com"
    page.goto = AsyncMock()
    page.content = AsyncMock(return_value="<html><body>Test</body></html>")
    page.inner_text = AsyncMock(return_value="Test content")
    page.query_selector = AsyncMock(return_value=None)
    page.query_selector_all = AsyncMock(return_value=[])
    page.close = AsyncMock()
    page.screenshot = AsyncMock(return_value=b"fake_png_bytes")

    mock_locator = MagicMock()
    mock_locator.count = AsyncMock(return_value=0)
    page.locator = MagicMock(return_value=mock_locator)

    page.mouse = MagicMock()
    page.mouse.move = AsyncMock()
    page.mouse.click = AsyncMock()
    page.mouse.wheel = AsyncMock()

    page.context = MagicMock()
    page.context.set_extra_http_headers = AsyncMock()

    return page


@pytest.fixture
def mock_browser():
    """Create a mock Browser object."""
    browser = AsyncMock()
    browser.close = AsyncMock()
    browser.is_connected = MagicMock(return_value=True)
    return browser


@pytest.fixture
def mock_browser_context(mock_page, mock_browser):
    """Create a mock BrowserContext object."""
    context = AsyncMock()
    context.new_page = AsyncMock(return_value=mock_page)
    context.close = AsyncMock()
    context.browser = mock_browser
    context.set_extra_http_headers = AsyncMock()
    return context


@pytest.fixture
def mock_playwright(mock_browser_context, mock_browser):
    """Create a mock Playwright object."""
    playwright = AsyncMock()
    playwright.chromium.connect_over_cdp = AsyncMock(return_value=mock_browser)
    mock_browser.contexts = [mock_browser_context]
    playwright.stop = AsyncMock()
    return playwright


@pytest.fixture
def mock_browser_manager(mock_playwright, mock_browser_context, mock_browser, mock_page):
    """Create a mock BrowserManager matching develop branch's CDP-based architecture."""
    from core.browser import BrowserInfo

    browser_info = BrowserInfo(mock_browser, mock_browser_context, ws_url="ws://localhost:9222/devtools/browser/test")
    browser_info.pages = {}

    manager = MagicMock()
    manager.playwright = mock_playwright
    manager.browsers = {"test-browser": browser_info}

    async def mock_connect_browser(browser_id, ws_url):
        new_info = BrowserInfo(mock_browser, mock_browser_context, ws_url=ws_url)
        manager.browsers[browser_id] = new_info
        return new_info

    manager.connect_browser = mock_connect_browser
    manager.disconnect_browser = AsyncMock(return_value=True)
    manager.get_browser = MagicMock(return_value=browser_info)
    manager.first_browser_id = MagicMock(return_value="test-browser")
    manager.shutdown = AsyncMock()

    return manager


@pytest.fixture
def client(mock_playwright, mock_browser_context, mock_page, mock_browser_manager):
    """
    Create the FastAPI TestClient with mocked browser dependencies.
    """
    with patch('main.async_playwright') as mock_async_playwright:
        mock_async_playwright.return_value.start = AsyncMock(return_value=mock_playwright)

        from main import app
        from core.browser import browser_manager

        app.state.playwright = mock_playwright
        app.state.browser_manager = mock_browser_manager

        with patch.object(browser_manager, 'playwright', mock_playwright), \
             patch.object(browser_manager, 'browsers', mock_browser_manager.browsers), \
             patch.object(browser_manager, 'connect_browser', mock_browser_manager.connect_browser), \
             patch.object(browser_manager, 'disconnect_browser', mock_browser_manager.disconnect_browser), \
             patch.object(browser_manager, 'get_browser', mock_browser_manager.get_browser), \
             patch.object(browser_manager, 'first_browser_id', mock_browser_manager.first_browser_id):

            with TestClient(app) as test_client:
                yield test_client
