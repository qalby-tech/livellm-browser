import asyncio
import base64
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from core.dependencies import PageDep
from helpers.playwright import scroll_to_bottom, click_elements, fill_elements, remove_elements
from models.requests import (
    InteractRequest, OutputAction, MoveAction, MouseClickAction, ScrollAction,
    ScrollToBottomAction, IdleAction, LoginAction, SelectAction,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Interact"])


@router.post("/interact")
async def interact(request: InteractRequest, page: PageDep) -> Response:
    """
    Unified endpoint for page interactions.

    1. Navigate to ``url`` (if provided) and wait ``idle`` seconds.
    2. Execute all ``actions`` in order (scroll, click, move, idle, login, selector).
    3. Return result based on ``output_action``:
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

        for action in request.actions:
            if isinstance(action, SelectAction):
                if action.do == "click":
                    await click_elements(page, action.type, action.value, action.args.nth)
                elif action.do == "fill":
                    await fill_elements(page, action.type, action.value, action.args.value, action.args.nth)
                elif action.do == "remove":
                    await remove_elements(page, action.type, action.value, action.args.nth)

            elif isinstance(action, MoveAction):
                await page.mouse.move(action.x, action.y, steps=action.steps)

            elif isinstance(action, MouseClickAction):
                await page.mouse.click(
                    action.x, action.y,
                    button=action.button,
                    click_count=action.click_count,
                    delay=action.delay,
                )

            elif isinstance(action, ScrollAction):
                await page.mouse.wheel(action.x, action.y)

            elif isinstance(action, ScrollToBottomAction):
                await scroll_to_bottom(page, action.step_pixels, action.step_delay, action.timeout)

            elif isinstance(action, IdleAction):
                await asyncio.sleep(action.duration)

            elif isinstance(action, LoginAction):
                if action.username and action.password:
                    credentials = base64.b64encode(
                        f"{action.username}:{action.password}".encode()
                    ).decode()
                    await page.context.set_extra_http_headers({"Authorization": f"Basic {credentials}"})
                else:
                    await page.context.set_extra_http_headers({})

        # Output based on output_action
        if request.output_action in (OutputAction.screenshot, OutputAction.screenshot_full):
            full = request.output_action == OutputAction.screenshot_full
            screenshot_bytes = await page.screenshot(full_page=full, type="png")
            return Response(content=screenshot_bytes, media_type="image/png")
        elif request.output_action == OutputAction.html:
            content = await page.content()
            return Response(content=content, media_type="text/html")
        else:
            content = await page.inner_text("body")
            return Response(content=content, media_type="text/plain")

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to interact: {str(e)}")
