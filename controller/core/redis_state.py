import asyncio
import json
import logging
from typing import Optional

import redis.asyncio as aioredis

from core.config import settings

logger = logging.getLogger(__name__)

REDIS_KEY = "livellm:browsers"
REDIS_KEY_CONTROLLER = "livellm:controller:browsers"
SYNC_INTERVAL = 10


class RedisControllerState:
    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None
        self._sync_task: Optional[asyncio.Task] = None
        self._browser_manager = None

    async def connect(self):
        self.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        await self.redis.ping()
        logger.info(f"Connected to Redis at {settings.redis_url}")

    async def get_all_browsers(self) -> dict[str, str]:
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
                    result[browser_id] = value
            return result
        except Exception as e:
            logger.warning(f"Failed to read browsers from Redis: {e}")
            return {}

    async def get_browser_ws_url(self, browser_id: str) -> Optional[str]:
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
        self._browser_manager = browser_manager
        self._sync_task = asyncio.create_task(self._sync_loop())
        logger.info("Redis browser sync task started")

    async def stop_sync(self):
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
            self._sync_task = None
            logger.info("Redis browser sync task stopped")

    async def _sync_loop(self):
        try:
            while True:
                await asyncio.sleep(SYNC_INTERVAL)
                await self._sync_browsers()
                await self._publish_controller_state()
        except asyncio.CancelledError:
            pass

    async def _sync_browsers(self):
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

        for browser_id, ws_url in redis_browsers.items():
            existing = manager.browsers.get(browser_id)

            if existing is None:
                try:
                    await manager.connect_browser(browser_id, ws_url)
                    logger.info(f"Sync: connected browser '{browser_id}' from Redis")
                except Exception as e:
                    logger.debug(f"Sync: failed to connect '{browser_id}': {e}")
                continue

            if existing.ws_url != ws_url:
                logger.info(
                    f"Sync: ws_url drift for '{browser_id}' "
                    f"({existing.ws_url} -> {ws_url}), reconnecting"
                )
                try:
                    await manager.recover_connection(browser_id, ws_url)
                except Exception as e:
                    logger.warning(f"Sync: reconnect after drift failed for '{browser_id}': {e}")
                continue

            if not existing.browser.is_connected():
                logger.info(f"Sync: '{browser_id}' connection dead, recovering")
                try:
                    await manager.recover_connection(browser_id, ws_url)
                except Exception as e:
                    logger.warning(f"Sync: revive failed for '{browser_id}': {e}")

    async def _publish_controller_state(self):
        if not self._browser_manager or not self.redis:
            return

        try:
            pipe = self.redis.pipeline()
            for browser_id, info in self._browser_manager.browsers.items():
                entry = {
                    "session_count": len(info.pages),
                    "connected": info.browser.is_connected() if info.browser else False,
                    "ws_url": info.ws_url,
                }
                pipe.hset(REDIS_KEY_CONTROLLER, browser_id, json.dumps(entry))
            if self._browser_manager.browsers:
                await pipe.execute()
        except Exception as e:
            logger.debug(f"Failed to publish controller state: {e}")

    async def disconnect(self):
        await self.stop_sync()
        if self.redis:
            await self.redis.aclose()
            self.redis = None
            logger.info("Disconnected from Redis")


redis_controller_state = RedisControllerState()
