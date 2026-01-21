# LiveLLM Browser

A headless browser automation API running in Docker with VNC access. Control Chrome programmatically via REST API while watching the browser in real-time through VNC.

Built with FastAPI, Patchright (undetectable Playwright fork), and runs in a Docker container with XFCE desktop environment.

## Features

- **REST API Control** - Navigate, click, scroll, fill forms, extract content
- **Multi-Browser Support** - Create multiple isolated browser instances with persistent profiles
- **Undetectable Automation** - Uses Patchright with native Playwright locators
- **VNC Access** - Watch browser actions in real-time via VNC or noVNC web interface
- **Session Management** - Multiple isolated browser sessions via `X-Session-Id` header
- **Selector Actions** - CSS/XPath selectors with chainable actions (html, text, click, fill, remove, attribute)
- **Page Interactions** - Screenshot, scroll, mouse move, click at coordinates

## Quick Start

### Docker Compose (Recommended)

```bash
docker compose up -d
```

Access points:
- **API**: http://localhost:8000
- **API Docs**: http://localhost:8000/docs
- **noVNC Web**: http://localhost:6901 (password: `headless`)
- **VNC**: localhost:5901 (password: `headless`)

### Local Development

```bash
# Install dependencies
uv sync

# Install Chrome
uv run patchright install chrome

# Run server
uv run main.py
```

## API Reference

### Browser Management

Browsers are isolated Chrome instances with their own profile data. A default browser is always available.

#### List Browsers
```bash
GET /browsers
```
Returns all active browsers with their IDs, profile paths, and session counts.

#### Create Browser
```bash
POST /browsers
```
```json
{
  "profile_dir": "/path/to/profile"
}
```
- `profile_dir` - Optional. If provided, becomes the `browser_id` and stores profile there
- If omitted, generates UUID as `browser_id` and stores profile in `./profiles/{uuid}`

**Examples:**
```bash
# Create browser with custom profile directory
curl -X POST http://localhost:8000/browsers \
  -H "Content-Type: application/json" \
  -d '{"profile_dir": "/data/my-profile"}'
# Response: {"browser_id": "/data/my-profile", "profile_path": "/data/my-profile", "session_count": 0}

# Create browser with auto-generated UUID
curl -X POST http://localhost:8000/browsers
# Response: {"browser_id": "a1b2c3d4-...", "profile_path": "./profiles/a1b2c3d4-...", "session_count": 0}
```

#### Delete Browser
```bash
DELETE /browsers/{browser_id}
```
Closes browser and all its sessions. Cannot delete the default browser.

### Session Management

Sessions are browser tabs/pages within a browser. Use `X-Session-Id` header to target specific sessions.

#### Start Session
```bash
POST /start_session
```
```json
{
  "browser_id": "/data/my-profile"
}
```
- `browser_id` - Optional. Can also use `X-Browser-Id` header. Defaults to default browser.

Returns a `session_id` to use in subsequent requests.

#### End Session
```bash
DELETE /end_session
Header: X-Session-Id: <session_id>
Header: X-Browser-Id: <browser_id>  (optional)
```

### Using Headers

All endpoints accept these headers:
- `X-Browser-Id` - Target a specific browser (defaults to default browser)
- `X-Session-Id` - Target a specific session/tab (auto-creates if not exists)

```bash
# Use specific browser and session
curl -X POST http://localhost:8000/content \
  -H "X-Browser-Id: /data/my-profile" \
  -H "X-Session-Id: abc-123" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'
```

### Content Endpoints

#### Get Page Content
```bash
POST /content
```
```json
{
  "url": "https://example.com",
  "return_html": true,
  "wait_until": "commit",
  "idle": 1.5
}
```
- `url` - Optional. If omitted, uses current page
- `return_html` - `true` for HTML, `false` for text only
- `wait_until` - `"commit"`, `"domcontentloaded"`, `"load"`, or `"networkidle"`

#### Execute Selectors
```bash
POST /selectors
```
```json
{
  "url": "https://example.com",
  "selectors": [
    {"name": "title", "type": "css", "value": "h1", "actions": [{"action": "text"}]},
    {"name": "links", "type": "css", "value": "a", "actions": [{"action": "attribute", "name": "href"}]}
  ]
}
```

### Selector Actions

| Action | Description | Parameters |
|--------|-------------|------------|
| `html` | Get outer HTML (default) | - |
| `text` | Get text content | - |
| `click` | Click element(s) | `nth`: 0=first, -1=last, null=all |
| `fill` | Fill input field(s) | `value`, `nth` |
| `attribute` | Get attribute value | `name` (attribute name) |
| `remove` | Remove element(s) from DOM | `nth`: 0=first, -1=last, null=all |

**Examples:**

```json
// Get text from all h1 elements
{"name": "titles", "type": "css", "value": "h1", "actions": [{"action": "text"}]}

// Click the first button
{"name": "btn", "type": "css", "value": "button", "actions": [{"action": "click"}]}

// Click all buttons
{"name": "btns", "type": "css", "value": "button", "actions": [{"action": "click", "nth": null}]}

// Fill search input
{"name": "search", "type": "css", "value": "input[name='q']", "actions": [{"action": "fill", "value": "hello"}]}

// Get href attributes
{"name": "links", "type": "css", "value": "a", "actions": [{"action": "attribute", "name": "href"}]}

// XPath: Get href directly
{"name": "links", "type": "xml", "value": "//a/@href"}

// Remove all ads
{"name": "ads", "type": "css", "value": ".advertisement", "actions": [{"action": "remove", "nth": null}]}

// Chain actions: get text then click
{"name": "menu", "type": "css", "value": ".menu-item", "actions": [{"action": "text"}, {"action": "click"}]}
```

