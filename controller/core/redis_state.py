import asyncio
import json
import logging
import os
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

REDIS_KEY = "livellm:browsers"
SYNC_INTERVAL = 10  # seconds between Redis → BrowserManager syncs


def get_redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://localhost:6379/0")


class RedisControllerState:
    """Reads browser WS URLs from Redis so the controller can auto-discover browsers.

    Provides both on-demand lookups (``get_browser_ws_url``) and a background
    sync task that continuously reconciles the BrowserManager with Redis state.
    """

    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None
        self._sync_task: Optional[asyncio.Task] = None
        self._browser_manager = None  # set by start_sync()

    async def connect(self):
        url = get_redis_url()
        self.redis = aioredis.from_url(url, decode_responses=True)
        await self.redis.ping()
        logger.info(f"Connected to Redis at {url}")

    async def get_all_browsers(self) -> dict[str, str]:
        """Get all registered browsers from Redis. Returns {browser_id: ws_url}."""
        if not self.redis:
            return {}
        try:
            raw = await self.redis.hgetall(REDIS_KEY)
            if not raw:
                return {}
            result = {}
            for browser_id, value in raw.items():
                try:
                    entry = json.loads(value)
                    ws_url = entry.get("ws_url", value)
                    result[browser_id] = ws_url
                except (json.JSONDecodeError, TypeError):
                    # Legacy format: plain string
                    result[browser_id] = value
            return result
        except Exception as e:
            logger.warning(f"Failed to read browsers from Redis: {e}")
            return {}

    async def get_browser_ws_url(self, browser_id: str) -> Optional[str]:
        """Get a specific browser's WS URL from Redis."""
        if not self.redis:
            return None
        try:
            value = await self.redis.hget(REDIS_KEY, browser_id)
            if not value:
                return None
            try:
                entry = json.loads(value)
                return entry.get("ws_url", value)
            except (json.JSONDecodeError, TypeError):
                return value
        except Exception as e:
            logger.warning(f"Failed to read browser '{browser_id}' from Redis: {e}")
            return None

    async def start_sync(self, browser_manager):
        """Start background sync task that reconciles BrowserManager with Redis.

        This ensures browsers are always connected even if the controller restarts.
        """
        self._browser_manager = browser_manager
        self._sync_task = asyncio.create_task(self._sync_loop())
        logger.info("Redis browser sync task started")

    async def stop_sync(self):
        """Stop the background sync task."""
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
            self._sync_task = None
            logger.info("Redis browser sync task stopped")

    async def _sync_loop(self):
        """Periodically sync browsers from Redis into the BrowserManager."""
        try:
            while True:
                await asyncio.sleep(SYNC_INTERVAL)
                await self._sync_browsers()
                if self._browser_manager:
                    try:
                        await self._browser_manager.cleanup_stale_pages()
                    except Exception as e:
                        logger.debug(f"Sync: stale page cleanup failed: {e}")
        except asyncio.CancelledError:
            pass

    async def _sync_browsers(self):
        """One-shot sync: connect browsers found in Redis but not in BrowserManager,
        and disconnect browsers in BrowserManager but no longer in Redis."""
        if not self._browser_manager or not self.redis:
            return

        try:
            redis_browsers = await self.get_all_browsers()
        except Exception as e:
            logger.warning(f"Sync: failed to read Redis: {e}")
            return

        if not redis_browsers:
            return

        manager = self._browser_manager

        # Connect browsers that are in Redis but not in manager
        for browser_id, ws_url in redis_browsers.items():
            if browser_id not in manager.browsers:
                try:
                    await manager.connect_browser(browser_id, ws_url)
                    logger.info(f"Sync: connected browser '{browser_id}' from Redis")
                except Exception as e:
                    logger.debug(f"Sync: failed to connect '{browser_id}': {e}")

        # Note: we do NOT disconnect browsers that are in the manager but not in Redis.
        # The browser might have a momentary Redis hiccup — the heartbeat will re-register.
        # Stale connections are handled by the per-request recovery in dependencies.py.

    async def disconnect(self):
        await self.stop_sync()
        if self.redis:
            await self.redis.aclose()
            self.redis = None
            logger.info("Disconnected from Redis")


redis_controller_state = RedisControllerState()