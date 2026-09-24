# LiveLLM Browser

Dockerized Chrome browser with a FastAPI control plane for programmatic browser management, extension injection, cookie import/export, and CDP proxying.

## Quick Start

```bash
docker compose up --build
```

This starts two services:

| Service | Port | Description |
|---------|------|-------------|
| **Browser** | `9000` (launcher), `9222` (CDP), `6901` (noVNC) | Chrome instance manager (extensions, cookies, profiles); noVNC password `headless` |
| **Controller** | `8000` | Playwright automation API (search, scrape, interact) |

The browser pins its CDP proxy to a fixed port (`CDP_PORT=9222`), and the
controller is handed a static registry file (compose `configs.browsers_json`)
pointing at `ws://livellm-browser:9222/...`. The controller warm-connects on
startup and lazily on first request — no registration step.

## How It Works

```
You / Your App
     │
     ├──► Browser Service (:9000)    Manage Chrome instances, extensions, cookies
     │         │
     │         ├─ Chrome (profile: default)
     │         │    └─ CDP Proxy (:stable_port) ◄── survives restarts
     │         │
     │         └─ Chrome (profile: custom)
     │              └─ CDP Proxy (:stable_port)
     │
     └──► Controller (:8000)         Automate pages (search, scrape, click, screenshot)
               │
               └─ Playwright ──► CDP Proxy ──► Chrome
                  (auto-reconnects on browser restart)
```

---

## Browser Service API (port 9000)

Manages Chrome instances, profiles, extensions, and cookies.

### Browsers

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/browsers` | List all running browsers |
| `POST` | `/browsers` | Create a new browser |
| `DELETE` | `/browsers/{id}` | Close a browser |
| `POST` | `/browsers/{id}/restart` | Restart a browser (preserves profile) |

**Create a browser with extensions, cookies, and proxy:**

```bash
curl -X POST http://localhost:9000/browsers \
  -H "Content-Type: application/json" \
  -d '{
    "profile_uid": "my_profile",
    "extensions": ["dknlfmjaanfblgfdfebhijalfmhmjjjo"],
    "cookies": [
      {"name": "session", "value": "abc123", "domain": ".example.com", "path": "/"}
    ],
    "proxy": {
      "server": "http://proxy:8080",
      "username": "user",
      "password": "pass"
    }
  }'
```

| Field | Type | Description |
|-------|------|-------------|
| `profile_uid` | string | Persistent profile name. Omit for ephemeral (lost on close). |
| `extensions` | string[] | Chrome Web Store extension IDs to pre-install. |
| `cookies` | object[] | Cookies to load on startup. |
| `proxy` | object | HTTP proxy config (`server`, `username`, `password`, `bypass`). |

### Extensions

Extensions are auto-downloaded from the Chrome Web Store by ID, unpacked, and injected into Chrome's profile. All mutating operations automatically restart the browser.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/browsers/{id}/extensions` | List extensions (includes `enabled` state) |
| `POST` | `/browsers/{id}/extensions` | Install extensions (auto-restarts) |
| `DELETE` | `/browsers/{id}/extensions/{ext_id}` | Remove an extension (auto-restarts) |
| `PATCH` | `/browsers/{id}/extensions/{ext_id}` | Enable/disable an extension (auto-restarts) |

```bash
# Install NopeCHA captcha solver on the default browser
curl -X POST http://localhost:9000/browsers/default/extensions \
  -H "Content-Type: application/json" \
  -d '{"extensions": ["dknlfmjaanfblgfdfebhijalfmhmjjjo"]}'

# Disable it (keeps files, just turns it off)
curl -X PATCH http://localhost:9000/browsers/default/extensions/dknlfmjaanfblgfdfebhijalfmhmjjjo \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'

# Re-enable it
curl -X PATCH http://localhost:9000/browsers/default/extensions/dknlfmjaanfblgfdfebhijalfmhmjjjo \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'

# Remove it completely
curl -X DELETE http://localhost:9000/browsers/default/extensions/dknlfmjaanfblgfdfebhijalfmhmjjjo
```

### Cookies

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/browsers/{id}/cookies` | Export all cookies as JSON |
| `POST` | `/browsers/{id}/cookies` | Import cookies from JSON |

```bash
# Export
curl http://localhost:9000/browsers/default/cookies > cookies.json

# Import
curl -X POST http://localhost:9000/browsers/default/cookies \
  -H "Content-Type: application/json" \
  -d @cookies.json
```

---

## Controller API (port 8000)

All controller endpoints are prefixed with `/parser` (e.g. `http://localhost:8000/parser/browsers`).

### Connecting a Browser

The controller connects to Chrome via CDP WebSocket. With a registry (`BROWSERS_CONFIG` set, as in compose and on Kubernetes) the registry alone decides which browsers it drives: a browser added to the file is used from the next call, one removed from it is disconnected with its sessions, and `POST /browsers` / `DELETE /browsers/{id}` answer 403. Without `BROWSERS_CONFIG` you register browsers by hand:

