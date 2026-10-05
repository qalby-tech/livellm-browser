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
import logging
import re

from patchright.async_api import (  # noqa: F401
    Browser,
    BrowserContext,
    ElementHandle,
    Page,
    Playwright,
    async_playwright as _chrome_playwright,
)
from playwright.async_api import async_playwright as _camoufox_playwright
import patchright._impl._transport as _chrome_transport
import playwright._impl._transport as _camoufox_transport

logger = logging.getLogger(__name__)

CHROME = "chrome"
CAMOUFOX = "camoufox"
ENGINES = (CHROME, CAMOUFOX)

# NODE_OPTIONS' --max-old-space-size (sized by the operator from the pod's
# memory limit) is the Browser API's whole Node heap budget. A mixed pool runs
# one driver per engine, so each driver is started with an equal share of it:
# together they never pass what the pod was sized for. (A Chrome-only pool's
# one driver has half the budget too: the heap a second driver would get stays
# free for Python and the pages' traffic.)
_HEAP = re.compile(r"(--max[-_]old[-_]space[-_]size=)(\d+)")


def driver_node_options(value):
    """One driver's NODE_OPTIONS: ``value`` with every heap cap split into
    len(ENGINES) shares (other options kept); None or no cap = unchanged."""
    if not value:
        return value
    return _HEAP.sub(lambda m: f"{m.group(1)}{max(int(m.group(2)) // len(ENGINES), 1)}", value)


def _split_heap(get_driver_env):
    def driver_env() -> dict:
        env = get_driver_env()
        if env.get("NODE_OPTIONS"):
            env["NODE_OPTIONS"] = driver_node_options(env["NODE_OPTIONS"])
        return env

    driver_env.splits_heap = True
    return driver_env


# Each client's transport starts its Node driver with get_driver_env(), bound
# by name in that module; a client that no longer has it keeps the whole cap
# (tests/test_driver_heap.py fails then).
for _transport in (_chrome_transport, _camoufox_transport):
    _env = getattr(_transport, "get_driver_env", None)
    if _env is None:
        logger.warning(f"{_transport.__name__} has no get_driver_env: its driver keeps the whole heap cap")
    elif not getattr(_env, "splits_heap", False):
        _transport.get_driver_env = _split_heap(_env)


def async_playwright(engine: str = CHROME):
    """The client of an engine, to ``.start()`` (one Node driver each)."""
    if engine == CAMOUFOX:
        return _camoufox_playwright()
    if engine == CHROME:
        return _chrome_playwright()
    raise ValueError(f"unknown engine {engine!r}")
