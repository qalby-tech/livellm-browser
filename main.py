import asyncio
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.responses import Response
from models.responses import (
    PingResponse, 
    SearchResult,
    SelectorResult,
    ActionResult
)
from js_helpers import (
    get_html_js,
    get_text_js,
    get_click_js,
    get_fill_js,
    get_attribute_js,
    get_attribute_direct_xpath_js
)
from models.requests import (
    SearchRequest,
    GetHtmlRequest,
    SelectorRequest,
    MouseMoveRequest,
    ScreenshotRequest,
    ClickRequest,
    ScrollRequest,
    HtmlAction,
    TextAction,
    ClickAction,
    FillAction,
    AttributeAction
)
from contextlib import asynccontextmanager
from patchright.async_api import async_playwright
from patchright.async_api import BrowserContext
from patchright.async_api import Page
import uuid
import logging
from typing import List, Annotated, Optional


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

@asynccontextmanager
async def lifespan(app: FastAPI):
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch_persistent_context(
        user_data_dir="./profile",
        headless=False,
        channel="chrome",
        no_viewport=True,
        args=["--start-maximized"],
    )
    app.state.playwright = playwright
    app.state.browser = browser
    app.state.pages: dict[str, Page] = {} # id: page object
    yield
    # Clean up all pages before closing browser
    for page in app.state.pages.values():
        try:
            await page.close()
        except Exception as e:
            logger.warning(f"Error closing page: {e}")
    await browser.close()
    await playwright.stop()


app = FastAPI(title="Controller API", version="0.1.0", lifespan=lifespan)


BrowserContextDep = Annotated[BrowserContext, Depends(lambda: app.state.browser)]
SessionIdDep = Annotated[Optional[str], Header(alias="X-Session-Id")]

async def get_or_create_page(
    request: Request,
    context: BrowserContextDep, 
    session_id: SessionIdDep = None
) -> Page:
    """
    Get or create a page object for the given session ID.
    If no session_id is provided, a new one is generated and stored in the request state.
    """
    # Generate session_id if not provided
    if session_id is None:
        session_id = str(uuid.uuid4())
        request.state.session_id = session_id
    else:
        request.state.session_id = session_id
    
    # Get pages dict from app state
    pages = request.app.state.pages
    
    # Check if page already exists
    page = pages.get(session_id, None)
    if page:
        # Verify page is still valid (not closed)
        try:
            # Try to access a property to check if page is still valid
            _ = page.url
            return page
        except Exception:
            # Page was closed, create a new one
            logger.info(f"Page for session {session_id} was closed, creating new one")
            page = await context.new_page()
            pages[session_id] = page
            return page
    
    # Create new page
    page = await context.new_page()
    pages[session_id] = page
    logger.info(f"Created new page for session {session_id}")
    return page


PageDep = Annotated[Page, Depends(get_or_create_page)]


@app.get("/ping")
async def root() -> PingResponse:
    """Health check endpoint"""
    return PingResponse(status="ok", message="Controller API is running")


@app.get("/start_session")
async def start_session(context: BrowserContextDep) -> dict:
    """
    Start a new session and return the session ID (creates page object).
    You can use this session_id in the X-Session-Id header for subsequent requests.
    """
    session_id = str(uuid.uuid4())
    page = await context.new_page()
    app.state.pages[session_id] = page
    logger.info(f"Started new session: {session_id}")
    return {"session_id": session_id, "message": "Session created. Use X-Session-Id header in subsequent requests."}


@app.get("/end_session")
async def end_session(session_id: SessionIdDep = None) -> dict:
    """
    End a session and delete the page object.
    Requires X-Session-Id header.
    """
    if session_id is None:
        raise HTTPException(status_code=400, detail="X-Session-Id header is required")
    
    page = app.state.pages.pop(session_id, None)
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
        # Navigate to Google search
        await page.goto(f"https://www.google.com/search?q={request.query}&num={request.count}", wait_until="commit")
        
        await asyncio.sleep(3)

        # Extract search results
        results = []
        
        # Get all divs with data-rpos attribute
        result_divs = await page.query_selector_all('div[data-rpos]')
        
        for result_div in result_divs:
            if len(results) >= request.count:
                break
                
            try:
                # Inside this div, find all span elements
                span_elements = await result_div.query_selector_all('span')

                # Find the first span that contains an <a> tag
                link_element = None
                for span in span_elements:
                    a = await span.query_selector('a')
                    if a:
                        link_element = a
                        break

                if not link_element:
                    continue  # skip if no link found

                href = await link_element.get_attribute('href')
                title = await link_element.inner_text()

                # For snippet: find span(s) whose inner_html contains <em>
                # (because inner_text() strips tags, so you can't check for <em> in text)
                snippet_texts = []
                for span in span_elements:
                    html = await span.inner_html()
                    if '<em>' in html:
                        text = await span.inner_text()
                        snippet_texts.append(text)

                # Join snippets or take first, depending on your needs
                snippet = '\n'.join(snippet_texts)  # or snippet_texts[0] if you expect one

                results.append(SearchResult(
                    link=href,
                    title=title,
                    snippet=snippet
                ))
                        
            except Exception as e:
                # Skip this result if extraction fails
                continue
        
        return results
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to search: {str(e)}")


