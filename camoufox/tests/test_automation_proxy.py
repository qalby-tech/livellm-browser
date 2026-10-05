"""The public automation address: what it admits, what it refuses, and the
restart in between. A fake upstream records what reaches the server."""
import asyncio
import base64
import os

import pytest

from core.automation_proxy import AutomationProxy, parse_head


class Upstream:
    """Stands for Playwright's server: records each request head, answers 101,
    then echoes."""

    def __init__(self):
        self.heads = []
        self.server = None
        self.port = None

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def handle(self, r, w):
        head = await r.readuntil(b"\r\n\r\n")
        self.heads.append(head.decode("latin-1"))
        w.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        await w.drain()
        while True:
            data = await r.read(65536)
            if not data:
                break
            w.write(data)
            await w.drain()
        w.close()

    async def stop(self):
        self.server.close()


def upgrade(path="/playwright/default", extra="", method="GET", upgrade_hdr=True):
    key = base64.b64encode(os.urandom(16)).decode()
    up = "Upgrade: websocket\r\nConnection: Upgrade\r\n" if upgrade_hdr else ""
    return (f"{method} {path} HTTP/1.1\r\nHost: b1-cdp-x.apps.example\r\n{up}"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: Playwright/1.62.0\r\n{extra}\r\n").encode()


async def send(port, data, read=True):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(data)
    await w.drain()
    line = (await asyncio.wait_for(r.readline(), timeout=10)).decode() if read else ""
    return line.strip(), r, w


@pytest.fixture
async def world():
    up = await Upstream().start()
    proxy = AutomationProxy(0, "/playwright/default", host="127.0.0.1", wait_target=1.0)
    await proxy.start()
    proxy.bind_port = proxy._server.sockets[0].getsockname()[1]
    proxy.retarget(up.port, "/0123456789abcdef0123456789abcdef")
    yield proxy, up
    await proxy.stop()
    await up.stop()


async def test_a_playwright_upgrade_reaches_the_live_server(world):
    proxy, up = world
    line, r, w = await send(proxy.bind_port, upgrade("/playwright/default?token=llt_secret"))
    assert line == "HTTP/1.1 101 Switching Protocols"
    await r.readuntil(b"\r\n\r\n")
    head = up.heads[-1]
    first = head.split("\r\n")[0]
    assert first == "GET /0123456789abcdef0123456789abcdef HTTP/1.1"  # path rewritten, query gone
    assert "llt_secret" not in head
    assert f"Host: 127.0.0.1:{up.port}\r\n" in head and "apps.example" not in head
    assert "User-Agent: Playwright/1.62.0" in head
    assert proxy.clients == 1 and proxy.idle_for() == 0
    w.write(b"ping-bytes")
    await w.drain()
    assert await r.readexactly(10) == b"ping-bytes"
    w.close()
    for _ in range(50):
        if proxy.clients == 0:
            break
        await asyncio.sleep(0.02)
    assert proxy.clients == 0 and proxy.idle_for() >= 0


@pytest.mark.parametrize("data,status", [
    (upgrade(extra="Origin: http://localhost:6901\r\n"), "403"),
    (upgrade(extra="origin: https://evil.example\r\n"), "403"),
    (upgrade("/playwright/default", extra="X-Pad: " + "a" * 9000 + "\r\nOrigin: null\r\n"), "403"),  # Origin past the first 8 KiB
    (upgrade("/devtools/browser/default"), "404"),
    (upgrade("/playwright/default/x"), "404"),
    (upgrade("/"), "404"),
    (upgrade(upgrade_hdr=False), "400"),
    (upgrade(method="POST"), "400"),
    (b"GET /playwright/default HTTP/1.1\r\nX: " + b"a" * 17000 + b"\r\n\r\n", "431"),
    (b"garbage\r\n\r\n", "400"),
])
async def test_refusals(world, data, status):
    proxy, up = world
    line, r, w = await send(proxy.bind_port, data)
    assert line.split(" ")[1] == status, line
    assert up.heads == []  # nothing reached the server
    w.close()


async def test_a_restart_holds_new_connections_then_503(world):
    proxy, up = world
    proxy.untarget()
    line, r, w = await send(proxy.bind_port, upgrade())
    assert line.startswith("HTTP/1.1 503")  # wait_target = 1 s
    w.close()

    proxy.untarget()

    async def later():
        await asyncio.sleep(0.3)
        proxy.retarget(up.port, "/ffff")

    asyncio.ensure_future(later())
    line, r, w = await send(proxy.bind_port, upgrade())
    assert line == "HTTP/1.1 101 Switching Protocols"
    assert up.heads[-1].startswith("GET /ffff HTTP/1.1")
    w.close()


def test_parse_head():
    assert parse_head(b"GET / HTTP/1.1\r\nA: b\r\nC:d") == ("GET", "/", "HTTP/1.1", [("A", "b"), ("C", "d")])
    assert parse_head(b"GET / HTTP/1.1\r\n folded") is None
    assert parse_head(b"GET /") is None
