import asyncio
import logging
from typing import List, Optional

from patchright.async_api import Page

logger = logging.getLogger(__name__)


def build_locator(page: Page, selector_type: str, selector_value: str):
    """Build a Playwright locator from selector type and value."""
    if selector_type == "css":
        return page.locator(selector_value)
    return page.locator(f"xpath={selector_value}")


async def click_elements(page: Page, selector_type: str, selector_value: str, nth: Optional[int] = 0) -> List[str]:
    """Click on elements. nth=0 first, nth=-1 last, nth=None all."""
    locator = build_locator(page, selector_type, selector_value)
    count = await locator.count()
    if count == 0:
        return []

    results = []
    if nth is None:
        for i in range(count):
            try:
                await locator.nth(i).click()
                results.append("clicked")
            except Exception as e:
                results.append(f"error: {str(e)}")
    elif nth == -1:
        try:
            await locator.last.click()
            results.append("clicked")
        except Exception as e:
            results.append(f"error: {str(e)}")
    else:
        try:
            await locator.nth(nth).click()
            results.append("clicked")
        except Exception as e:
            results.append(f"error: {str(e)}")
    return results


async def fill_elements(
    page: Page, selector_type: str, selector_value: str, value: str, nth: Optional[int] = 0,
) -> List[str]:
    """Fill elements with value. nth=0 first, nth=-1 last, nth=None all."""
    locator = build_locator(page, selector_type, selector_value)
    count = await locator.count()
    if count == 0:
        return []

    results = []
    if nth is None:
        for i in range(count):
            try:
                await locator.nth(i).fill(value)
                results.append("filled")
            except Exception as e:
                results.append(f"error: {str(e)}")
    elif nth == -1:
        try:
            await locator.last.fill(value)
            results.append("filled")
        except Exception as e:
            results.append(f"error: {str(e)}")
    else:
        try:
            await locator.nth(nth).fill(value)
            results.append("filled")
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
        for i in range(count - 1, -1, -1):
            try:
                await locator.nth(i).evaluate("el => el.remove()")
                results.append("removed")
            except Exception as e:
                results.append(f"error: {str(e)}")
        results.reverse()
    elif nth == -1:
        try:
            await locator.last.evaluate("el => el.remove()")
            results.append("removed")
        except Exception as e:
            results.append(f"error: {str(e)}")
    else:
        try:
            await locator.nth(nth).evaluate("el => el.remove()")
            results.append("removed")
        except Exception as e:
            results.append(f"error: {str(e)}")
    return results


async def scroll_to_bottom(page: Page, step_pixels: int, step_delay: float, timeout: float):
    """
    Scroll page to bottom using mouse wheel with step-based scrolling.
    Keeps scrolling until the timeout is reached (duration-based, not bottom-detection).
    """
    start_time = asyncio.get_event_loop().time()
    while True:
        if asyncio.get_event_loop().time() - start_time > timeout:
            break
        await page.mouse.wheel(0, step_pixels)
        await asyncio.sleep(step_delay)
