import asyncio
import os
from pathlib import Path
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.responses import Response
from models.responses import (
    PingResponse, 
    SearchResult,
    SelectorResult,
    ActionResult
)
from models.requests import (
    SearchRequest,
    GetHtmlRequest,
    SelectorRequest,
    InteractRequest,
    HtmlAction,
    TextAction,
    ClickAction,
    FillAction,
    AttributeAction,
    RemoveAction,
    ScreenshotAction,
    ScrollAction,
    ScrollToBottomAction,
    MoveAction,
    MouseClickAction,
    IdleAction,
    LoginAction
)
from contextlib import asynccontextmanager
from patchright.async_api import async_playwright, Playwright
from patchright.async_api import Browser, BrowserContext
from patchright.async_api import Page
import uuid
import logging
from typing import List, Annotated, Optional, AsyncGenerator
from pydantic import BaseModel, Field

import shutil

# Default profile configuration
PROFILES_DIR = Path("./profiles")
DEFAULT_BROWSER_ID = "default"


def cleanup_profile_locks(profile_path: Path):
    """Remove Chrome lock files from a profile directory to prevent startup errors."""
    if not profile_path.exists():
        return
        
    locks = [
        "SingletonLock",
        "SingletonSocket",
        "SingletonCookie"
    ]
    
    for lock_name in locks:
        lock_file = profile_path / lock_name
        # Check if file exists or is a broken symlink
        if os.path.lexists(lock_file):
            try:
                if os.path.islink(lock_file):
                    os.unlink(lock_file)
                elif lock_file.is_dir():
                    shutil.rmtree(lock_file)
                else:
                    lock_file.unlink()
                logger.info(f"Removed lock file: {lock_file}")
            except Exception as e:
                logger.warning(f"Failed to remove lock file {lock_file}: {e}")



class PingFilter(logging.Filter):
    """Filter out /ping health check requests from access logs."""
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return "/ping" not in message

uvicorn_access_logger = logging.getLogger("uvicorn.access")
uvicorn_access_logger.addFilter(PingFilter())

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler = logging.StreamHandler()
handler.setFormatter(formatter)
logger.addHandler(handler)


class BrowserInfo:
    """Container for browser, context and its associated pages."""
    def __init__(self, browser: Browser, context: BrowserContext, profile_path: Optional[Path] = None):
        self.browser = browser
        self.context = context
        self.profile_path = profile_path
        self.pages: dict[str, Page] = {}



