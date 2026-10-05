"""The browser's public automation address: 0.0.0.0:<AUTOMATION_PORT>.

Playwright clients reach the browser's own Playwright server through this
proxy at the stable path /playwright/default; the server itself listens on
loopback at an ephemeral port and a random path that change with every
launch, so this proxy is the only way in, and it admits only:

- a WebSocket upgrade (GET, Upgrade: websocket, Connection: upgrade) of
  exactly /playwright/default (the query is dropped: the console's ?token=
  never reaches Playwright);
- with no Origin header. Playwright clients send none; a web page always
  does, so a page in the browser (or anywhere) can't drive it.

The whole request head is read (up to 16 KiB, else 431) before anything is
decided or forwarded. The path is rewritten to the live one and Host to the
server's. Bytes are tunnelled only once the server answered 101 (one request
per connection): any other answer (Playwright's 428 for a client of another
version, say) is passed on with its body and the connection closes, so no
second request on it ever reaches the server unchecked. During a restart a
new connection waits up to 30 s for the next server, else 503.
Connections are counted: the browser's housekeeping closes the contexts
clients left once none has been connected for a while.
"""
import asyncio
import logging
import time
from typing import Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

MAX_HEAD = 16 * 1024
MAX_REFUSAL_BODY = 64 * 1024   # the body of a server answer that is not 101
REASONS = {400: "Bad Request", 403: "Forbidden", 404: "Not Found", 431: "Request Header Fields Too Large",
           502: "Bad Gateway", 503: "Service Unavailable"}


def parse_head(head: bytes):
    """(method, target, version, [(name, value)]) of a request head, or None."""
    try:
        text = head.decode("latin-1")
    except UnicodeDecodeError:
        return None
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        return None
    headers = []
    for line in lines[1:]:
        if not line:
            continue
        if line[0] in " \t" or ":" not in line:
            return None  # folded or malformed: refused, never guessed at
        name, _, value = line.partition(":")
        headers.append((name.strip(), value.strip()))
    return parts[0], parts[1], parts[2], headers


def parse_response_head(head: bytes):
    """(status, {lowercased name: value}) of a response head, or None."""
    try:
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ", 2)
        if len(parts) < 2 or not parts[0].startswith("HTTP/1.") or not parts[1].isdigit():
            return None
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers.setdefault(name.strip().lower(), value.strip())
        return int(parts[1]), headers
    except UnicodeDecodeError:
        return None


