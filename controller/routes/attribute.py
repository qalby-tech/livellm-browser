import asyncio
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from core.dependencies import PageDep
from helpers.bs import extract_selectors
from helpers.playwright import scroll_to_bottom
from models.requests import AttributeRequest

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Attribute"])


@router.post("/attribute")
async def get_attributes(request: AttributeRequest, page: PageDep) -> JSONResponse:
    """
    Extract structured data from a page using CSS or XPath selectors.

    Flow: navigate → idle → scroll_to_bottom → get HTML → parse with
    BeautifulSoup (CSS) or lxml (XPath) → return JSON list.

    Parsing runs in a background thread (``asyncio.to_thread``) so it
    does not block the event loop.

    Returns a JSON array of ``{"name": str, "values": [str, ...]}`` objects,
    one per selector defined in the request.
    """
    try:
        if request.url:
            await page.goto(request.url, wait_until=request.wait_until, timeout=request.timeout)

        if request.idle > 0:
            await asyncio.sleep(request.idle)

        if request.steps > 0:
            scroll_timeout = request.steps * request.step_delay
            await scroll_to_bottom(page, request.step_pixels, request.step_delay, scroll_timeout)
            logger.info(
                "Scrolled %d steps (%.1fs), pixels=%d, delay=%.1fs",
                request.steps, scroll_timeout, request.step_pixels, request.step_delay,
            )

        html = await page.content()

        try:
            await page.evaluate("window.stop()")
        except Exception:
            pass

        selector_dicts = [
            {
                "name": s.name,
                "selector": s.selector,
                "type": s.type,
                "attribute": s.attribute,
            }
            for s in request.selectors
        ]
        results = await extract_selectors(html, selector_dicts)

        return JSONResponse(content=results)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to extract attributes: {str(e)}")
