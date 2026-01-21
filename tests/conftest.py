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
    page.query_selector_all = AsyncMock(return_value=[])
    page.close = AsyncMock()
    page.screenshot = AsyncMock(return_value=b"fake_png_bytes")
    
    # Mock locator for xpath selectors
    mock_locator = MagicMock()
    mock_locator.count = AsyncMock(return_value=0)
    page.locator = MagicMock(return_value=mock_locator)
    
    # Mock mouse for interact actions
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock()
    page.mouse.click = AsyncMock()
    page.mouse.wheel = AsyncMock()
    
    # Mock context for login action (set_extra_http_headers)
    page.context = MagicMock()
    page.context.set_extra_http_headers = AsyncMock()
    
    return page


@pytest.fixture
def mock_browser():
    """Create a mock Browser object."""
    browser = AsyncMock()
    browser.close = AsyncMock()
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
def mock_playwright(mock_browser_context):
    """Create a mock Playwright object."""
    playwright = AsyncMock()
    playwright.chromium.launch_persistent_context = AsyncMock(return_value=mock_browser_context)
    playwright.stop = AsyncMock()
    return playwright


@pytest.fixture
def mock_browser_manager(mock_playwright, mock_browser_context, mock_browser, mock_page):
    """Create a mock BrowserManager object."""
    from pathlib import Path
    
    # Create a mock BrowserInfo
    browser_info = MagicMock()
    browser_info.browser = mock_browser
    browser_info.context = mock_browser_context
    browser_info.profile_path = Path("./profiles/default")
    browser_info.pages = {}
    
    # Create mock browser manager
    manager = MagicMock()
    manager.playwright = mock_playwright
    manager.browsers = {"./profiles/default": browser_info}
    
    # Mock methods
    async def mock_create_browser(profile_dir=None, proxy=None):
        new_browser_info = MagicMock()
        new_browser_info.browser = mock_browser
        new_browser_info.context = mock_browser_context
        new_browser_info.profile_path = Path(profile_dir) if profile_dir else Path(f"./profiles/test-uuid")
        new_browser_info.pages = {}
        
        browser_id = profile_dir if profile_dir else "test-uuid"
        manager.browsers[browser_id] = new_browser_info
        return browser_id, new_browser_info
    
    manager.create_browser = mock_create_browser
    manager.get_browser = MagicMock(return_value=browser_info)
    manager.get_default_browser = MagicMock(return_value=browser_info)
    manager.get_default_browser_id = MagicMock(return_value="./profiles/default")
    manager.close_browser = AsyncMock(return_value=True)
    manager.shutdown = AsyncMock()
    
    return manager


@pytest.fixture
def client(mock_playwright, mock_browser_context, mock_page, mock_browser_manager):
    """
    Create the FastAPI TestClient with mocked browser dependencies.
    """
    with patch('main.async_playwright') as mock_async_playwright:
        # Setup the mock chain
        mock_async_playwright.return_value.start = AsyncMock(return_value=mock_playwright)
        
        # Import app after patching
        from main import app, browser_manager
        
        # Pre-configure app state to skip lifespan startup
        app.state.playwright = mock_playwright
        app.state.browser_manager = mock_browser_manager
        
        # Also patch the global browser_manager
        with patch.object(browser_manager, 'playwright', mock_playwright), \
             patch.object(browser_manager, 'browsers', mock_browser_manager.browsers), \
             patch.object(browser_manager, 'create_browser', mock_browser_manager.create_browser), \
             patch.object(browser_manager, 'get_browser', mock_browser_manager.get_browser), \
             patch.object(browser_manager, 'get_default_browser', mock_browser_manager.get_default_browser), \
             patch.object(browser_manager, 'get_default_browser_id', mock_browser_manager.get_default_browser_id):
            
            with TestClient(app) as test_client:
                yield test_client