### Page Interactions

#### Interact Endpoint
```bash
POST /interact
```

Unified endpoint for screenshots, scrolling, mouse movements, and clicks.

**Actions:**

| Action | Description | Parameters |
|--------|-------------|------------|
| `screenshot` | Take screenshot | `full_page`: boolean |
| `scroll` | Scroll page | `x`, `y` (delta) |
| `move` | Move mouse | `x`, `y`, `steps` |
| `mouse_click` | Click at coordinates | `x`, `y`, `button`, `click_count`, `delay` |
| `idle` | Wait/sleep | `duration` (seconds) |
| `html` | Get page HTML | - |
| `text` | Get page text | - |

**Examples:**

```json
// Screenshot current page
{"actions": [{"action": "screenshot"}]}

// Full page screenshot of URL
{"url": "https://example.com", "actions": [{"action": "screenshot", "full_page": true}]}

// Scroll down 500px
{"actions": [{"action": "scroll", "y": 500}]}

// Click at coordinates
{"actions": [{"action": "mouse_click", "x": 100, "y": 200}]}

// Right-click
{"actions": [{"action": "mouse_click", "x": 100, "y": 200, "button": "right"}]}

// Double-click
{"actions": [{"action": "mouse_click", "x": 100, "y": 200, "click_count": 2}]}

// Move mouse smoothly
{"actions": [{"action": "move", "x": 300, "y": 400, "steps": 20}]}

// Wait 2 seconds
{"actions": [{"action": "idle", "duration": 2}]}

// Get page HTML
{"actions": [{"action": "html"}]}

// Get page text
{"actions": [{"action": "text"}]}

// Navigate, scroll, then get text
{
  "url": "https://example.com",
  "actions": [
    {"action": "scroll", "y": 500},
    {"action": "text"}
  ]
}

// Chain: click, wait, screenshot
{
  "actions": [
    {"action": "mouse_click", "x": 100, "y": 200},
    {"action": "idle", "duration": 2},
    {"action": "screenshot"}
  ]
}
```

### Google Search

```bash
POST /search
```
```json
{
  "query": "fastapi tutorial",
  "count": 10
}
```

Returns search results with `link`, `title`, and `snippet`.

## Complete Workflow Example

```bash
# 1. Create a browser with persistent profile
curl -X POST http://localhost:8000/browsers \
  -H "Content-Type: application/json" \
  -d '{"profile_dir": "/data/work-profile"}'
# Returns: {"browser_id": "/data/work-profile", ...}

# 2. Start a session in that browser
curl -X POST http://localhost:8000/start_session \
  -H "X-Browser-Id: /data/work-profile"
# Returns: {"session_id": "abc-123", "browser_id": "/data/work-profile", ...}

# 3. Navigate to a page
curl -X POST http://localhost:8000/content \
  -H "X-Browser-Id: /data/work-profile" \
  -H "X-Session-Id: abc-123" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'

# 4. Execute actions on same page (no URL needed)
curl -X POST http://localhost:8000/selectors \
  -H "X-Browser-Id: /data/work-profile" \
  -H "X-Session-Id: abc-123" \
  -H "Content-Type: application/json" \
  -d '{"selectors": [{"name": "title", "type": "css", "value": "h1", "actions": [{"action": "text"}]}]}'

# 5. Take screenshot of current state
curl -X POST http://localhost:8000/interact \
  -H "X-Browser-Id: /data/work-profile" \
  -H "X-Session-Id: abc-123" \
  -H "Content-Type: application/json" \
  -d '{"actions": [{"action": "screenshot"}]}' \
  --output screenshot.png

# 6. End session when done
curl -X DELETE http://localhost:8000/end_session \
  -H "X-Browser-Id: /data/work-profile" \
  -H "X-Session-Id: abc-123"

# 7. Close browser (optional - keeps profile for next time)
curl -X DELETE "http://localhost:8000/browsers/%2Fdata%2Fwork-profile"
```

## Using Default Browser

For simple use cases, you don't need to manage browsers explicitly:

```bash
# Start session in default browser
curl -X POST http://localhost:8000/start_session
# Returns: {"session_id": "abc-123", ...}

# Use session
curl -X POST http://localhost:8000/content \
  -H "X-Session-Id: abc-123" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'

# End session
curl -X DELETE http://localhost:8000/end_session \
  -H "X-Session-Id: abc-123"
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `VNC_PW` | `headless` | VNC password |
| `VNC_RESOLUTION` | `1920x1080` | Screen resolution |
| `DISPLAY` | `:1` | X display number |

### Kubernetes / Helm

A Helm chart is available in the `livellm-browser-chart` directory. It creates a single PVC for browser profiles at `/controller/profiles`.

```bash
helm install livellm-browser ./livellm-browser-chart
```

## Tech Stack

- **FastAPI** - REST API framework
- **Patchright** - Undetectable Playwright fork
- **Docker** - Containerization
- **XFCE** - Desktop environment
- **TigerVNC** - VNC server
- **noVNC** - Web-based VNC client
- **UV** - Python package manager

## License

MIT
