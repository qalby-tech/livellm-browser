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
from typing import List, Annotated, Optional
from pydantic import BaseModel, Field

# Default profile configuration
PROFILES_DIR = Path("./profiles")
DEFAULT_BROWSER_ID = "default"


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
    def __init__(self, browser: Browser, context: BrowserContext, profile_path: Path):
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
        await self.create_browser(profile_dir=str(PROFILES_DIR / DEFAULT_BROWSER_ID))
        logger.info("Browser manager started with default browser")
    
    async def create_browser(
        self, 
        profile_dir: Optional[str] = None,
        proxy: Optional["ProxySettings"] = None
    ) -> tuple[str, BrowserInfo]:
        """
        Create a new browser instance.
        
        Args:
            profile_dir: Profile directory path. If provided, used as browser_id.
                        If not provided, generates UUID and uses profiles/{uuid}.
            proxy: Optional proxy settings for the browser.
        
        Returns:
            Tuple of (browser_id, BrowserInfo)
        """
        if not self.playwright:
            raise RuntimeError("Browser manager not started")
        
        # Determine browser_id and profile_path
        if profile_dir:
            browser_id = profile_dir
            profile_path = Path(profile_dir)
        else:
            browser_id = str(uuid.uuid4())
            profile_path = PROFILES_DIR / browser_id
        
        if browser_id in self.browsers:
            raise ValueError(f"Browser with id '{browser_id}' already exists")

        # Build launch arguments
        launch_kwargs = {
            "user_data_dir": str(profile_path),
            "headless": False,
            "channel": "chrome",
            "no_viewport": True,
            "args": ["--start-maximized"],
        }
        
        # Add proxy if provided
        if proxy:
            proxy_config = {"server": proxy.server}
            if proxy.username:
                proxy_config["username"] = proxy.username
            if proxy.password:
                proxy_config["password"] = proxy.password
            if proxy.bypass:
                proxy_config["bypass"] = proxy.bypass
            launch_kwargs["proxy"] = proxy_config
            logger.info(f"Browser '{browser_id}' configured with proxy: {proxy.server}")
        
        # Launch browser with persistent context (Playwright creates dir if needed)
        # This returns a BrowserContext, but we can access the Browser via context.browser
        context = await self.playwright.chromium.launch_persistent_context(**launch_kwargs)
        
        # Get the browser object from the context so we can properly close it
        browser = context.browser
        if browser is None:
            raise RuntimeError(f"Failed to get browser object from context for profile {profile_path}")
        
        browser_info = BrowserInfo(browser, context, profile_path)
        self.browsers[browser_id] = browser_info
        logger.info(f"Created browser '{browser_id}' with profile at {profile_path}")
        
        return browser_id, browser_info
    
    def get_browser(self, browser_id: str) -> BrowserInfo:
        """Get a browser by its ID."""
        if browser_id not in self.browsers:
            raise KeyError(f"Browser with id '{browser_id}' not found")
        return self.browsers[browser_id]
    
    def get_default_browser(self) -> BrowserInfo:
        """Get the default browser."""
        return self.browsers[str(PROFILES_DIR / DEFAULT_BROWSER_ID)]
    
    def get_default_browser_id(self) -> str:
        """Get the default browser ID."""
        return str(PROFILES_DIR / DEFAULT_BROWSER_ID)
    
    async def close_browser(self, browser_id: str) -> bool:
        """Close and remove a browser instance."""
        default_id = str(PROFILES_DIR / DEFAULT_BROWSER_ID)
        if browser_id == default_id:
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
    
    async def shutdown(self):
        """Close all browsers and cleanup."""
        for browser_id in list(self.browsers.keys()):
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
                logger.warning(f"Error closing context for browser {browser_id}: {e}")
            
            # Close browser (this properly releases all resources and lock files)
            try:
                await browser_info.browser.close()
            except Exception as e:
                logger.warning(f"Error closing browser {browser_id}: {e}")
        
        self.browsers.clear()
        logger.info("All browsers closed")