```bash
# 1. Get the browser's CDP port from the Browser service
curl http://localhost:9000/browsers
# Returns: [{"browser_id":"default","cdp_port":9222,...}]

# 2. Register it with the controller (only when BROWSERS_CONFIG is unset)
curl -X POST http://localhost:8000/parser/browsers \
  -H "Content-Type: application/json" \
  -d '{"browser_id": "default", "ws_url": "ws://livellm-browser:9222/devtools/browser/default"}'
```

`GET /browsers` lists every browser a call can land on, without addresses:

```json
[{"browser_id": "default", "connected": true, "healthy": true, "open_tabs": 2, "session_count": 1}]
```

`healthy` turns false for 30 seconds after a browser could not be reached; calls that name no browser skip it meanwhile.

If the browser restarts (e.g. after installing an extension), the controller **auto-reconnects** on the next request — no manual re-registration needed.

> **A file-backed registry is the source of truth.** Each browser is its own pod fronted by a stable Service, so its CDP `ws_url` is deterministic and never drifts — the in-pod CDP proxy keeps a **fixed port** (`CDP_PORT`, default 9222) and rewrites the ws path across Chrome restarts, while the Service keeps a stable DNS name across pod restarts. The operator writes the namespace's browsers into a ConfigMap (`{"browsers": {"<id>": "ws://<svc>:9222/devtools/browser/<id>"}}`) that the controller mounts at `BROWSERS_CONFIG` (default `/etc/livellm/browsers.json`) and re-reads on demand. The controller resolves `X-Browser-Id` against this map and reconnects only when a live connection dies.

### Choosing a browser

One controller drives many browsers. Each call lands on one of them in one of three ways:

1. **Nothing named**: the browser with the fewest open tabs (every tab, including ones a person opened, plus calls still running). Nothing waits or is refused: a browser that cannot be reached is skipped and the next one is tried, and one that takes more than a few seconds to connect is passed over until it is up; `MAX_PAGES_PER_BROWSER` only ranks a busy browser last.
2. **`X-Session-Id`**: the browser the session was started on. No other header is needed.
3. **`X-Browser-Id: <name>`**, or the path prefix `/browsers/<name>/`: that browser. `POST /parser/browsers/agent-2/content` is exactly `POST /parser/content` with `X-Browser-Id: agent-2`.

The prefix never clashes with the management routes: those are `/browsers` and `/browsers/{id}`, while a pinned call always has a path after the name.

Every response for which a browser was chosen carries `X-Browser-Id` naming it.

| Status | When |
|--------|------|
| `400` | The path names one browser and `X-Browser-Id` another, or `start_session`'s body names another |
| `404` | The named browser is not in the controller, or the session is unknown (never started, ended, or its browser was removed) |
| `409` | A named browser contradicts the session's browser |
| `502` | The named (or session's) browser cannot be reached |
| `503` | No browsers at all, or none of them can be reached |

### Sessions

A session is a browser tab. Start one with `POST /start_session`, or omit `X-Session-Id` to get an ad-hoc tab that is created for the request and closed on the way out. A session stays on its browser: later calls send `X-Session-Id` alone. If its tab was closed or its browser reconnected, the session gets a new tab on the same browser. Sessions live in the controller's memory, so a controller restart ends them all.

After extraction, every page operation issues `window.stop()` so Chrome stops streaming bytes back over CDP into the Node driver heap — important for large/heavy pages that would otherwise keep loading resources after the response was already returned.

```bash
# Start a persistent session (on a chosen browser: add -H "X-Browser-Id: default")
curl -X POST http://localhost:8000/parser/start_session
# Returns: {"session_id": "abc-123", "browser_id": "default", ...}

# Use it: X-Session-Id alone goes to its browser
curl -X POST http://localhost:8000/parser/content \
  -H "Content-Type: application/json" \
  -H "X-Session-Id: abc-123" \
  -d '{"url": "https://example.com"}'

# End it
curl -X DELETE http://localhost:8000/parser/end_session \
  -H "X-Session-Id: abc-123"
```

### Headers

Most endpoints accept these optional headers:

| Header | Description |
|--------|-------------|
| `X-Browser-Id` | Target browser (omit to use the one with the fewest open tabs, or the session's) |
| `X-Session-Id` | Target session/tab (omit for ad-hoc) |

Responses carry `X-Browser-Id` naming the browser that answered.

### Content — Get Page Text/HTML/Screenshot

```bash
# Get page text (with auto-scroll)
curl -X POST http://localhost:8000/parser/content \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "output_action": "text"}'

# Get full-page screenshot
curl -X POST http://localhost:8000/parser/content \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "output_action": "screenshot_full"}' \
  --output screenshot.png
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `url` | string | current page | URL to open |
| `output_action` | string | `"text"` | `"text"`, `"html"`, `"screenshot"`, `"screenshot_full"` |
| `wait_until` | string | `"commit"` | `"commit"`, `"domcontentloaded"`, `"load"`, `"networkidle"` |
| `idle` | number | `2` | Seconds to wait after load |
| `steps` | number | `8` | Scroll steps (0 = no scroll) |
| `step_delay` | number | `1.5` | Seconds between scroll steps |
| `step_pixels` | number | `1500` | Pixels per scroll step |

### Interact — Click, Type, Scroll

```bash
curl -X POST http://localhost:8000/parser/interact \
  -H "Content-Type: application/json" \
  -H "X-Session-Id: abc-123" \
  -d '{
    "url": "https://example.com",
    "actions": [
      {"action": "selector", "value": "input[name=q]", "do": "fill", "args": {"value": "hello"}},
      {"action": "selector", "value": "button[type=submit]", "do": "click"},
      {"action": "idle", "duration": 2}
    ],
    "output_action": "screenshot"
  }'
```

Action types: `scroll`, `scroll_to_bottom`, `move`, `mouse_click`, `idle`, `login`, `selector`.

### Attribute — Extract Data with Selectors

```bash
curl -X POST http://localhost:8000/parser/attribute \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "selectors": [
      {"name": "titles", "selector": "h2", "type": "css"},
      {"name": "links", "selector": "a", "type": "css", "attribute": "href"}
    ]
  }'
