"""Browser routing at the edge of the app (a pure ASGI middleware).

Pinning a browser by path
-------------------------
``/browsers/<name>/<path>`` is the same call as ``<path>`` sent with
``X-Browser-Id: <name>``: the prefix is cut off and the header added before
routing, so every endpoint takes the path form with no route of its own.
Both forms are accepted with or without the ``/parser`` root path.

What keeps this apart from the management routes: those are ``/browsers``
and ``/browsers/<name>``, never more than one segment after ``/browsers``,
while a pinned call always has something after the name. So ``GET /browsers``,
``POST /browsers``, ``GET /browsers/agent-2`` and ``DELETE /browsers/agent-2``
are management, and ``DELETE /browsers/agent-2/end_session`` is end_session
on agent-2. A route added under ``/browsers/<name>/...`` would never be
reached. The prefix is taken once. A path name and an ``X-Browser-Id`` that
disagree answer 400.

The answering browser
---------------------
Every response for which a browser was chosen carries ``X-Browser-Id`` naming
it. The resolver (core.dependencies) records the browser in ``scope["state"]``
and hands over the release of its in-flight count, which happens here once
the response has been sent.
"""
import re
from urllib.parse import quote

from starlette.responses import JSONResponse

_PINNED = re.compile(r"^/browsers/([^/]+)(/.+)$")
_HEADER = b"x-browser-id"


class BrowserRouting:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        scope = dict(scope)
        state = scope.setdefault("state", {})

        error = _pin_from_path(scope)
        if error:
            await JSONResponse({"detail": error}, status_code=400)(scope, receive, send)
            return

        async def send_with_browser(message):
            if message["type"] == "http.response.start":
                bid = state.get("browser_id")
                if bid:
                    headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != _HEADER]
                    headers.append((_HEADER, bid.encode("latin-1", "replace")))
                    message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_browser)
        finally:
            release = state.pop("release_browser", None)
            if release is not None:
                release()


def _pin_from_path(scope) -> str:
    """Rewrite a /browsers/<name>/<path> call in place; returns an error or ''."""
    root = scope.get("root_path", "") or ""
    path = scope["path"]
    prefix = ""
    if root and path.startswith(root + "/"):
        prefix, path = root, path[len(root):]
    m = _PINNED.match(path)
    if not m:
        return ""
    name, rest = m.group(1), m.group(2)

    try:
        value = name.encode("latin-1")
    except UnicodeEncodeError:
        return f"'{name}' is not a browser name."
    headers = list(scope.get("headers", []))
    sent = [v for k, v in headers if k.lower() == _HEADER]
    if any(v != value for v in sent):
        other = sent[0].decode("latin-1")
        return f"The path names browser '{name}' but X-Browser-Id names '{other}'."
    if not sent:
        headers.append((_HEADER, value))

    scope["path"] = prefix + rest
    scope["raw_path"] = quote(scope["path"]).encode("ascii")
    scope["headers"] = headers
    return ""
