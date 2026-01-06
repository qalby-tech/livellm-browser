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
    
    # Mock locator for xpath selectors
    mock_locator = MagicMock()
    mock_locator.count = AsyncMock(return_value=0)
    page.locator = MagicMock(return_value=mock_locator)
    
    return page


@pytest.fixture
def mock_browser_context(mock_page):
    """Create a mock BrowserContext object."""
    context = AsyncMock()
    context.new_page = AsyncMock(return_value=mock_page)
    context.close = AsyncMock()
    return context


@pytest.fixture
def mock_playwright(mock_browser_context):
    """Create a mock Playwright object."""
    playwright = AsyncMock()
    playwright.chromium.launch_persistent_context = AsyncMock(return_value=mock_browser_context)
    playwright.stop = AsyncMock()
    return playwright


@pytest.fixture
def client(mock_playwright, mock_browser_context, mock_page):
    """
    Create the FastAPI TestClient with mocked browser dependencies.
    """
    with patch('main.async_playwright') as mock_async_playwright:
        # Setup the mock chain
        mock_async_playwright.return_value.start = AsyncMock(return_value=mock_playwright)
        
        # Import app after patching
        from main import app
        
        # Pre-configure app state to skip lifespan startup
        app.state.playwright = mock_playwright
        app.state.browser = mock_browser_context
        app.state.pages = {}
        
        with TestClient(app) as test_client:
            yield test_client
