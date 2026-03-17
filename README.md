# LiveLLM Browser

Dockerized Chrome browser with a FastAPI control plane for programmatic browser management, extension injection, cookie import/export, and CDP proxying.

## Quick Start

```bash
docker compose up --build
```

This starts three services:

| Service | Port | Description |
|---------|------|-------------|
| **Browser** | `9000` | Chrome instance manager (extensions, cookies, profiles) |
| **Controller** | `8000` | Playwright automation API (search, scrape, interact) |
| **noVNC** | `6901` | Visual browser access in your web browser (password: `headless`) |

On startup, the `register-browser` init container automatically:
1. Waits for both services to be ready
2. Discovers the default browser from the Browser service
3. Registers it with the Controller via CDP WebSocket

After that, the Controller is ready to use.

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

The controller connects to Chrome via CDP WebSocket. This happens automatically on `docker compose up`, but you can also do it manually:

```bash
# 1. Get the browser's CDP port from the Browser service
curl http://localhost:9000/browsers
# Returns: [{"browser_id":"default","cdp_port":52137,...}]

# 2. Register it with the controller
curl -X POST http://localhost:8000/parser/browsers \
  -H "Content-Type: application/json" \
  -d '{"browser_id": "default", "ws_url": "ws://livellm-browser:52137/devtools/browser/default"}'
```

If the browser restarts (e.g. after installing an extension), the controller **auto-reconnects** on the next request — no manual re-registration needed.

### Sessions

A session is a browser tab. Create one explicitly, or omit `X-Session-Id` to get an ad-hoc tab that closes after the request.

```bash
# Start a persistent session
curl -X POST http://localhost:8000/parser/start_session
# Returns: {"session_id": "abc-123", "browser_id": "default", ...}

# End it
curl -X DELETE http://localhost:8000/parser/end_session \
  -H "X-Session-Id: abc-123"
```

### Headers

Most endpoints accept these optional headers:

| Header | Description |
|--------|-------------|
| `X-Browser-Id` | Target browser (defaults to first connected) |
| `X-Session-Id` | Target session/tab (omit for ad-hoc) |

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
| `GET` | `/browsers` | List connected browsers |
| `POST` | `/browsers` | Register browser via CDP |
| `DELETE` | `/browsers/{id}` | Disconnect browser |
| `POST` | `/start_session` | Create a tab |
| `DELETE` | `/end_session` | Close a tab |
| `POST` | `/content` | Get page text/HTML/screenshot |
| `POST` | `/interact` | Click, type, scroll on a page |
| `POST` | `/attribute` | Extract data with CSS/XPath selectors |
| `POST` | `/search` | Google web search |
| `POST` | `/search_news` | Google news search |
| `POST` | `/search_images` | Google image search |
| `POST` | `/search_videos` | Google video search |
