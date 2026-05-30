import asyncio
import os
import logging
import time
import json
from pathlib import Path
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

REDIS_KEY = "livellm:browsers"
REDIS_KEY_DESIRED = "livellm:desired:browsers"

BROWSER_TTL_SECONDS = 60
HEARTBEAT_INTERVAL = 15
DESIRED_STATE_CHECK_INTERVAL = 10


def get_redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://localhost:6379/0")


def get_browser_address() -> str:
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
    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None
        self._registered: dict[str, dict] = {}
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._desired_state_task: Optional[asyncio.Task] = None
        self._on_desired_state_change = None

    async def connect(self):
        url = get_redis_url()
        self.redis = aioredis.from_url(url, decode_responses=True)
        await self.redis.ping()
        logger.info(f"Connected to Redis at {url}")
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._desired_state_task = asyncio.create_task(self._desired_state_loop())

    def on_desired_state_change(self, callback):
        self._on_desired_state_change = callback

    async def register_browser(self, browser_id: str, proxy_port: int, cdp_port: int = 0, extensions: Optional[list[str]] = None):
        if not self.redis:
            return

        address = get_browser_address()
        ws_url = f"ws://{address}:{proxy_port}/devtools/browser/{browser_id}"

        entry = {
            "ws_url": ws_url,
            "proxy_port": str(proxy_port),
            # cdp_port must be a JSON number — the Go browser-operator
            # unmarshals it into an int and crash-loops on a quoted string.
            "cdp_port": int(cdp_port),
            "registered_at": str(int(time.time())),
            "extensions": extensions or [],
        }

        await self.redis.hset(REDIS_KEY, browser_id, json.dumps(entry))
        await self.redis.expire(REDIS_KEY, BROWSER_TTL_SECONDS * 3)

        self._registered[browser_id] = {"address": address, "proxy_port": proxy_port, "cdp_port": cdp_port, "extensions": extensions or []}
        logger.info(f"Registered browser '{browser_id}' in Redis: {ws_url}")

    async def unregister_browser(self, browser_id: str):
        if not self.redis:
            return
        await self.redis.hdel(REDIS_KEY, browser_id)
        self._registered.pop(browser_id, None)
        logger.info(f"Unregistered browser '{browser_id}' from Redis")

    async def unregister_all(self):
        if not self.redis:
            return
        for browser_id in list(self._registered.keys()):
            try:
                await self.redis.hdel(REDIS_KEY, browser_id)
            except Exception as e:
                logger.warning(f"Failed to unregister '{browser_id}': {e}")
        self._registered.clear()
        logger.info("Cleared all instance browser entries from Redis")

    async def get_desired_state(self, browser_id: str) -> Optional[dict]:
        if not self.redis:
            return None
        try:
            val = await self.redis.hget(REDIS_KEY_DESIRED, browser_id)
            if val:
                return json.loads(val)
        except Exception as e:
            logger.warning(f"Failed to read desired state for '{browser_id}': {e}")
        return None

    async def disconnect(self):
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._desired_state_task:
            self._desired_state_task.cancel()
            try:
                await self._desired_state_task
            except asyncio.CancelledError:
                pass
            self._desired_state_task = None

        if self.redis:
            await self.redis.aclose()
            self.redis = None
            logger.info("Disconnected from Redis")

    async def _heartbeat_loop(self):
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if not self.redis or not self._registered:
                    continue
                try:
                    pipe = self.redis.pipeline()
                    for browser_id, info in list(self._registered.items()):
                        ws_url = f"ws://{info['address']}:{info['proxy_port']}/devtools/browser/{browser_id}"
                        entry = {
                            "ws_url": ws_url,
                            "proxy_port": str(info["proxy_port"]),
                            # JSON number — see register_browser.
                            "cdp_port": int(info.get("cdp_port", 0) or 0),
                            "registered_at": str(int(time.time())),
                            "extensions": info.get("extensions", []),
                        }
                        pipe.hset(REDIS_KEY, browser_id, json.dumps(entry))
                    pipe.expire(REDIS_KEY, BROWSER_TTL_SECONDS * 3)
                    await pipe.execute()
                    logger.debug(f"Heartbeat: refreshed {len(self._registered)} browser(s) in Redis")
                except Exception as e:
                    logger.warning(f"Heartbeat failed: {e}")
        except asyncio.CancelledError:
            pass

    async def _desired_state_loop(self):
        try:
            while True:
                await asyncio.sleep(DESIRED_STATE_CHECK_INTERVAL)
                if not self.redis or not self._registered:
                    continue
                if not self._on_desired_state_change:
                    continue
                try:
                    for browser_id in list(self._registered.keys()):
                        desired = await self.get_desired_state(browser_id)
                        if desired:
                            await self._on_desired_state_change(browser_id, desired)
                except Exception as e:
                    logger.warning(f"Desired state check failed: {e}")
        except asyncio.CancelledError:
            pass


redis_browser_state = RedisBrowserState()
