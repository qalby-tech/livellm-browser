"""The two Playwright clients this Browser API drives its browsers with.

One Browser API holds browsers of both engines. Each engine has its own
client, and each client its own Node driver (core/browser.py starts Chrome's
at start and Camoufox's at the first Camoufox browser):

- Chrome: patchright, the patched Playwright Chrome is driven with over CDP
  (chromium.connect_over_cdp to the browser's CDP proxy);
- Camoufox: stock Playwright, the client a Camoufox browser's own Playwright
  server accepts (firefox.connect; a client of another minor is refused with
  428, so its version follows browser/camoufox's).

A browser's engine is its registry entry's (core/registry.py). Every module
imports the Playwright names from here. Page, ElementHandle and the other
types are for annotations only: both clients have the same API, and nothing
checks an object's class.
"""
from patchright.async_api import (  # noqa: F401
    Browser,
    BrowserContext,
    ElementHandle,
    Page,
    Playwright,
    async_playwright as _chrome_playwright,
)
from playwright.async_api import async_playwright as _camoufox_playwright

CHROME = "chrome"
CAMOUFOX = "camoufox"
ENGINES = (CHROME, CAMOUFOX)


def async_playwright(engine: str = CHROME):
    """The client of an engine, to ``.start()`` (one Node driver each)."""
    if engine == CAMOUFOX:
        return _camoufox_playwright()
    if engine == CHROME:
        return _chrome_playwright()
    raise ValueError(f"unknown engine {engine!r}")
