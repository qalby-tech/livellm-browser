"""
Pytest configuration and fixtures for smoke tests.
"""
import itertools
import json
import os
from types import SimpleNamespace

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
def fresh_manager(tmp_path):
    """The global BrowserManager with empty state, and a registry with no file.

    The manager is a module singleton, so every test gets its own browsers,
    sessions, in-flight counts and health marks, restored afterwards.
    """
    from core.browser import browser_manager
    from core.registry import browser_registry

    with patch.multiple(
        browser_manager,
        browsers={}, sessions={}, _in_flight={}, _unhealthy_until={}, _rr=0,
        playwright=None, _playwright_pid=None,
    ), patch.multiple(
        browser_registry, path=str(tmp_path / "absent.json"), _cache={}, _mtime=-1.0,
    ):
        yield browser_manager


@pytest.fixture
def client(mock_playwright, mock_browser_context, mock_browser, fresh_manager, monkeypatch):
    """
    Create the FastAPI TestClient with mocked browser dependencies.

    Standalone mode (no BROWSERS_CONFIG) with one browser, "test-browser",
    already connected.
    """
    from core.browser import BrowserInfo

    monkeypatch.delenv("BROWSERS_CONFIG", raising=False)
    with patch('main.async_playwright') as mock_async_playwright:
        mock_async_playwright.return_value.start = AsyncMock(return_value=mock_playwright)

        from main import app

        fresh_manager.browsers["test-browser"] = BrowserInfo(
            mock_browser, mock_browser_context,
            ws_url="ws://localhost:9222/devtools/browser/test", browser_id="test-browser",
        )

        with TestClient(app) as test_client:
            yield test_client


# ==================== Two-browser pool ====================
#
# Fake Chrome instances behind a fake CDP connect: tabs live in the Chrome
# (so they survive a reconnect, and a person's tabs count), and a Chrome can
# be taken down, which breaks its connections and refuses new ones.

class FakePage:
    def __init__(self, context, tag):
        self._context = context
        self.tag = tag
        self.url = "about:blank"
        self.closed = False

    async def goto(self, url, **kwargs):
        self.url = url

    async def inner_text(self, selector):
        return self.tag

    async def content(self):
        return f"<html><body>{self.tag}</body></html>"

    async def evaluate(self, *args, **kwargs):
        return None

    async def close(self):
        if not self.closed:
            self.closed = True
            self._context.pages.remove(self)


class FakeContext:
    def __init__(self, name):
        self.name = name
        self.pages = []
        self._opened = 0

    async def new_page(self):
        self._opened += 1
        page = FakePage(self, f"{self.name}-p{self._opened}")
        self.pages.append(page)
        return page


class FakeChrome:
    def __init__(self, name):
        self.name = name
        self.up = True
        self.context = FakeContext(name)


class FakeCdpBrowser:
    """One CDP connection to a FakeChrome."""

    def __init__(self, chrome):
        self._chrome = chrome
        self._open = True
        self.contexts = [chrome.context]

    def is_connected(self):
        return self._open and self._chrome.up

    async def close(self):
        self._open = False


class FakeNet:
    """The browsers' side of the network: ws://<name>:9222/... reaches Chrome <name>."""

    def __init__(self, *names):
        self.chromes = {n: FakeChrome(n) for n in names}
        self.connects = []

    def chrome(self, name):
        if name not in self.chromes:
            self.chromes[name] = FakeChrome(name)
        return self.chromes[name]

    def down(self, name):
        self.chrome(name).up = False

    async def connect_over_cdp(self, ws_url, headers=None):
        name = ws_url.split("://", 1)[1].split(":", 1)[0]
        self.connects.append(name)
        chrome = self.chrome(name)
        if not chrome.up:
            raise ConnectionError(f"connect ECONNREFUSED {ws_url}")
        return FakeCdpBrowser(chrome)

    def playwright(self):
        # No private driver attributes: driver_alive() reads as alive.
        return SimpleNamespace(
            chromium=SimpleNamespace(connect_over_cdp=self.connect_over_cdp),
            stop=AsyncMock(),
        )


@pytest.fixture
def pool(request, tmp_path, monkeypatch, fresh_manager):
    """A managed controller (BROWSERS_CONFIG set) over agent-1 and agent-2.

    Parametrize indirectly with a list of ids to start from another registry.
    ``pool.set_registry(*ids)`` rewrites it the way the operator does.
    """
    from core.registry import browser_registry

    ids = getattr(request, "param", ["agent-1", "agent-2"])
    reg = tmp_path / "browsers.json"
    net = FakeNet("agent-1", "agent-2")
    stamp = itertools.count(1)

    def set_registry(*names):
        reg.write_text(json.dumps({"browsers": {
            n: f"ws://{n}:9222/devtools/browser/{n}" for n in names
        }}))
        t = 1_000_000_000 + next(stamp)
        os.utime(reg, (t, t))

    set_registry(*ids)
    monkeypatch.setenv("BROWSERS_CONFIG", str(reg))
    with patch.object(browser_registry, "path", str(reg)), \
            patch("main.async_playwright") as mock_async_playwright:
        mock_async_playwright.return_value.start = AsyncMock(return_value=net.playwright())
        from main import app

        with TestClient(app) as test_client:
            yield SimpleNamespace(
                client=test_client, net=net, manager=fresh_manager, set_registry=set_registry,
            )
