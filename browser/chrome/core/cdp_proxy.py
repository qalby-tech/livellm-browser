"""The browser's public CDP address: 0.0.0.0:<CDP_PORT> (9222) -> Chrome.

Chrome's own DevTools port is an ephemeral loopback port that changes with
every launch; this proxy keeps one stable port in front of it and survives
browser restarts (``retarget``).

For every connection it reads the whole request head first (up to 16 KiB,
else 431), and before anything reaches Chrome:

- a request that carries an Origin header is refused (403). A web page always
  sends Origin on a WebSocket (and on cross-origin fetches); automation
  clients (Playwright, Puppeteer, CDP libraries) send none. Chrome itself runs
  without --remote-allow-origins, so it refuses such an upgrade too;
- the stable path /devtools/browser/<anything> is rewritten to the browser's
  current ws endpoint (so an address stays valid across restarts);
- Host is rewritten to 127.0.0.1:<chrome port> (Chrome accepts only an IP or
  localhost there).

Bytes are tunnelled only once Chrome answered 101 (a WebSocket). Any other
answer (Chrome's /json HTTP endpoints, a refusal) is passed on with its body
and the connection closes, so no second request on one connection ever
reaches Chrome unchecked.
"""
import asyncio
import logging
from typing import Optional

from core.const import STABLE_WS_PREFIX

logger = logging.getLogger(__name__)

MAX_HEAD = 16 * 1024
# The body of an answer that is not 101 (Chrome's /json lists), passed on whole
# up to this.
MAX_ANSWER_BODY = 1024 * 1024
HEAD_TIMEOUT = 30.0
REASONS = {400: "Bad Request", 403: "Forbidden", 431: "Request Header Fields Too Large",
           502: "Bad Gateway"}


def parse_head(head: bytes):
    """(method, target, version, [(name, value)]) of a request head (without
    its final CRLFCRLF), or None when it is malformed."""
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
    except UnicodeDecodeError:
        return None
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/1.") or not parts[1].isdigit():
        return None
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers.setdefault(name.strip().lower(), value.strip())
    return int(parts[1]), headers