class AutomationProxy:
    def __init__(self, bind_port: int, path: str, host: str = "0.0.0.0", wait_target: float = 30.0,
                 max_head: int = MAX_HEAD):
        self.bind_port = bind_port
        self.host = host
        self.path = path
        self.wait_target = wait_target
        self.max_head = max_head
        self._target: Optional[Tuple[int, str]] = None
        self._ready: Optional[asyncio.Event] = None
        self._server = None
        self._conns = set()
        self.clients = 0
        self._idle_since: float = time.monotonic()

    # ── target ──

    def _event(self) -> asyncio.Event:
        if self._ready is None:
            self._ready = asyncio.Event()
        return self._ready

    def retarget(self, port: int, ws_path: str) -> None:
        self._target = (port, ws_path)
        self._event().set()
        logger.info(f"Automation proxy :{self.bind_port}{self.path} -> 127.0.0.1:{port} (a new server)")

    def untarget(self) -> None:
        """No server now (a restart): new connections wait for the next."""
        self._target = None
        self._event().clear()

    def idle_for(self) -> float:
        """Seconds since the last client left (0 while one is connected)."""
        if self.clients > 0:
            return 0.0
        return time.monotonic() - self._idle_since

    # ── connections ──

    async def _answer(self, writer: asyncio.StreamWriter, status: int) -> None:
        try:
            writer.write(f"HTTP/1.1 {status} {REASONS.get(status, '')}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def _read_head(self, reader: asyncio.StreamReader):
        """(head, bytes after it), or None when the client went away first."""
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = await reader.read(4096)
            if not chunk:
                return None
            buf += chunk
            if len(buf) > self.max_head and b"\r\n\r\n" not in buf[: self.max_head + 4]:
                raise OverflowError
        end = buf.index(b"\r\n\r\n") + 4
        if end > self.max_head:
            raise OverflowError
        return buf[:end], buf[end:]

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._conns.add(writer)
        try:
            await self._handle(reader, writer)
        finally:
            self._conns.discard(writer)

    async def _handle(self, reader, writer) -> None:
        try:
            got = await asyncio.wait_for(self._read_head(reader), timeout=30)
        except OverflowError:
            return await self._answer(writer, 431)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            writer.close()
            return
        if got is None:
            writer.close()
            return
        head, rest = got
        parsed = parse_head(head[:-4])
        if parsed is None:
            return await self._answer(writer, 400)
        method, target, _version, headers = parsed
        lower = {}
        for name, value in headers:
            lower.setdefault(name.lower(), value)
        # A page always sends Origin on a WebSocket; Playwright clients never.
        if "origin" in lower:
            return await self._answer(writer, 403)
        if urlsplit(target).path != self.path:
            return await self._answer(writer, 404)
        connection = {t.strip().lower() for t in lower.get("connection", "").split(",")}
        if method != "GET" or lower.get("upgrade", "").lower() != "websocket" or "upgrade" not in connection:
            return await self._answer(writer, 400)

        upstream = await self._connect()
        if upstream is None:
            return await self._answer(writer, 503)
        (port, ws_path), up_reader, up_writer = upstream

        out = [f"GET {ws_path} HTTP/1.1"]
        for name, value in headers:
            if name.lower() == "host":
                continue
            out.append(f"{name}: {value}")
        out.insert(1, f"Host: 127.0.0.1:{port}")
        # Only the head: a WebSocket client sends nothing before the 101, and
        # bytes after the head (a pipelined request) wait for that answer.
        up_writer.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))

        try:
            got = await asyncio.wait_for(self._read_head(up_reader), timeout=30)
        except (OverflowError, asyncio.TimeoutError, ConnectionError, OSError):
            got = None
        if got is None:
            up_writer.close()
            return await self._answer(writer, 502)
        answer, early = got
        status_line = answer.split(b"\r\n", 1)[0].split(b" ")
        if len(status_line) < 2 or status_line[1] != b"101":
            return await self._refused(answer, early, up_reader, up_writer, writer)

        writer.write(answer + early)
        if rest:
            up_writer.write(rest)
        self.clients += 1
        try:
            await asyncio.gather(self._pipe(reader, up_writer), self._pipe(up_reader, writer), return_exceptions=True)
        finally:
            self.clients -= 1
            if self.clients == 0:
                self._idle_since = time.monotonic()

    async def _refused(self, answer: bytes, early: bytes, up_reader, up_writer, writer) -> None:
        """The server answered something other than 101: pass it on (head and
        a bounded body, Connection: close), then close both sides."""
        parsed = parse_response_head(answer[:-4])
        body = early
        try:
            if parsed is not None:
                length = parsed[1].get("content-length")
                if length is not None and length.isdigit():
                    want = min(int(length), MAX_REFUSAL_BODY)
                    while len(body) < want:
                        chunk = await asyncio.wait_for(up_reader.read(want - len(body)), timeout=5)
                        if not chunk:
                            break
                        body += chunk
                    body = body[:want]
            head = answer[:-4].split(b"\r\n")
            kept = [head[0]] + [h for h in head[1:] if h.split(b":", 1)[0].strip().lower() not in (b"connection", b"keep-alive", b"content-length", b"transfer-encoding")]
            if parsed is not None and "transfer-encoding" in parsed[1]:
                body = b""  # a chunked body is not relayed
            kept += [b"Content-Length: " + str(len(body)).encode(), b"Connection: close"]
            writer.write(b"\r\n".join(kept) + b"\r\n\r\n" + body)
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass
        finally:
            up_writer.close()
            writer.close()

    async def _connect(self):
        """Open a connection to the live server, waiting out a restart."""
        deadline = time.monotonic() + self.wait_target
        while True:
            target = self._target
            if target is None:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                try:
                    await asyncio.wait_for(self._event().wait(), timeout=left)
                except asyncio.TimeoutError:
                    return None
                continue
            try:
                r, w = await asyncio.open_connection("127.0.0.1", target[0])
                return target, r, w
            except OSError:
                if time.monotonic() >= deadline:
                    return None
                await asyncio.sleep(0.5)

    @staticmethod
    async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await src.read(65536)
                if not data:
                    break
                dst.write(data)
                await dst.drain()
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            try:
                dst.close()
            except Exception:
                pass

    async def start(self) -> None:
        self._server = await asyncio.start_server(self.handle, self.host, self.bind_port)
        logger.info(f"Automation proxy listening on {self.host}:{self.bind_port}{self.path}")

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        for w in list(self._conns):
            try:
                w.close()
            except Exception:
                pass
        self._conns.clear()
