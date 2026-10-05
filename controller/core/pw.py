"""The Playwright this controller drives its browsers with.

A Chrome Browser API (BROWSER_ENGINE unset or "chrome") runs patchright, the
patched Playwright its Chrome browsers are driven with over CDP. A Camoufox
Browser API (BROWSER_ENGINE=camoufox, its own image built from
requirements-camoufox.txt) runs stock Playwright, the client its Camoufox
browsers' Playwright servers accept: the same minor, or they refuse with 428.

Every module imports the Playwright names from here, so one controller never
mixes the two.
"""
import os

CAMOUFOX = "camoufox"
CHROME = "chrome"

ENGINE = CAMOUFOX if (os.environ.get("BROWSER_ENGINE") or "").strip().lower() == CAMOUFOX else CHROME

if ENGINE == CAMOUFOX:
    from playwright.async_api import (  # noqa: F401
        Browser,
        BrowserContext,
        ElementHandle,
        Page,
        Playwright,
        async_playwright,
    )
else:
    from patchright.async_api import (  # noqa: F401
        Browser,
        BrowserContext,
        ElementHandle,
        Page,
        Playwright,
        async_playwright,
    )
