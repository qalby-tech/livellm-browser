import os
import logging
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

REDIS_KEY = "livellm:browsers"


def get_redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://localhost:6379/0")


def get_browser_address() -> str:
    """Get the address that the controller can use to reach this browser.

    Priority:
      1. BROWSER_ADDRESS env var (explicit override)
      2. POD_IP env var (Kubernetes downward API)
      3. Auto-detect via socket
    """
    addr = os.environ.get("BROWSER_ADDRESS")
    if addr:
        return addr
    pod_ip = os.environ.get("POD_IP")
    if pod_ip:
        return pod_ip
    import socket
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "127.0.0.1"


class RedisBrowserState:
    """Publishes browser WS URLs to Redis so the controller can discover them."""

    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None

    async def connect(self):
        url = get_redis_url()
        self.redis = aioredis.from_url(url, decode_responses=True)
        await self.redis.ping()
        logger.info(f"Connected to Redis at {url}")

    async def register_browser(self, browser_id: str, proxy_port: int):
        """Publish browser WS URL to Redis."""
        if not self.redis:
            return
        address = get_browser_address()
        ws_url = f"ws://{address}:{proxy_port}/devtools/browser/{browser_id}"
        await self.redis.hset(REDIS_KEY, browser_id, ws_url)
        logger.info(f"Registered browser '{browser_id}' in Redis: {ws_url}")

    async def unregister_browser(self, browser_id: str):
        """Remove browser from Redis."""
        if not self.redis:
            return
        await self.redis.hdel(REDIS_KEY, browser_id)
        logger.info(f"Unregistered browser '{browser_id}' from Redis")

    async def unregister_all(self):
        """Remove all browsers registered by this instance."""
        if not self.redis:
            return
        await self.redis.delete(REDIS_KEY)
        logger.info("Cleared all browser entries from Redis")

    async def disconnect(self):
        if self.redis:
            await self.redis.aclose()
            self.redis = None
            logger.info("Disconnected from Redis")


redis_browser_state = RedisBrowserState()