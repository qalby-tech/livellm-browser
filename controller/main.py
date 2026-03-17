import asyncio
import logging

from fastapi import FastAPI
from contextlib import asynccontextmanager
from patchright.async_api import async_playwright

from core.browser import browser_manager
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


# ==================== Lifespan ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start Playwright — no auto-connections; the operator (or API calls)
    # will register browsers via POST /browsers.
    playwright = await async_playwright().start()
    await browser_manager.start(playwright)

    app.state.playwright = playwright
    app.state.browser_manager = browser_manager

    logger.info("Controller started — waiting for browser registrations via POST /browsers")

    yield

    # Graceful shutdown
    logger.info("Application shutting down, cleaning up resources...")
    try:
        await browser_manager.shutdown(timeout=25.0)
    except Exception as e:
        logger.error(f"Error during browser shutdown: {e}")
    try:
        await asyncio.wait_for(playwright.stop(), timeout=5.0)
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
