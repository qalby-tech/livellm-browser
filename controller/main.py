import asyncio
import logging

from fastapi import FastAPI
from contextlib import asynccontextmanager
from patchright.async_api import async_playwright

from core.browser import browser_manager
from core.registry import browser_registry
from routes import health, browsers, search, content, interact, attribute


# ==================== Logging ====================

class PingFilter(logging.Filter):
    """Filter out /ping health check requests from access logs."""
    def filter(self, record: logging.LogRecord) -> bool:
        return "/ping" not in record.getMessage()


logging.getLogger("uvicorn.access").addFilter(PingFilter())

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
logger.addHandler(_handler)


# ==================== Background Tasks ====================

STALE_PAGE_CLEANUP_INTERVAL = 60  # seconds between cleanup sweeps


async def _stale_page_cleanup_loop():
    """Periodically close pages that are no longer responsive.

    Patchright's Node.js driver leaks memory when pages accumulate — each
    open page holds references in the driver process.  Left unchecked the
    driver eventually OOM-crashes (``FATAL ERROR: Ineffective mark-compacts
    near heap limit``), killing every connection.
    """
    try:
        while True:
            await asyncio.sleep(STALE_PAGE_CLEANUP_INTERVAL)
            try:
                await browser_manager.cleanup_stale_pages()
            except Exception as e:
                logger.warning(f"Stale page cleanup failed: {e}")
    except asyncio.CancelledError:
        pass


# ==================== Lifespan ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start Playwright
    playwright = await async_playwright().start()
    await browser_manager.start(playwright)

    app.state.playwright = playwright
    app.state.browser_manager = browser_manager

    # Warm-connect any browsers already in the registry (best-effort). Browsers
    # are also auto-connected lazily on first request, so this is just an
    # optimization — registry misses or connection failures are non-fatal.
    try:
        browsers = browser_registry.get_all_browsers()
        for browser_id, ws_url in browsers.items():
            if browser_id not in browser_manager.browsers:
                try:
                    await browser_manager.connect_browser(browser_id, ws_url)
                    logger.info(f"Warm-connected browser '{browser_id}' from registry: {ws_url}")
                except Exception as e:
                    logger.warning(f"Failed to warm-connect browser '{browser_id}': {e}")
        if browsers:
            logger.info(f"Registry lists {len(browsers)} browser(s)")
    except Exception as e:
        logger.warning(f"Failed to warm-connect browsers from registry: {e}")

    # Start periodic stale page cleanup to prevent memory leaks in the
    # Patchright Node.js driver (which OOM-crashes if pages accumulate).
    stale_cleanup_task = asyncio.create_task(_stale_page_cleanup_loop())

    logger.info("Controller started — browser discovery via file registry")

    yield

    # Graceful shutdown
    logger.info("Application shutting down, cleaning up resources...")
    stale_cleanup_task.cancel()
    try:
        await stale_cleanup_task
    except asyncio.CancelledError:
        pass
    try:
        await browser_manager.shutdown(timeout=25.0)
    except Exception as e:
        logger.error(f"Error during browser shutdown: {e}")
    # Use manager's playwright (may have been restarted during recovery)
    pw = browser_manager.playwright or playwright
    try:
        await asyncio.wait_for(pw.stop(), timeout=5.0)
    except asyncio.TimeoutError:
        logger.warning("Timeout stopping playwright, continuing shutdown")
    except Exception as e:
        logger.warning(f"Error stopping playwright: {e}")
    logger.info("Shutdown complete")


# ==================== App ====================

app = FastAPI(
    title="Controller API",
    version="0.4.0",
    lifespan=lifespan,
    root_path="/parser",
)

app.include_router(health.router)
app.include_router(browsers.router)
app.include_router(search.router)
app.include_router(content.router)
app.include_router(interact.router)
app.include_router(attribute.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