@app.post("/content")
async def get_content(request: GetHtmlRequest, page: PageDep) -> str:
    """
    Get the HTML or text content of the given page
    
    Args:
        request: GetHtmlRequest with URL and return_html flag
        page: Page object automatically managed by session (via X-Session-Id header)
        
    Returns:
        str: The HTML content if return_html is True, or inner text if False
    """
    try:
        await page.goto(request.url, wait_until=request.wait_until, timeout=request.timeout)
        
        # Wait for idle time if specified
        if request.idle > 0:
            await asyncio.sleep(request.idle)
        
        if request.return_html:
            content = await page.content()
        else:
            # Get inner text of the whole page
            content = await page.inner_text("body")
        
        return content
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get content: {str(e)}")


@app.post("/selectors")
async def execute_selectors(request: SelectorRequest, page: PageDep) -> List[SelectorResult]:
    """
    Execute CSS or XPath selectors on a page and perform actions on matched elements.
    
    All actions use page.evaluate() with isolated_context=True for undetectability.
    
    Args:
        request: SelectorRequest with URL and list of selectors with actions to execute
        page: Page object automatically managed by session (via X-Session-Id header)
        
    Returns:
        List[SelectorResult]: Array of results with name and action results for each selector
        
    Action types (each action is a separate model):
        - HtmlAction: Returns outer HTML of elements (default)
        - TextAction: Returns text content of elements
        - ClickAction: Clicks on element(s) - supports nth param (0=first, -1=last, null=all)
        - FillAction: Fills input element(s) - supports nth param (0=first, -1=last, null=all)
        - AttributeAction: Gets attribute value (requires 'name' param)
        
    Examples:
        
        1. Get HTML (default behavior):
           {"name": "nav", "type": "css", "value": "nav.main"}
           
        2. Get text content:
           {"name": "titles", "type": "css", "value": "h1", "actions": [{"action": "text"}]}
           
        3. Click first element (default nth=0):
           {"name": "btn", "type": "css", "value": "#submit", "actions": [{"action": "click"}]}
           
        4. Click last element:
           {"name": "btn", "type": "css", "value": ".item", "actions": [{"action": "click", "nth": -1}]}
           
        5. Click all elements:
           {"name": "btns", "type": "css", "value": ".btn", "actions": [{"action": "click", "nth": null}]}
           
        6. Fill first input (default nth=0):
           {"name": "search", "type": "css", "value": "input[name='q']", 
            "actions": [{"action": "fill", "value": "search term"}]}
            
        7. Fill all inputs:
           {"name": "fields", "type": "css", "value": "input.field",
            "actions": [{"action": "fill", "value": "test", "nth": null}]}
           
        8. Get href attribute (CSS):
           {"name": "links", "type": "css", "value": "a.nav-link", 
            "actions": [{"action": "attribute", "name": "href"}]}
           
        9. Get href attribute (XPath direct):
           {"name": "links", "type": "xml", "value": "//a/@href"}
           
        10. Multiple actions (get text then click first):
            {"name": "menu", "type": "css", "value": ".dropdown-item", 
             "actions": [{"action": "text"}, {"action": "click"}]}
    """
    try:
        await page.goto(request.url, wait_until=request.wait_until, timeout=request.timeout)
        
        # Wait for idle time if specified
        if request.idle > 0:
            await asyncio.sleep(request.idle)
        
        results = []
        
        for selector in request.selectors:
            action_results = []
            
            for action in selector.actions:
                try:
                    values = []
                    
                    if isinstance(action, HtmlAction):
                        # Get outer HTML
                        js_code = get_html_js(selector.type)
                        values = await page.evaluate(js_code, selector.value, isolated_context=True)
                        logger.info(f"Got {len(values)} HTML elements for selector {selector.name}")
                        
                    elif isinstance(action, TextAction):
                        # Get text content
                        js_code = get_text_js(selector.type)
                        values = await page.evaluate(js_code, selector.value, isolated_context=True)
                        logger.info(f"Got {len(values)} text values for selector {selector.name}")
                        
                    elif isinstance(action, ClickAction):
                        # Click on elements with nth parameter
                        # nth: 0=first (default), -1=last, None=all
                        js_code = get_click_js(selector.type)
                        values = await page.evaluate(
                            js_code, 
                            [selector.value, action.nth], 
                            isolated_context=True
                        )
                        nth_desc = "first" if action.nth == 0 else ("last" if action.nth == -1 else "all")
                        logger.info(f"Clicked {len(values)} elements ({nth_desc}) for selector {selector.name}")
                        
                    elif isinstance(action, FillAction):
                        # Fill elements with value and nth parameter
                        # nth: 0=first (default), -1=last, None=all
                        js_code = get_fill_js(selector.type)
                        values = await page.evaluate(
                            js_code, 
                            [selector.value, action.value, action.nth], 
                            isolated_context=True
                        )
                        nth_desc = "first" if action.nth == 0 else ("last" if action.nth == -1 else "all")
                        logger.info(f"Filled {len(values)} elements ({nth_desc}) for selector {selector.name}")
                            
                    elif isinstance(action, AttributeAction):
                        # Get attribute value
                        # Check if XPath selector already targets attribute (e.g., //a/@href)
                        if selector.type == "xml" and "/@" in selector.value:
                            js_code = get_attribute_direct_xpath_js()
                            values = await page.evaluate(js_code, selector.value, isolated_context=True)
                        else:
                            js_code = get_attribute_js(selector.type)
                            values = await page.evaluate(
                                js_code, 
                                [selector.value, action.name], 
                                isolated_context=True
                            )
                        logger.info(f"Got {len(values)} attribute values for selector {selector.name}")
                    
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


