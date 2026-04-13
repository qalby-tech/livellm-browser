import os
import logging
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

REDIS_KEY = "livellm:browsers"


def get_redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://localhost:6379/0")


class RedisControllerState:
    """Reads browser WS URLs from Redis so the controller can auto-discover browsers."""

    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None

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
            data = await self.redis.hgetall(REDIS_KEY)
            return data if data else {}
        except Exception as e:
            logger.warning(f"Failed to read browsers from Redis: {e}")
            return {}

    async def get_browser_ws_url(self, browser_id: str) -> Optional[str]:
        """Get a specific browser's WS URL from Redis."""
        if not self.redis:
            return None
        try:
            return await self.redis.hget(REDIS_KEY, browser_id)
        except Exception as e:
            logger.warning(f"Failed to read browser '{browser_id}' from Redis: {e}")
            return None

    async def disconnect(self):
        if self.redis:
            await self.redis.aclose()
            self.redis = None
            logger.info("Disconnected from Redis")


redis_controller_state = RedisControllerState()