class CDPProxy:
    """A TCP proxy from a stable public port to Chrome's CDP port (see the
    module docstring). The bind port stays the same across browser restarts;
    ``retarget`` only updates the Chrome port and ws endpoint."""

    def __init__(
        self,
        bind_port: int,
        target_port: int,
        ws_endpoint: str = "",
        connect_retries: int = 5,
        connect_retry_delay: float = 1.0,
        host: str = "0.0.0.0",
        max_head: int = MAX_HEAD,
    ):
        self.bind_port = bind_port
        self.target_port = target_port
        self.ws_endpoint = ws_endpoint
        self.connect_retries = connect_retries
        self.connect_retry_delay = connect_retry_delay
        self.host = host
        self.max_head = max_head
        self.server = None
        self._clients = set()

    def retarget(self, target_port: int, ws_endpoint: str):
        """Update the Chrome target without restarting the proxy server."""
        self.target_port = target_port
        self.ws_endpoint = ws_endpoint
        logger.info(f"CDP Proxy on :{self.bind_port} retargeted -> 127.0.0.1:{target_port} (ws: {ws_endpoint})")

    # ── one connection ──

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._clients.add(writer)
        try:
            await self._handle(reader, writer)
        finally:
            self._clients.discard(writer)

    async def _answer(self, writer: asyncio.StreamWriter, status: int) -> None:
        try:
            writer.write(
                f"HTTP/1.1 {status} {REASONS.get(status, '')}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
            )
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def _read_head(self, reader: asyncio.StreamReader, limit: int):
        """(head with its CRLFCRLF, the bytes after it), or None when the peer
        went away first. OverflowError past ``limit``."""
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = await reader.read(4096)
            if not chunk:
                return None
            buf += chunk
            if len(buf) > limit and b"\r\n\r\n" not in buf[: limit + 4]:
                raise OverflowError
        end = buf.index(b"\r\n\r\n") + 4
        if end > limit:
            raise OverflowError
        return buf[:end], buf[end:]

    def _rewrite(self, method: str, target: str, version: str, headers) -> bytes:
        """The head Chrome gets: the stable path mapped to the live endpoint,
        and Host set to Chrome's own address."""
        if self.ws_endpoint and method == "GET" and target.startswith(STABLE_WS_PREFIX + "/") \
                and len(target) > len(STABLE_WS_PREFIX) + 1:
            target = self.ws_endpoint
        out = [f"{method} {target} {version}", f"Host: 127.0.0.1:{self.target_port}"]
        out += [f"{name}: {value}" for name, value in headers if name.lower() != "host"]
        return ("\r\n".join(out) + "\r\n\r\n").encode("latin-1")

    async def _open_chrome(self):
        """Connect to Chrome, retrying: it may still be starting up or
        momentarily away during a restart."""
        last_error: Optional[Exception] = None
        for attempt in range(1, self.connect_retries + 1):
            try:
                return await asyncio.open_connection("127.0.0.1", self.target_port)
            except Exception as exc:
                last_error = exc
                if attempt < self.connect_retries:
                    logger.debug(
                        f"Proxy connect attempt {attempt}/{self.connect_retries} "
                        f"to 127.0.0.1:{self.target_port} failed: {exc}, "
                        f"retrying in {self.connect_retry_delay}s…"
                    )
                    await asyncio.sleep(self.connect_retry_delay)
        logger.error(
            f"Proxy failed to connect to Chrome at 127.0.0.1:{self.target_port} "
            f"after {self.connect_retries} attempts: {last_error}"
        )
        return None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            got = await asyncio.wait_for(self._read_head(reader, self.max_head), timeout=HEAD_TIMEOUT)
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
        method, target, version, headers = parsed
        # A page always sends Origin; automation clients never do.
        if any(name.lower() == "origin" for name, _ in headers):
            return await self._answer(writer, 403)

        opened = await self._open_chrome()
        if opened is None:
            return await self._answer(writer, 502)
        up_reader, up_writer = opened
        # Only the head: a WebSocket client sends nothing before the 101, and
        # bytes after the head (a pipelined request) wait for that answer.
        try:
            up_writer.write(self._rewrite(method, target, version, headers))
            await up_writer.drain()
            answer = await asyncio.wait_for(self._read_head(up_reader, 64 * 1024), timeout=HEAD_TIMEOUT)
        except (OverflowError, asyncio.TimeoutError, ConnectionError, OSError):
            answer = None
        if answer is None:
            up_writer.close()
            return await self._answer(writer, 502)
        answer_head, early = answer
        status_line = answer_head.split(b"\r\n", 1)[0].split(b" ")
        if len(status_line) < 2 or status_line[1] != b"101":
            return await self._relay_once(answer_head, early, up_reader, up_writer, writer)

        writer.write(answer_head + early)
        if rest:
            up_writer.write(rest)
        await asyncio.gather(self._pipe(reader, up_writer), self._pipe(up_reader, writer), return_exceptions=True)

    async def _relay_once(self, answer: bytes, early: bytes, up_reader, up_writer, writer) -> None:
        """Chrome answered something other than 101: pass the head and its body
        (Content-Length, up to MAX_ANSWER_BODY) on with Connection: close, then
        close both sides."""
        parsed = parse_response_head(answer[:-4])
        body = early
        try:
            if parsed is not None:
                length = parsed[1].get("content-length")
                if length is not None and length.isdigit():
                    want = min(int(length), MAX_ANSWER_BODY)
                    while len(body) < want:
                        chunk = await asyncio.wait_for(up_reader.read(want - len(body)), timeout=5)
                        if not chunk:
                            break
                        body += chunk
                    body = body[:want]
                if "transfer-encoding" in parsed[1]:
                    body = b""  # a chunked body is not relayed (Chrome sends none)
            head = answer[:-4].split(b"\r\n")
            kept = [head[0]] + [
                h for h in head[1:]
                if h.split(b":", 1)[0].strip().lower()
                not in (b"connection", b"keep-alive", b"content-length", b"transfer-encoding")
            ]
            kept += [b"Content-Length: " + str(len(body)).encode(), b"Connection: close"]
            writer.write(b"\r\n".join(kept) + b"\r\n\r\n" + body)
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass
        finally:
            up_writer.close()
            writer.close()

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

    # ── the server ──

    async def start(self):
        self.server = await asyncio.start_server(self.handle_client, self.host, self.bind_port)
        logger.info(f"CDP Proxy started: {self.host}:{self.bind_port} -> 127.0.0.1:{self.target_port}")

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()

        for w in list(self._clients):
            try:
                w.close()
            except Exception:
                pass
        self._clients.clear()
