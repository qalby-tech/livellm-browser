import asyncio
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from core.dependencies import PageDep
from helpers.playwright import scroll_to_bottom
from models.requests import ContentRequest, OutputAction

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Content"])


@router.post("/content")
async def get_content(request: ContentRequest, page: PageDep) -> Response:
    """
    Get page content with automatic scrolling.

    Shortcut for: navigate → idle → scroll_to_bottom → output.
    The scroll timeout is ``steps × step_delay`` seconds.

    ``output_action`` controls the response format:
    - ``text`` — inner text (default)
    - ``html`` — full page HTML
    - ``screenshot`` — viewport PNG screenshot
    - ``screenshot_full`` — full-page PNG screenshot
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
                f"Scrolled {request.steps} steps ({scroll_timeout:.1f}s), "
                f"pixels={request.step_pixels}, delay={request.step_delay}s"
            )

        if request.output_action in (OutputAction.screenshot, OutputAction.screenshot_full):
            full = request.output_action == OutputAction.screenshot_full
            screenshot_bytes = await page.screenshot(full_page=full, type="png")
            response = Response(content=screenshot_bytes, media_type="image/png")
        elif request.output_action == OutputAction.html:
            content = await page.content()
            response = Response(content=content, media_type="text/html")
        else:
            content = await page.inner_text("body")
            response = Response(content=content, media_type="text/plain")

        # Halt any in-flight resource downloads so Chrome stops streaming
        # bytes back over CDP into the Node driver heap.
        try:
            await page.evaluate("window.stop()")
        except Exception:
            pass

        return response

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get content: {str(e)}")
