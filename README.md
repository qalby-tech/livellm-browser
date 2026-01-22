# LiveLLM Browser

A headless browser automation API running in Docker with VNC access. Control Chrome programmatically via REST API while watching the browser in real-time through VNC.

Built with FastAPI, Patchright (undetectable Playwright fork), and runs in a Docker container with XFCE desktop environment.

## Features

- **REST API Control** - Navigate, click, scroll, fill forms, extract content
- **Multi-Browser Support** - Create multiple isolated browser instances with persistent profiles
- **Proxy Support** - Configure HTTP/HTTPS/SOCKS proxy per browser instance
- **HTTP Authentication** - Set HTTP Basic Auth credentials for protected pages
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
  "profile_uid": "my-profile",
  "proxy": {
    "server": "http://myproxy.com:3128",
    "username": "proxyuser",
    "password": "proxypass",
    "bypass": "localhost,*.example.com"
  }
}
```
- `profile_uid` - Optional.
  - If provided, used as `browser_id` and stores profile in `./profiles/{uid}` (persistent).
  - If omitted, generates random UUID as `browser_id` (ephemeral/incognito).
- `proxy` - Optional. Proxy settings for the browser (only configurable at browser creation)
  - `server` - Proxy server URL (required if proxy is set)
  - `username` - Proxy auth username (optional)
  - `password` - Proxy auth password (optional)
  - `bypass` - Comma-separated hosts to bypass proxy (optional)

**Note:** The default browser does not use a proxy. Proxy settings can only be configured when creating a new browser.

**Examples:**
```bash
# Create browser with named persistent profile
curl -X POST http://localhost:8000/browsers \
  -H "Content-Type: application/json" \
  -d '{"profile_uid": "work-profile"}'
# Response: {"browser_id": "work-profile", "profile_path": "profiles/work-profile", "session_count": 0}

# Create ephemeral browser (incognito)
curl -X POST http://localhost:8000/browsers
# Response: {"browser_id": "a1b2c3d4-...", "profile_path": null, "session_count": 0}


# Create browser with proxy
curl -X POST http://localhost:8000/browsers \
  -H "Content-Type: application/json" \
  -d '{"proxy": {"server": "http://proxy.example.com:8080"}}'

# Create browser with authenticated proxy
curl -X POST http://localhost:8000/browsers \
  -H "Content-Type: application/json" \
  -d '{"proxy": {"server": "http://proxy.example.com:8080", "username": "user", "password": "pass"}}'

# Create browser with SOCKS5 proxy
curl -X POST http://localhost:8000/browsers \
  -H "Content-Type: application/json" \
  -d '{"proxy": {"server": "socks5://127.0.0.1:1080"}}'
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
| `login` | Set HTTP Basic Auth | `username`, `password` |

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

// Set HTTP Basic Auth credentials and navigate
{
  "actions": [
    {"action": "login", "username": "admin", "password": "secret123"}
  ]
}

// Login and access protected page
{
  "url": "https://protected.example.com/admin",
  "actions": [
    {"action": "login", "username": "admin", "password": "secret123"},
    {"action": "html"}
  ]
}

// Clear HTTP credentials
{
  "actions": [
    {"action": "login", "username": "", "password": ""}
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

## Parallel Scraping Example

Here is a Python example showing how to scrape multiple pages in parallel using the default browser profile.

```python
import asyncio
import aiohttp

API_URL = "http://localhost:8000"

async def scrape_url(session, url):
    """
    Creates a new browser tab (session), scrapes content, and closes the tab.
    Uses the default browser profile automatically.
    """
    try:
        # 1. Start a new session (tab) in the default browser
        async with session.post(f"{API_URL}/start_session") as resp:
            data = await resp.json()
            session_id = data["session_id"]
            print(f"Started session {session_id} for {url}")

        # 2. Navigate and get content
        # We pass X-Session-Id to target our specific tab
        headers = {"X-Session-Id": session_id}
        payload = {
            "url": url,
            "return_html": False, # Get text only
            "wait_until": "domcontentloaded",
            "idle": 1  # Optional wait for dynamic content
        }
        
        async with session.post(f"{API_URL}/content", json=payload, headers=headers) as resp:
            content = await resp.text()
            print(f"[{url}] Content length: {len(content)}")
            
        # 3. Cleanup: End the session (close the tab)
        async with session.delete(f"{API_URL}/end_session", headers=headers) as resp:
            await resp.json()
            print(f"Closed session {session_id}")
            
        return {"url": url, "content_length": len(content)}

    except Exception as e:
        print(f"Error scraping {url}: {e}")
        return {"url": url, "error": str(e)}

async def main():
    urls = [
        "https://example.com",
        "https://python.org",
        "https://fastapi.tiangolo.com",
        "https://github.com",
        "https://news.ycombinator.com"
    ]

    # Create a persistent HTTP session for API calls
    async with aiohttp.ClientSession() as session:
        # Create tasks for all URLs to run in parallel
        tasks = [scrape_url(session, url) for url in urls]
        
        # Run all tasks concurrently
        results = await asyncio.gather(*tasks)
        
        print("\n--- Results ---")
        for res in results:
            print(res)

if __name__ == "__main__":
    # uv add aiohttp
    asyncio.run(main())
```

### Key Points:
1.  **Isolation**: Each `scrape_url` call creates its own **Session ID** (`start_session`). This corresponds to a unique tab in the browser.
2.  **Concurrency**: `asyncio.gather` runs all requests simultaneously. The browser handles multiple tabs efficiently.
3.  **Cleanup**: Always call `end_session` to close the tab and free up memory.
4.  **Default Browser**: By not specifying `X-Browser-Id`, all sessions open in the default persistent profile.

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