class BrowserManager:
    """Manages multiple browser instances."""
    def __init__(self):
        self.playwright: Optional[Playwright] = None
        self.browsers: dict[str, BrowserInfo] = {}
    
    async def start(self, playwright: Playwright):
        """Initialize the browser manager with playwright instance."""
        self.playwright = playwright
        # Create default browser
        await self.create_browser(profile_uid=DEFAULT_BROWSER_ID)
        logger.info("Browser manager started with default browser")
    
    async def create_browser(
        self, 
        profile_uid: Optional[str] = None,
        proxy: Optional["ProxySettings"] = None
    ) -> tuple[str, BrowserInfo]:
        """
        Create a new browser instance.
        
        Args:
            profile_uid: Profile name/UID. If provided, checks profiles/{uid} and creates persistent context.
                        If not provided, creates a fresh ephemeral browser instance (incognito).
            proxy: Optional proxy settings for the browser.
        
        Returns:
            Tuple of (browser_id, BrowserInfo)
        """
        if not self.playwright:
            raise RuntimeError("Browser manager not started")
        
        # Determine browser_id and profile_path
        if profile_uid:
            browser_id = profile_uid
            profile_path = PROFILES_DIR / profile_uid
            # Ensure no stale locks for this profile if it exists
            cleanup_profile_locks(profile_path)
            is_persistent = True
        else:
            browser_id = str(uuid.uuid4())
            profile_path = None
            is_persistent = False
        
        if browser_id in self.browsers:
            raise ValueError(f"Browser with id '{browser_id}' already exists")

        # Proxy configuration
        proxy_config = None
        if proxy:
            proxy_config = {"server": proxy.server}
            if proxy.username:
                proxy_config["username"] = proxy.username
            if proxy.password:
                proxy_config["password"] = proxy.password
            if proxy.bypass:
                proxy_config["bypass"] = proxy.bypass
            logger.info(f"Browser '{browser_id}' configured with proxy: {proxy.server}")
        
        browser = None
        context = None

        if is_persistent:
            # Build launch arguments for persistent context
            launch_kwargs = {
                "user_data_dir": str(profile_path),
                "headless": False,
                "channel": "chrome",
                "no_viewport": True,
                "args": ["--start-maximized"],
            }
            if proxy_config:
                launch_kwargs["proxy"] = proxy_config
            
            # Launch browser with persistent context (Playwright creates dir if needed)
            context = await self.playwright.chromium.launch_persistent_context(**launch_kwargs)
            browser = context.browser
        
        else:
            # Build launch arguments for ephemeral browser
            launch_kwargs = {
                "headless": False,
                "channel": "chrome",
                "args": ["--start-maximized"],
            }
            if proxy_config:
                launch_kwargs["proxy"] = proxy_config
            
            browser = await self.playwright.chromium.launch(**launch_kwargs)
            
            # Create new context
            context = await browser.new_context(no_viewport=True)

        if browser is None and context:
            browser = context.browser
            
        if browser is None:
            raise RuntimeError(f"Failed to get browser object for {browser_id}")
        
        browser_info = BrowserInfo(browser, context, profile_path)
        self.browsers[browser_id] = browser_info
        
        if is_persistent:
            logger.info(f"Created persistent browser '{browser_id}' with profile at {profile_path}")
        else:
            logger.info(f"Created ephemeral browser '{browser_id}'")
        
        return browser_id, browser_info
    
    def get_browser(self, browser_id: str) -> BrowserInfo:
        """Get a browser by its ID."""
        if browser_id not in self.browsers:
            raise KeyError(f"Browser with id '{browser_id}' not found")
        return self.browsers[browser_id]
    
    def get_default_browser(self) -> BrowserInfo:
        """Get the default browser."""
        return self.browsers[DEFAULT_BROWSER_ID]
    
    def get_default_browser_id(self) -> str:
        """Get the default browser ID."""
        return DEFAULT_BROWSER_ID
    
    async def close_browser(self, browser_id: str) -> bool:
        """Close and remove a browser instance."""
        if browser_id == DEFAULT_BROWSER_ID:
            raise ValueError("Cannot close the default browser")
        
        if browser_id not in self.browsers:
            return False
        
        browser_info = self.browsers[browser_id]
        
        # Close all pages
        for page in browser_info.pages.values():
            try:
                await page.close()
            except Exception as e:
                logger.warning(f"Error closing page: {e}")
        
        # Close browser context
        try:
            await browser_info.context.close()
        except Exception as e:
            logger.warning(f"Error closing context: {e}")
        
        # Close browser (this properly releases all resources and lock files)
        try:
            await browser_info.browser.close()
        except Exception as e:
            logger.warning(f"Error closing browser: {e}")
        
        del self.browsers[browser_id]
        logger.info(f"Closed browser '{browser_id}'")
        return True
    
    async def shutdown(self, timeout: float = 25.0):
        """Close all browsers and cleanup with timeout protection."""
        logger.info("Starting browser shutdown...")
        
        async def _shutdown_task():
            for browser_id in list(self.browsers.keys()):
                browser_info = self.browsers[browser_id]
                
                # Close all pages first
                for page in browser_info.pages.values():
                    try:
                        await asyncio.wait_for(page.close(), timeout=2.0)
                    except asyncio.TimeoutError:
                        logger.warning(f"Timeout closing page, forcing close")
                    except Exception as e:
                        logger.warning(f"Error closing page: {e}")
                
                # Close browser context
                try:
                    await asyncio.wait_for(browser_info.context.close(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning(f"Timeout closing context for browser {browser_id}")
                except Exception as e:
                    logger.warning(f"Error closing context for browser {browser_id}: {e}")
                
                # Close browser (this properly releases all resources and lock files)
                try:
                    await asyncio.wait_for(browser_info.browser.close(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning(f"Timeout closing browser {browser_id}")
                except Exception as e:
                    logger.warning(f"Error closing browser {browser_id}: {e}")
            
            self.browsers.clear()
            logger.info("All browsers closed")
        
        try:
            await asyncio.wait_for(_shutdown_task(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(f"Shutdown timed out after {timeout}s, forcing cleanup")
            self.browsers.clear()


# Global browser manager
browser_manager = BrowserManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Clean up Chrome lock files before starting Playwright
    # This ensures a clean state on container restart
    default_profile = PROFILES_DIR / DEFAULT_BROWSER_ID
    cleanup_profile_locks(default_profile)
    default_profile.mkdir(parents=True, exist_ok=True)
    
    # Start playwright and browser manager
    playwright = await async_playwright().start()
    try:
        await browser_manager.start(playwright)
    except Exception as e:
        logger.error(f"Failed to start browser manager: {e}")
        # Try to cleanup and re-raise or handle gracefully?
        # For now, we log and continue, but app might be unhealthy
    
    app.state.playwright = playwright
    app.state.browser_manager = browser_manager
    
    yield
    
    # Cleanup: properly close all browsers before stopping Playwright
    logger.info("Application shutting down, cleaning up resources...")
    try:
        await browser_manager.shutdown(timeout=25.0)
    except Exception as e:
        logger.error(f"Error during browser shutdown: {e}")
    
    try:
        await asyncio.wait_for(playwright.stop(), timeout=5.0)
    except asyncio.TimeoutError:
        logger.warning("Timeout stopping playwright, continuing shutdown")
    except Exception as e:
        logger.warning(f"Error stopping playwright: {e}")
    
    logger.info("Shutdown complete")


app = FastAPI(title="Controller API", version="0.2.0", lifespan=lifespan, root_path="/parser")


# Request/Response models for browser management
class ProxySettings(BaseModel):
    """Proxy configuration for browser."""
    server: str = Field(..., description="Proxy server URL (e.g., 'http://myproxy.com:3128')")
    username: Optional[str] = Field(default=None, description="Proxy authentication username")
    password: Optional[str] = Field(default=None, description="Proxy authentication password")
    bypass: Optional[str] = Field(default=None, description="Comma-separated hosts to bypass proxy")


class CreateBrowserRequest(BaseModel):
    profile_uid: Optional[str] = Field(
        default=None, 
        description="Profile name/UID. If provided, uses persistent profile in profiles/{uid}. If not, creates ephemeral session."
    )
    proxy: Optional[ProxySettings] = Field(
        default=None,
        description="Proxy settings for the browser. Only applies when creating a new browser."
    )


class BrowserResponse(BaseModel):
    browser_id: str
    profile_path: Optional[str]
    session_count: int


class StartSessionRequest(BaseModel):
    browser_id: Optional[str] = Field(default=None, description="Browser to create session in. Defaults to default browser.")


# Dependencies
BrowserIdDep = Annotated[Optional[str], Header(alias="X-Browser-Id")]
SessionIdDep = Annotated[Optional[str], Header(alias="X-Session-Id")]


async def get_browser_info(
    request: Request,
    browser_id: BrowserIdDep = None
) -> BrowserInfo:
    """
    Get browser info, defaulting to the default browser.
    
    If a specific browser_id is provided and doesn't exist, it will be created automatically.
    If no browser_id is provided, uses the default browser and ensures it exists.
    """
    manager: BrowserManager = request.app.state.browser_manager
    bid = browser_id or manager.get_default_browser_id()
    
    try:
        return manager.get_browser(bid)
    except KeyError:
        # Browser doesn't exist - create it automatically
        logger.info(f"Browser '{bid}' not found, creating it automatically")
        try:
            # Create the browser with the profile_uid (for persistent profile)
            _, browser_info = await manager.create_browser(profile_uid=bid)
            return browser_info
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to create browser '{bid}': {str(e)}")


BrowserInfoDep = Annotated[BrowserInfo, Depends(get_browser_info)]


async def get_or_create_page(
    request: Request,
    browser_info: BrowserInfoDep,
    session_id: SessionIdDep = None
) -> AsyncGenerator[Page, None]:
    """
    Get or create a page object for the given session ID within a browser.
    If no session_id is provided, a new one is generated and stored in the request state,
    and the session is treated as 'ad-hoc' (closed after request).
    """
    is_ad_hoc = False
    # Generate session_id if not provided
    if session_id is None:
        session_id = str(uuid.uuid4())
        is_ad_hoc = True
        
    request.state.session_id = session_id
    
    pages = browser_info.pages
    page = None
    
    # Check if page already exists
    if session_id in pages:
        page = pages[session_id]
        # Verify page is still valid (not closed)
        try:
            _ = page.url
        except Exception:
            logger.info(f"Page for session {session_id} was closed, creating new one")
            page = None
            
    if page is None:
        # Create new page
        page = await browser_info.context.new_page()
        pages[session_id] = page
        logger.info(f"Created new page for session {session_id} (ad-hoc={is_ad_hoc})")
    
    try:
        yield page
    finally:
        if is_ad_hoc:
            try:
                # Remove from pages dict first to prevent race conditions or stale access
                pages.pop(session_id, None)
                await page.close()
                logger.info(f"Closed ad-hoc page for session {session_id}")
            except Exception as e:
                logger.warning(f"Error closing ad-hoc page for session {session_id}: {e}")


PageDep = Annotated[Page, Depends(get_or_create_page)]


# ==================== Browser Management Endpoints ====================

@app.get("/ping")
async def root() -> PingResponse:
    """Health check endpoint"""
    return PingResponse(status="ok", message="Controller API is running")


@app.get("/browsers")
async def list_browsers() -> List[BrowserResponse]:
    """List all active browsers."""
    return [
        BrowserResponse(
            browser_id=bid,
            profile_path=str(info.profile_path) if info.profile_path else None,
            session_count=len(info.pages)
        )
        for bid, info in browser_manager.browsers.items()
    ]


@app.post("/browsers")
async def create_browser(request: CreateBrowserRequest = CreateBrowserRequest()) -> BrowserResponse:
    """
    Create a new browser instance.
    
    If profile_uid is provided, it creates a persistent browser with that profile (profiles/{uid}).
    If not provided, creates an ephemeral browser with a random UUID.
    
    Proxy settings can be specified to route all browser traffic through a proxy server.
    """
    try:
        browser_id, browser_info = await browser_manager.create_browser(
            profile_uid=request.profile_uid,
            proxy=request.proxy
        )
        return BrowserResponse(
            browser_id=browser_id,
            profile_path=str(browser_info.profile_path) if browser_info.profile_path else None,
            session_count=0
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/browsers/{browser_id:path}")
async def delete_browser(browser_id: str) -> dict:
    """
    Close and remove a browser instance.
    Cannot delete the default browser.
    """
    try:
        success = await browser_manager.close_browser(browser_id)
        if success:
            return {"status": "success", "message": f"Browser '{browser_id}' closed"}
        else:
            raise HTTPException(status_code=404, detail=f"Browser '{browser_id}' not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ==================== Session Management Endpoints ====================

@app.post("/start_session")
async def start_session(
    request: StartSessionRequest = StartSessionRequest(),
    browser_id: BrowserIdDep = None
) -> dict:
    """
    Start a new session in a browser and return the session ID.
    
    The browser_id can be specified via:
    - X-Browser-Id header
    - request body browser_id field
    
    If neither is provided, uses the default browser.
    If the specified browser doesn't exist, it will be created automatically.
    """
    # Use request body browser_id if header not provided
    bid = browser_id or request.browser_id or browser_manager.get_default_browser_id()
    
    try:
        browser_info = browser_manager.get_browser(bid)
    except KeyError:
        # Browser doesn't exist - create it automatically
        logger.info(f"Browser '{bid}' not found, creating it automatically")
        try:
            _, browser_info = await browser_manager.create_browser(profile_uid=bid)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to create browser '{bid}': {str(e)}")
    
    session_id = str(uuid.uuid4())
    page = await browser_info.context.new_page()
    browser_info.pages[session_id] = page
    logger.info(f"Started new session: {session_id} in browser '{bid}'")
    
    return {
        "session_id": session_id,
        "browser_id": bid,
        "message": "Session created. Use X-Session-Id and X-Browser-Id headers in subsequent requests."
    }


@app.delete("/end_session")
async def end_session(
    browser_info: BrowserInfoDep,
    session_id: SessionIdDep = None
) -> dict:
    """
    End a session and close the page.
    Requires X-Session-Id header. X-Browser-Id header is optional (defaults to default browser).
    """
    if session_id is None:
        raise HTTPException(status_code=400, detail="X-Session-Id header is required")
    
    page = browser_info.pages.pop(session_id, None)
    if page:
        try:
            await page.close()
            logger.info(f"Closed page for session {session_id}")
            return {"status": "success", "message": f"Session {session_id} ended"}
        except Exception as e:
            logger.warning(f"Error closing page for session {session_id}: {e}")
            return {"status": "success", "message": f"Session {session_id} removed (page was already closed)"}
    else:
        return {"status": "success", "message": f"Session {session_id} not found (already ended or never existed)"}


# ==================== Helper Functions for Native Playwright ====================

def build_locator(page: Page, selector_type: str, selector_value: str):
    """Build a Playwright locator from selector type and value."""
    if selector_type == "css":
        return page.locator(selector_value)
    else:  # xml/xpath
        return page.locator(f"xpath={selector_value}")


async def get_elements_html(page: Page, selector_type: str, selector_value: str) -> List[str]:
    """Get outer HTML of all matching elements using native Playwright."""
    locator = build_locator(page, selector_type, selector_value)
    count = await locator.count()
    results = []
    for i in range(count):
        try:
            html = await locator.nth(i).evaluate("el => el.outerHTML")
            results.append(html)
        except Exception as e:
            results.append(f"error: {str(e)}")
    return results


async def get_elements_text(page: Page, selector_type: str, selector_value: str) -> List[str]:
    """Get text content of all matching elements using native Playwright."""
    locator = build_locator(page, selector_type, selector_value)
    return await locator.all_inner_texts()


async def click_elements(page: Page, selector_type: str, selector_value: str, nth: Optional[int] = 0) -> List[str]:
    """Click on elements. nth=0 first, nth=-1 last, nth=None all."""
    locator = build_locator(page, selector_type, selector_value)
    count = await locator.count()
    if count == 0:
        return []
    
    results = []
    if nth is None:
        # Click all
        for i in range(count):
            try:
                await locator.nth(i).click()
                results.append("clicked")
            except Exception as e:
                results.append(f"error: {str(e)}")
    elif nth == -1:
        # Click last
        try:
            await locator.last.click()
            results.append("clicked")
        except Exception as e:
            results.append(f"error: {str(e)}")
    else:
        # Click nth (default 0 = first)
        try:
            await locator.nth(nth).click()
            results.append("clicked")
        except Exception as e:
            results.append(f"error: {str(e)}")
    return results


async def fill_elements(page: Page, selector_type: str, selector_value: str, value: str, nth: Optional[int] = 0) -> List[str]:
    """Fill elements with value. nth=0 first, nth=-1 last, nth=None all."""
    locator = build_locator(page, selector_type, selector_value)
    count = await locator.count()
    if count == 0:
        return []
    
    results = []
    if nth is None:
        # Fill all
        for i in range(count):
            try:
                await locator.nth(i).fill(value)
                results.append("filled")
            except Exception as e:
                results.append(f"error: {str(e)}")
    elif nth == -1:
        # Fill last
        try:
            await locator.last.fill(value)
            results.append("filled")
        except Exception as e:
            results.append(f"error: {str(e)}")
    else:
        # Fill nth (default 0 = first)
        try:
            await locator.nth(nth).fill(value)
            results.append("filled")
        except Exception as e:
            results.append(f"error: {str(e)}")
    return results


async def get_elements_attribute(page: Page, selector_type: str, selector_value: str, attr_name: str) -> List[str]:
    """Get attribute value from all matching elements."""
    # Handle direct XPath attribute syntax like //a/@href
    if selector_type == "xml" and "/@" in selector_value:
        # Use evaluate for direct attribute XPath
        result = await page.evaluate("""
            (xpath) => {
                const result = document.evaluate(xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                const values = [];
                for (let i = 0; i < result.snapshotLength; i++) {
                    const node = result.snapshotItem(i);
                    values.push(node.nodeValue || node.textContent || '');
                }
                return values;
            }
        """, selector_value)
        return result
    
    locator = build_locator(page, selector_type, selector_value)
    count = await locator.count()
    results = []
    for i in range(count):
        try:
            val = await locator.nth(i).get_attribute(attr_name)
            results.append(val or "")
        except Exception as e:
            results.append(f"error: {str(e)}")
    return results


async def remove_elements(page: Page, selector_type: str, selector_value: str, nth: Optional[int] = 0) -> List[str]:
    """Remove elements from DOM. nth=0 first, nth=-1 last, nth=None all."""
    locator = build_locator(page, selector_type, selector_value)
    count = await locator.count()
    if count == 0:
        return []
    
    results = []
    if nth is None:
        # Remove all (in reverse order to avoid index shifting)
        for i in range(count - 1, -1, -1):
            try:
                await locator.nth(i).evaluate("el => el.remove()")
                results.append("removed")
            except Exception as e:
                results.append(f"error: {str(e)}")
        results.reverse()  # Return in original order
    elif nth == -1:
        # Remove last
        try:
            await locator.last.evaluate("el => el.remove()")
            results.append("removed")
        except Exception as e:
            results.append(f"error: {str(e)}")
    else:
        # Remove nth (default 0 = first)
        try:
            await locator.nth(nth).evaluate("el => el.remove()")
            results.append("removed")
        except Exception as e:
            results.append(f"error: {str(e)}")
    return results


# ==================== Core Endpoints ====================

async def _parse_search_results(page: Page, results: List[SearchResult], seen_links: set, count: int) -> List[SearchResult]:
    """
    Parse search results from the current Google search page.
    
    Args:
        page: The browser page with Google search results
        results: Existing results list to append to
        seen_links: Set of already seen links to avoid duplicates
        count: Maximum number of results to collect
        
    Returns:
        Updated list of SearchResult objects
    """
    result_divs = await page.query_selector_all('div[data-rpos]')
    
    for result_div in result_divs:
        if len(results) >= count:
            break
            
        try:
            span_elements = await result_div.query_selector_all('span')
            link_element = None
            for span in span_elements:
                a = await span.query_selector('a')
                if a:
                    link_element = a
                    break

            if not link_element:
                continue

            href = await link_element.get_attribute('href')
            
            # Skip if we've already seen this link
            if href in seen_links:
                continue
            seen_links.add(href)
            
            title = await link_element.inner_text()

            snippet_texts = []
            for span in span_elements:
                html = await span.inner_html()
                if '<em>' in html:
                    text = await span.inner_text()
                    snippet_texts.append(text)

            snippet = '\n'.join(snippet_texts)

            # Try to extract thumbnail image (base64-encoded data URL)
            image_data = None
            try:
                # Priority: Look for the thumbnail image with specific ID pattern or attribute
                # This avoids picking up the favicon (which is usually just a plain img or has different classes)
                img_element = await result_div.query_selector('img[id^="dimg_"]')
                
                if not img_element:
                    img_element = await result_div.query_selector('img[data-csiid]')

                if img_element:
                    src = await img_element.get_attribute('src')
                    if src and src.startswith('data:image/'):
                        image_data = src
            except Exception:
                pass  # Skip if image extraction fails

            results.append(SearchResult(
                link=href,
                title=title,
                snippet=snippet,
                image=image_data
            ))
                    
        except Exception:
            continue
    
    return results


@app.post("/search")
async def search(request: SearchRequest, page: PageDep) -> List[SearchResult]:
    """
    Search the web using Google and return search results
    
    Args:
        request: SearchRequest with query string and count of results
        page: Page object automatically managed by session (via X-Session-Id header)
        
    Returns:
        List[SearchResult]: Array of search results with link, title, and snippet
    """
    try:
        await page.goto(f"https://www.google.com/search?q={request.query}&num={request.count}", wait_until="commit")
        await asyncio.sleep(3)

        results = []
        seen_links = set()  # Track seen links to avoid duplicates across pages
        
        # Parse initial results
        results = await _parse_search_results(page, results, seen_links, request.count)
        
        # Pagination: if we don't have enough results, try next pages
        max_pages = 10  # Safety limit to prevent infinite loops
        current_page = 1
        
        while len(results) < request.count and current_page < max_pages:
            # Try to find and click the next page button
            # Google uses id="pnnext" for the "Next" link
            next_button = await page.query_selector('a#pnnext')
            
            if not next_button:
                # Try alternative selectors for next page
                logger.info("No next page button found. Trying alternative selectors: 'aria-label' method")
                next_button = await page.query_selector('a[aria-label="Next page"]')
            if not next_button:
                # Try finding by text content in pagination
                logger.info("No next page button found. Trying alternative selectors: 'next' text method")
                next_button = await page.query_selector('table.AaVjTc a:has-text("Next")')
            
            if not next_button:
                # No more pages available
                logger.info(f"No next page button found after page {current_page}. Got {len(results)} results.")
                break
            
            # Click next page
            await next_button.click()
            await asyncio.sleep(1)  # Wait for page to load
            
            current_page += 1
            previous_count = len(results)
            
            # Parse results from new page
            results = await _parse_search_results(page, results, seen_links, request.count)
            
            # If no new results were added, stop to avoid infinite loop
            if len(results) == previous_count:
                logger.info(f"No new results found on page {current_page}. Stopping pagination.")
                break
            
            logger.info(f"Page {current_page}: collected {len(results)}/{request.count} results")
        
        return results
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to search: {str(e)}")


@app.post("/content")
async def get_content(request: GetHtmlRequest, page: PageDep) -> str:
    """
    Get the HTML or text content of the given page.
    
    If URL is provided, navigates to it first. Otherwise, uses current page.
    """
    try:
        if request.url:
            await page.goto(request.url, wait_until=request.wait_until, timeout=request.timeout)
        
        if request.idle > 0:
            await asyncio.sleep(request.idle)
        
        if request.return_html:
            content = await page.content()
        else:
            content = await page.inner_text("body")
        
        return content
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get content: {str(e)}")


@app.post("/selectors")
async def execute_selectors(request: SelectorRequest, page: PageDep) -> List[SelectorResult]:
    """
    Execute CSS or XPath selectors on a page and perform actions on matched elements.
    
    Uses native Playwright locators for better performance.
    If URL is provided, navigates to it first. Otherwise, uses current page.
    """
    try:
        if request.url:
            await page.goto(request.url, wait_until=request.wait_until, timeout=request.timeout)
        
        if request.idle > 0:
            await asyncio.sleep(request.idle)
        
        results = []
        
        for selector in request.selectors:
            action_results = []
            
            for action in selector.actions:
                try:
                    values = []
                    
                    if isinstance(action, HtmlAction):
                        values = await get_elements_html(page, selector.type, selector.value)
                        logger.info(f"Got {len(values)} HTML elements for selector {selector.name}")
                        
                    elif isinstance(action, TextAction):
                        values = await get_elements_text(page, selector.type, selector.value)
                        logger.info(f"Got {len(values)} text values for selector {selector.name}")
                        
                    elif isinstance(action, ClickAction):
                        values = await click_elements(page, selector.type, selector.value, action.nth)
                        nth_desc = "first" if action.nth == 0 else ("last" if action.nth == -1 else "all")
                        logger.info(f"Clicked {len(values)} elements ({nth_desc}) for selector {selector.name}")
                        
                    elif isinstance(action, FillAction):
                        values = await fill_elements(page, selector.type, selector.value, action.value, action.nth)
                        nth_desc = "first" if action.nth == 0 else ("last" if action.nth == -1 else "all")
                        logger.info(f"Filled {len(values)} elements ({nth_desc}) for selector {selector.name}")
                            
                    elif isinstance(action, AttributeAction):
                        values = await get_elements_attribute(page, selector.type, selector.value, action.name)
                        logger.info(f"Got {len(values)} attribute values for selector {selector.name}")
                    
                    elif isinstance(action, RemoveAction):
                        values = await remove_elements(page, selector.type, selector.value, action.nth)
                        nth_desc = "first" if action.nth == 0 else ("last" if action.nth == -1 else "all")
                        logger.info(f"Removed {len(values)} elements ({nth_desc}) for selector {selector.name}")
                    
                    action_results.append(ActionResult(
                        action=action.action,
                        values=values if values else []
                    ))
                    
                except Exception as e:
                    logger.warning(f"Action {action.action} failed for selector {selector.name}: {e}")
                    action_results.append(ActionResult(
                        action=action.action,
                        values=[f"error: {str(e)}"]
                    ))
            
            results.append(SelectorResult(
                name=selector.name,
                results=action_results
            ))
        
        return results
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to execute selectors: {str(e)}")


@app.post("/interact")
async def interact(request: InteractRequest, page: PageDep) -> Response:
    """
    Unified endpoint for page interactions using an actions list.
    
    If URL is provided, navigates to it first. Otherwise, uses current page.
    Actions are executed in the order they appear in the list.
    """
    try:
        actions_performed = []
        screenshot_bytes = None
        content_result = None
        
        if request.url:
            await page.goto(request.url, wait_until=request.wait_until, timeout=request.timeout)
            actions_performed.append(f"navigated to {request.url}")
        
        if request.idle > 0:
            await asyncio.sleep(request.idle)
        
        for action in request.actions:
            if isinstance(action, MoveAction):
                await page.mouse.move(action.x, action.y, steps=action.steps)
                actions_performed.append(f"moved to ({action.x}, {action.y})")
                logger.info(f"Moved mouse to ({action.x}, {action.y}) with {action.steps} steps")
                
            elif isinstance(action, MouseClickAction):
                await page.mouse.click(
                    action.x, 
                    action.y, 
                    button=action.button,
                    click_count=action.click_count,
                    delay=action.delay
                )
                actions_performed.append(f"clicked at ({action.x}, {action.y}) with {action.button} button")
                logger.info(f"Clicked at ({action.x}, {action.y}) with {action.button} button")
                
            elif isinstance(action, ScrollAction):
                await page.mouse.wheel(action.x, action.y)
                actions_performed.append(f"scrolled by ({action.x}, {action.y})")
                logger.info(f"Scrolled by ({action.x}, {action.y})")
            
            elif isinstance(action, ScrollToBottomAction):
                # Smooth scroll to bottom logic
                start_time = asyncio.get_event_loop().time()
                
                while True:
                    # Check timeout - strict exit condition
                    if asyncio.get_event_loop().time() - start_time > action.timeout:
                        logger.info(f"Scroll to bottom finished (timeout {action.timeout}s reached)")
                        break
                    
                    # Scroll down by step
                    #await page.evaluate(f"window.scrollBy(0, {action.step_pixels})")
                    await page.mouse.wheel(0, action.step_pixels)
                    await asyncio.sleep(action.step_delay)
                    
                    # If we just want to keep scrolling until timeout, we don't strictly need to break at bottom.
                    # However, to be efficient, we can check if we really are stuck at bottom.
                    # But user requested "scroll until timeout", which implies forcing scroll attempts
                    # even if it looks like bottom (useful for aggressive infinite scrolls or tricky DOMs).
                    
                    # Optional: We can still check if we are at bottom to maybe speed up 'step_delay' 
                    # or just unconditionally scroll until timeout. 
                    # Based on user request "scroll not until bottom but until timeout is reached",
                    # we will prioritize the timeout loop.
                
                actions_performed.append("scrolled (duration based)")
                logger.info("Scrolled (duration based)")
                
            elif isinstance(action, IdleAction):
                await asyncio.sleep(action.duration)
                actions_performed.append(f"waited {action.duration}s")
                logger.info(f"Waited {action.duration} seconds")
            
            elif isinstance(action, LoginAction):
                import base64
                if action.username and action.password:
                    credentials = base64.b64encode(f"{action.username}:{action.password}".encode()).decode()
                    await page.context.set_extra_http_headers({"Authorization": f"Basic {credentials}"})
                    actions_performed.append(f"set http credentials for user '{action.username}'")
                    logger.info(f"Set HTTP Basic Auth credentials for user '{action.username}'")
                else:
                    # Clear credentials by setting empty headers
                    await page.context.set_extra_http_headers({})
                    actions_performed.append("cleared http credentials")
                    logger.info("Cleared HTTP Basic Auth credentials")
                
            elif isinstance(action, HtmlAction):
                content_result = await page.content()
                actions_performed.append("got html content")
                logger.info("Got HTML content")
                
            elif isinstance(action, TextAction):
                content_result = await page.inner_text("body")
                actions_performed.append("got text content")
                logger.info("Got text content")
                
            elif isinstance(action, ScreenshotAction):
                screenshot_bytes = await page.screenshot(full_page=action.full_page, type="png")
                actions_performed.append(f"screenshot taken (full_page={action.full_page})")
                logger.info(f"Screenshot taken (full_page={action.full_page})")
        
        if screenshot_bytes:
            return Response(content=screenshot_bytes, media_type="image/png")
        
        if content_result is not None:
            return Response(content=content_result, media_type="text/plain")
        
        import json
        return Response(
            content=json.dumps({"status": "success", "actions": actions_performed}),
            media_type="application/json"
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to interact: {str(e)}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )

