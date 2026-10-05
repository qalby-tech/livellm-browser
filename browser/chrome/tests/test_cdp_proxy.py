"""The public CDP port: what reaches Chrome, what is refused before it, and
the second request on one connection. A fake Chrome records each head."""
import asyncio
import base64
import os

import pytest

from core.cdp_proxy import CDPProxy, parse_head

WS_ENDPOINT = "/devtools/browser/0b1c2d3e-live"
VERSION_BODY = b'{"Browser":"Chrome/154.0.0.0","webSocketDebuggerUrl":"ws://127.0.0.1:41234/devtools/browser/x"}'


class FakeChrome:
    """Answers an upgrade with 101 and then echoes; any other request with a
    200 and Content-Length, keeping the connection open (as Chrome's HTTP
    server does) and recording every further request on it."""

    def __init__(self):
        self.heads = []
        self.server = None
        self.port = None
        self.refuse_upgrade = False

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def handle(self, r, w):
        while True:
            try:
                head = await r.readuntil(b"\r\n\r\n")
            except (asyncio.IncompleteReadError, ConnectionError):
                break
            text = head.decode("latin-1")
            self.heads.append(text)
            if "upgrade: websocket" in text.lower():
                if self.refuse_upgrade:
                    body = b"Rejected an incoming WebSocket connection"
                    w.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
                    await w.drain()
                    continue
                w.write(b"HTTP/1.1 101 WebSocket Protocol Handshake\r\nUpgrade: WebSocket\r\nConnection: Upgrade\r\n\r\n")
                await w.drain()
                while True:
                    data = await r.read(65536)
                    if not data:
                        break
                    w.write(data)
                    await w.drain()
                break
            w.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=UTF-8\r\nContent-Length: %d\r\n\r\n%s"
                    % (len(VERSION_BODY), VERSION_BODY))
            await w.drain()
        w.close()

    async def stop(self):
        self.server.close()


def upgrade(path="/devtools/browser/default", extra=""):
    key = base64.b64encode(os.urandom(16)).decode()
    return (f"GET {path} HTTP/1.1\r\nHost: b1-cdp-x.apps.example\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n{extra}\r\n").encode()


async def send(port, data):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(data)
    await w.drain()
    line = (await asyncio.wait_for(r.readline(), timeout=10)).decode()
    return line.strip(), r, w


async def rest_of(r):
    return await asyncio.wait_for(r.read(), timeout=10)


@pytest.fixture
async def world():
    chrome = await FakeChrome().start()
    proxy = CDPProxy(0, chrome.port, WS_ENDPOINT, connect_retries=1, connect_retry_delay=0, host="127.0.0.1")
    await proxy.start()
    proxy.bind_port = proxy.server.sockets[0].getsockname()[1]
    yield proxy, chrome
    await proxy.stop()
    await chrome.stop()


async def test_an_upgrade_without_origin_reaches_chrome_rewritten(world):
    proxy, chrome = world
    line, r, w = await send(proxy.bind_port, upgrade("/devtools/browser/default?token=llt_x"))
    assert line.startswith("HTTP/1.1 101"), line
    await r.readuntil(b"\r\n\r\n")
    w.write(b"\x81\x02hi")
    await w.drain()
    assert await asyncio.wait_for(r.readexactly(4), timeout=5) == b"\x81\x02hi"
    w.close()
    head = chrome.heads[0]
    assert head.startswith(f"GET {WS_ENDPOINT} HTTP/1.1\r\n"), head  # the stable path, query and all, mapped
    assert f"Host: 127.0.0.1:{chrome.port}\r\n" in head
    assert "b1-cdp-x.apps.example" not in head


@pytest.mark.parametrize("origin", ["http://evil.example", "http://localhost:6901", "null", "http://127.0.0.1:9222"])
async def test_any_origin_is_refused_before_chrome(world, origin):
    proxy, chrome = world
    line, r, w = await send(proxy.bind_port, upgrade(extra=f"Origin: {origin}\r\n"))
    assert line == "HTTP/1.1 403 Forbidden"
    w.close()
    assert chrome.heads == []


async def test_origin_in_any_case_and_behind_9kb_of_headers_is_refused(world):
    proxy, chrome = world
    pad = "".join(f"X-Pad-{i}: {'a' * 990}\r\n" for i in range(9))
    line, _, w = await send(proxy.bind_port, upgrade(extra=pad + "oRiGiN: http://evil.example\r\n"))
    assert line == "HTTP/1.1 403 Forbidden"
    w.close()
    # the same 9 KB without an Origin goes through: the whole head was read
    line, _, w = await send(proxy.bind_port, upgrade(extra=pad))
    assert line.startswith("HTTP/1.1 101"), line
    w.close()
    assert len(chrome.heads) == 1 and "oRiGiN" not in chrome.heads[0]


async def test_a_head_over_16_kib_is_431(world):
    proxy, chrome = world
    pad = "".join(f"X-Pad-{i}: {'a' * 990}\r\n" for i in range(17))
    line, _, w = await send(proxy.bind_port, upgrade(extra=pad + "Origin: http://evil.example\r\n"))
    assert line == "HTTP/1.1 431 Request Header Fields Too Large"
    w.close()
    assert chrome.heads == []


async def test_a_malformed_head_is_400(world):
    proxy, chrome = world
    line, _, w = await send(proxy.bind_port, b"GET /devtools/browser/default HTTP/1.1\r\n Origin: folded\r\n\r\n")
    assert line == "HTTP/1.1 400 Bad Request"
    w.close()
    assert chrome.heads == []


async def test_an_http_request_is_answered_once_and_the_connection_closes(world):
    """Chrome keeps an HTTP connection open; the proxy passes one answer on and
    closes, so a second request (an Origin one, here) never reaches Chrome."""
    proxy, chrome = world
    first = b"GET /json/version HTTP/1.1\r\nHost: x\r\n\r\n"
    second = b"GET /json/list HTTP/1.1\r\nHost: x\r\nOrigin: http://evil.example\r\n\r\n"
    line, r, w = await send(proxy.bind_port, first + second)
    assert line == "HTTP/1.1 200 OK"
    rest = await rest_of(r)
    assert rest.endswith(VERSION_BODY)
    assert b"Connection: close\r\n" in rest and f"Content-Length: {len(VERSION_BODY)}".encode() in rest
    w.close()
    assert [h.split("\r\n", 1)[0] for h in chrome.heads] == ["GET /json/version HTTP/1.1"]


async def test_chromes_own_refusal_is_passed_on(world):
    proxy, chrome = world
    chrome.refuse_upgrade = True
    line, r, w = await send(proxy.bind_port, upgrade())
    assert line == "HTTP/1.1 403 Forbidden"
    assert (await rest_of(r)).endswith(b"Rejected an incoming WebSocket connection")
    w.close()


async def test_no_chrome_is_502(world):
    proxy, chrome = world
    proxy.retarget(1, WS_ENDPOINT)  # nothing listens there
    line, _, w = await send(proxy.bind_port, upgrade())
    assert line == "HTTP/1.1 502 Bad Gateway"
    w.close()
    assert chrome.heads == []


def test_parse_head():
    assert parse_head(b"GET / HTTP/1.1\r\nHost: a\r\nOrigin: b") == ("GET", "/", "HTTP/1.1", [("Host", "a"), ("Origin", "b")])
    assert parse_head(b"GET / HTTP/2\r\nHost: a") is None
    assert parse_head(b"GET /\r\nHost: a") is None
    assert parse_head(b"GET / HTTP/1.1\r\nno-colon") is None
