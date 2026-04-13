import asyncio
import os
import logging
import time
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

REDIS_KEY = "livellm:browsers"

# Redis key TTL — browsers must re-register within this window to stay visible.
BROWSER_TTL_SECONDS = 60
# How often the background heartbeat runs.
HEARTBEAT_INTERVAL = 15


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
    """Publishes browser WS URLs to Redis so the controller can discover them.

    Each browser entry is a JSON-encoded hash stored under ``livellm:browsers``
    with the browser_id as the field name.  A background heartbeat task keeps
    the entries alive via key-level TTLs.
    """

    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None
        self._registered: dict[str, dict] = {}  # {browser_id: {address, proxy_port}}
        self._heartbeat_task: Optional[asyncio.Task] = None

    async def connect(self):
        url = get_redis_url()
        self.redis = aioredis.from_url(url, decode_responses=True)
        await self.redis.ping()
        logger.info(f"Connected to Redis at {url}")
        # Start background heartbeat
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def register_browser(self, browser_id: str, proxy_port: int):
        """Publish browser WS URL to Redis."""
        if not self.redis:
            return

        address = get_browser_address()
        ws_url = f"ws://{address}:{proxy_port}/devtools/browser/{browser_id}"

        entry = {
            "ws_url": ws_url,
            "proxy_port": str(proxy_port),
            "registered_at": str(int(time.time())),
        }

        # Store as JSON string in the hash field
        import json
        await self.redis.hset(REDIS_KEY, browser_id, json.dumps(entry))
        # Set per-field expiry (requires Redis 7.4+) or rely on heartbeat refresh
        await self.redis.expire(REDIS_KEY, BROWSER_TTL_SECONDS * 3)

        self._registered[browser_id] = {"address": address, "proxy_port": proxy_port}
        logger.info(f"Registered browser '{browser_id}' in Redis: {ws_url}")

    async def unregister_browser(self, browser_id: str):
        """Remove a specific browser from Redis."""
        if not self.redis:
            return
        await self.redis.hdel(REDIS_KEY, browser_id)
        self._registered.pop(browser_id, None)
        logger.info(f"Unregistered browser '{browser_id}' from Redis")

    async def unregister_all(self):
        """Remove only browsers registered by this instance."""
        if not self.redis:
            return
        for browser_id in list(self._registered.keys()):
            try:
                await self.redis.hdel(REDIS_KEY, browser_id)
            except Exception as e:
                logger.warning(f"Failed to unregister '{browser_id}': {e}")
        self._registered.clear()
        logger.info("Cleared all instance browser entries from Redis")

    async def disconnect(self):
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self.redis:
            await self.redis.aclose()
            self.redis = None
            logger.info("Disconnected from Redis")

    async def _heartbeat_loop(self):
        """Periodically re-register all browsers to keep entries alive."""
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if not self.redis or not self._registered:
                    continue
                try:
                    import json
                    pipe = self.redis.pipeline()
                    for browser_id, info in list(self._registered.items()):
                        ws_url = f"ws://{info['address']}:{info['proxy_port']}/devtools/browser/{browser_id}"
                        entry = {
                            "ws_url": ws_url,
                            "proxy_port": str(info["proxy_port"]),
                            "registered_at": str(int(time.time())),
                        }
                        pipe.hset(REDIS_KEY, browser_id, json.dumps(entry))
                    pipe.expire(REDIS_KEY, BROWSER_TTL_SECONDS * 3)
                    await pipe.execute()
                    logger.debug(f"Heartbeat: refreshed {len(self._registered)} browser(s) in Redis")
                except Exception as e:
                    logger.warning(f"Heartbeat failed: {e}")
        except asyncio.CancelledError:
            pass


redis_browser_state = RedisBrowserState()