# Returns: [{"name": "titles", "values": [...]}, {"name": "links", "values": [...]}]
```

### Search — Google Search

```bash
# Web search
curl -X POST http://localhost:8000/parser/search \
  -H "Content-Type: application/json" \
  -d '{"query": "openai", "count": 5}'

# News search
curl -X POST http://localhost:8000/parser/search_news \
  -H "Content-Type: application/json" \
  -d '{"query": "AI news", "count": 5}'

# Image search
curl -X POST http://localhost:8000/parser/search_images \
  -H "Content-Type: application/json" \
  -d '{"query": "cats", "count": 10}'

# Video search
curl -X POST http://localhost:8000/parser/search_videos \
  -H "Content-Type: application/json" \
  -d '{"query": "tutorial", "count": 5}'
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `query` | string | required | Search query |
| `count` | number | `5` | Max results |
| `idle` | number | `3` | Wait time before parsing (seconds) |
| `max_pages` | number | `10` | Max pages to paginate through |

---

## Full Endpoint Summary

### Browser Service (`:9000`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/browsers` | List browsers |
| `POST` | `/browsers` | Create browser |
| `DELETE` | `/browsers/{id}` | Close browser |
| `POST` | `/browsers/{id}/restart` | Restart browser |
| `GET` | `/browsers/{id}/extensions` | List extensions |
| `POST` | `/browsers/{id}/extensions` | Install extensions |
| `PATCH` | `/browsers/{id}/extensions/{ext_id}` | Enable/disable extension |
| `DELETE` | `/browsers/{id}/extensions/{ext_id}` | Remove extension |
| `GET` | `/browsers/{id}/cookies` | Export cookies |
| `POST` | `/browsers/{id}/cookies` | Import cookies |

### Controller (`:8000/parser`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/ping` | Health check |
| `GET` | `/browsers` | List browsers with their tab counts |
| `GET` | `/browsers/{id}` | One browser |
| `POST` | `/browsers` | Register browser via CDP (no `BROWSERS_CONFIG` only) |
| `DELETE` | `/browsers/{id}` | Disconnect browser (no `BROWSERS_CONFIG` only) |
| `*` | `/browsers/{id}/<path>` | `<path>` on browser `{id}` (same as `X-Browser-Id`) |
| `POST` | `/start_session` | Create a tab |
| `DELETE` | `/end_session` | Close a tab |
| `POST` | `/content` | Get page text/HTML/screenshot |
| `POST` | `/interact` | Click, type, scroll on a page |
| `POST` | `/attribute` | Extract data with CSS/XPath selectors |
| `POST` | `/search` | Google web search |
| `POST` | `/search_news` | Google news search |
| `POST` | `/search_images` | Google image search |
| `POST` | `/search_videos` | Google video search |

---

## Running on Kubernetes

For cluster deployments, use the [livellm-browser-operator](https://github.com/XvKuoMing/livellm-browser-operator) and its Helm chart. The operator manages `Browser` and `Controller` CRs: one pod per `Browser` (a stable Service + fixed CDP port), a per-namespace `Controller`, and the browser-registry ConfigMap that wires them together. It passes desired state (extensions, cookies, proxy) to the pod as env/volume and rolls it on change. The controller pod's `NODE_OPTIONS` is auto-sized from its memory limit (`max(limit/2, limit - 2 GiB)` MiB, clamped to 512-8192); the browser pod is left alone so Chrome keeps the memory budget. Override per-CR via `spec.env`, cluster-wide via `DEFAULT_*_ENV`.