@app.post("/screenshot")
async def get_screenshot(request: ScreenshotRequest, page: PageDep) -> Response:
    """
    Take a screenshot of the given webpage
    
    Args:
        request: ScreenshotRequest with URL and screenshot options
        page: Page object automatically managed by session (via X-Session-Id header)
        
    Returns:
        Response: PNG image of the screenshot
    """
    try:
        await page.goto(request.url, wait_until=request.wait_until, timeout=request.timeout)
        
        # Wait for idle time if specified
        if request.idle > 0:
            await asyncio.sleep(request.idle)
        
        # Take screenshot
        screenshot_bytes = await page.screenshot(full_page=request.full_page, type="png")
        
        return Response(content=screenshot_bytes, media_type="image/png")
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to take screenshot: {str(e)}")


@app.post("/move")
async def move_cursor(request: MouseMoveRequest, page: PageDep) -> dict:
    """
    Move the mouse cursor to a specific point on the page
    
    Args:
        request: MouseMoveRequest with URL and x, y coordinates
        page: Page object automatically managed by session (via X-Session-Id header)
        
    Returns:
        dict: Status message indicating success
    """
    try:
        await page.goto(request.url, wait_until="commit")
        
        # Move mouse to the specified coordinates
        await page.mouse.move(request.x, request.y, steps=request.steps)
        
        return {
            "status": "success",
            "message": f"Mouse moved to ({request.x}, {request.y}) with {request.steps} steps"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to move cursor: {str(e)}")


@app.post("/click")
async def click(request: ClickRequest, page: PageDep) -> dict:
    """
    Click at a specific point on the page
    
    Args:
        request: ClickRequest with URL and x, y coordinates
        page: Page object automatically managed by session (via X-Session-Id header)
        
    Returns:
        dict: Status message indicating success
    """
    try:
        await page.goto(request.url, wait_until="commit")
        
        # Click at the specified coordinates
        await page.mouse.click(
            request.x, 
            request.y, 
            button=request.button,
            click_count=request.click_count,
            delay=request.delay
        )
        
        return {
            "status": "success",
            "message": f"Clicked at ({request.x}, {request.y}) with {request.button} button"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to click: {str(e)}")


@app.post("/scroll")
async def scroll(request: ScrollRequest, page: PageDep) -> dict:
    """
    Scroll the page by specified delta
    
    Args:
        request: ScrollRequest with URL and scroll deltas
        page: Page object automatically managed by session (via X-Session-Id header)
        
    Returns:
        dict: Status message indicating success
    """
    try:
        await page.goto(request.url, wait_until="commit")
        
        # Scroll the page using mouse wheel
        await page.mouse.wheel(request.x, request.y)
        
        return {
            "status": "success",
            "message": f"Scrolled by ({request.x}, {request.y})"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to scroll: {str(e)}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )
