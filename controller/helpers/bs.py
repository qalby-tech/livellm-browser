"""
BeautifulSoup / lxml helpers for fast HTML extraction.

All heavy parsing runs synchronously and is offloaded to a thread via
``asyncio.to_thread`` so the event loop stays free.
"""

import asyncio
import logging
from typing import Dict, List, Optional

from bs4 import BeautifulSoup
from lxml import html as lxml_html

logger = logging.getLogger(__name__)


# ==================== Sync helpers (run in thread) ====================

def _extract_css(
    html: str,
    selector: str,
    attribute: Optional[str],
) -> List[str]:
    """Extract values from *html* using a CSS selector (BeautifulSoup)."""
    soup = BeautifulSoup(html, "lxml")
    elements = soup.select(selector)
    if attribute:
        return [el.get(attribute, "") for el in elements if el.get(attribute)]
    return [el.get_text(strip=True) for el in elements if el.get_text(strip=True)]


def _extract_xpath(
    html: str,
    selector: str,
    attribute: Optional[str],
) -> List[str]:
    """Extract values from *html* using an XPath expression (lxml)."""
    tree = lxml_html.fromstring(html)
    elements = tree.xpath(selector)

    results: List[str] = []
    for el in elements:
        if isinstance(el, str):
            # XPath returned a text node or attribute value directly
            val = el.strip()
            if val:
                results.append(val)
        elif isinstance(el, lxml_html.HtmlElement):
            if attribute:
                val = el.get(attribute, "")
                if val:
                    results.append(val)
            else:
                val = lxml_html.tostring(el, method="text", encoding="unicode").strip()
                if val:
                    results.append(val)
    return results


def _extract_all_sync(
    html: str,
    selectors: List[Dict],
) -> List[Dict]:
    """
    Run all selectors against *html* and return results.

    Each selector dict must have: name, selector, type, attribute.
    Returns: [{"name": ..., "values": [...]}, ...]
    """
    results = []
    for sel in selectors:
        name = sel["name"]
        selector = sel["selector"]
        sel_type = sel["type"]
        attribute = sel.get("attribute")

        try:
            if sel_type == "xpath":
                values = _extract_xpath(html, selector, attribute)
            else:
                values = _extract_css(html, selector, attribute)
        except Exception as e:
            logger.warning("Selector '%s' (%s) failed: %s", name, selector, e)
            values = []

        results.append({"name": name, "values": values})

    return results


# ==================== Async wrapper ====================

async def extract_selectors(
    html: str,
    selectors: List[Dict],
) -> List[Dict]:
    """
    Parse *html* and extract data for each selector.

    Runs synchronous BS4 / lxml parsing in a thread so the event loop
    is never blocked.

    Parameters
    ----------
    html : str
        Full page HTML.
    selectors : list[dict]
        Each dict: ``{"name": str, "selector": str, "type": "css"|"xpath",
        "attribute": str|None}``.

    Returns
    -------
    list[dict]
        ``[{"name": ..., "values": [...]}, ...]``
    """
    return await asyncio.to_thread(_extract_all_sync, html, selectors)