# Global browser manager
browser_manager = BrowserManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Clean up Chrome lock files before starting Playwright
    # This ensures a clean state on container restart
    default_profile = PROFILES_DIR / DEFAULT_BROWSER_ID
    default_profile.mkdir(parents=True, exist_ok=True)
    
    # Start playwright and browser manager
    playwright = await async_playwright().start()
    await browser_manager.start(playwright)
    
    app.state.playwright = playwright
    app.state.browser_manager = browser_manager
    
    yield
    
    # Cleanup: properly close all browsers before stopping Playwright
    await browser_manager.shutdown()
    await playwright.stop()


app = FastAPI(title="Controller API", version="0.2.0", lifespan=lifespan)


# Request/Response models for browser management
class ProxySettings(BaseModel):
    """Proxy configuration for browser."""
    server: str = Field(..., description="Proxy server URL (e.g., 'http://myproxy.com:3128')")
    username: Optional[str] = Field(default=None, description="Proxy authentication username")
    password: Optional[str] = Field(default=None, description="Proxy authentication password")
    bypass: Optional[str] = Field(default=None, description="Comma-separated hosts to bypass proxy")


class CreateBrowserRequest(BaseModel):
    profile_dir: Optional[str] = Field(
        default=None, 
        description="Profile directory path. If provided, used as browser_id. If not, UUID is generated."
    )
    proxy: Optional[ProxySettings] = Field(
        default=None,
        description="Proxy settings for the browser. Only applies when creating a new browser."
    )


class BrowserResponse(BaseModel):
    browser_id: str
    profile_path: str
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
    """Get browser info, defaulting to the default browser."""
    manager: BrowserManager = request.app.state.browser_manager
    bid = browser_id or manager.get_default_browser_id()
    try:
        return manager.get_browser(bid)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Browser '{bid}' not found")


BrowserInfoDep = Annotated[BrowserInfo, Depends(get_browser_info)]


async def get_or_create_page(
    request: Request,
    browser_info: BrowserInfoDep,
    session_id: SessionIdDep = None
) -> Page:
    """
    Get or create a page object for the given session ID within a browser.
    If no session_id is provided, a new one is generated and stored in the request state.
    """
    # Generate session_id if not provided
    if session_id is None:
        session_id = str(uuid.uuid4())
    request.state.session_id = session_id
    
    pages = browser_info.pages
    
    # Check if page already exists
    page = pages.get(session_id, None)
    if page:
        # Verify page is still valid (not closed)
        try:
            _ = page.url
            return page
        except Exception:
            logger.info(f"Page for session {session_id} was closed, creating new one")
            page = await browser_info.context.new_page()
            pages[session_id] = page
            return page
    
    # Create new page
    page = await browser_info.context.new_page()
    pages[session_id] = page
    logger.info(f"Created new page for session {session_id}")
    return page


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
            profile_path=str(info.profile_path),
            session_count=len(info.pages)
        )
        for bid, info in browser_manager.browsers.items()
    ]


@app.post("/browsers")
async def create_browser(request: CreateBrowserRequest = CreateBrowserRequest()) -> BrowserResponse:
    """
    Create a new browser instance.
    
    If profile_dir is provided, it becomes the browser_id.
    If not provided, a UUID is generated as browser_id and profile stored in profiles/{uuid}.
    
    Proxy settings can be specified to route all browser traffic through a proxy server.
    Note: Proxy settings can only be configured when creating a new browser.
    
    The profile directory is created by Playwright when the browser starts.
    """
    try:
        browser_id, browser_info = await browser_manager.create_browser(
            profile_dir=request.profile_dir,
            proxy=request.proxy
        )
        return BrowserResponse(
            browser_id=browser_id,
            profile_path=str(browser_info.profile_path),
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
    """
    # Use request body browser_id if header not provided
    bid = browser_id or request.browser_id or browser_manager.get_default_browser_id()
    
    try:
        browser_info = browser_manager.get_browser(bid)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Browser '{bid}' not found")
    
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
        result_divs = await page.query_selector_all('div[data-rpos]')
        
        for result_div in result_divs:
            if len(results) >= request.count:
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
                title = await link_element.inner_text()

                snippet_texts = []
                for span in span_elements:
                    html = await span.inner_html()
                    if '<em>' in html:
                        text = await span.inner_text()
                        snippet_texts.append(text)

                snippet = '\n'.join(snippet_texts)

                results.append(SearchResult(
                    link=href,
                    title=title,
                    snippet=snippet
                ))
                        
            except Exception:
                continue
        
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

