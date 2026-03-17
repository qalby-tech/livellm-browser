import asyncio
import re
import logging

logger = logging.getLogger(__name__)

class CDPProxy:
    """
    A TCP proxy that forwards connections from a stable public port to Chrome's CDP port.

    It rewrites:
    - The Host header to 127.0.0.1:<target_port> (bypass Chrome's strict check)
    - The request path to the current ws_endpoint (so stale URLs still connect after restart)

    The proxy survives browser restarts via retarget() — the bind port stays the same,
    only the Chrome target port and ws_endpoint are updated.
    """
    def __init__(self, bind_port: int, target_port: int, ws_endpoint: str = ""):
        self.bind_port = bind_port
        self.target_port = target_port
        self.ws_endpoint = ws_endpoint
        self.server = None
        self._clients = set()

    def retarget(self, target_port: int, ws_endpoint: str):
        """Update the Chrome target without restarting the proxy server."""
        self.target_port = target_port
        self.ws_endpoint = ws_endpoint
        logger.info(f"CDP Proxy on :{self.bind_port} retargeted -> 127.0.0.1:{target_port} (ws: {ws_endpoint})")

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._clients.add(writer)
        try:
            remote_reader, remote_writer = await asyncio.open_connection('127.0.0.1', self.target_port)
        except Exception as e:
            logger.error(f"Proxy failed to connect to Chrome at 127.0.0.1:{self.target_port}: {e}")
            writer.close()
            self._clients.discard(writer)
            return

        async def forward_client_to_chrome():
            try:
                data = await reader.read(8192)
                if not data:
                    return

                try:
                    text_data = data.decode('utf-8', errors='ignore')
                    if 'HTTP/' in text_data:
                        # Rewrite the request path to current ws_endpoint
                        if self.ws_endpoint:
                            text_data = re.sub(
                                r'^(GET\s+)/devtools/browser/[^\s]+',
                                rf'\g<1>{self.ws_endpoint}',
                                text_data,
                                count=1
                            )
                        # Rewrite Host header
                        new_host = f"127.0.0.1:{self.target_port}"
                        text_data = re.sub(
                            r'(?i)^(Host:\s*)[^\r\n]+',
                            rf'\g<1>{new_host}',
                            text_data,
                            flags=re.MULTILINE
                        )
                        data = text_data.encode('utf-8')
                except Exception as e:
                    logger.warning(f"Error rewriting headers: {e}")

                remote_writer.write(data)
                await remote_writer.drain()

                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    remote_writer.write(data)
                    await remote_writer.drain()
            except Exception:
                pass
            finally:
                remote_writer.close()

        async def forward_chrome_to_client():
            try:
                while True:
                    data = await remote_reader.read(65536)
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
            except Exception:
                pass
            finally:
                writer.close()

        t1 = asyncio.create_task(forward_client_to_chrome())
        t2 = asyncio.create_task(forward_chrome_to_client())

        await asyncio.gather(t1, t2, return_exceptions=True)
        self._clients.discard(writer)

    async def start(self):
        self.server = await asyncio.start_server(self.handle_client, '0.0.0.0', self.bind_port)
        logger.info(f"CDP Proxy started: 0.0.0.0:{self.bind_port} -> 127.0.0.1:{self.target_port}")

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()

        for w in list(self._clients):
            try:
                w.close()
            except:
                pass
        self._clients.clear()
