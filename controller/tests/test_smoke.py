"""
Smoke tests for livellm-controller FastAPI controller.

Run with: uv run pytest tests/ -v
"""
from fastapi.testclient import TestClient


class TestHealthEndpoint:
    """Tests for the /ping health check endpoint."""

    def test_ping_returns_200(self, client: TestClient):
        response = client.get("/ping")
        assert response.status_code == 200

    def test_ping_response_structure(self, client: TestClient):
        response = client.get("/ping")
        data = response.json()
        assert data["status"] == "ok"
        assert "running" in data["message"].lower()


class TestSessionManagement:
    """Tests for session creation and deletion."""

    def test_start_session_returns_200(self, client: TestClient):
        response = client.post("/start_session")
        assert response.status_code == 200

    def test_start_session_returns_session_id(self, client: TestClient):
        response = client.post("/start_session")
        data = response.json()
        assert "session_id" in data
        assert len(data["session_id"]) > 0
        assert "-" in data["session_id"]

    def test_end_session_without_header_returns_400(self, client: TestClient):
        response = client.delete("/end_session")
        assert response.status_code == 400

    def test_end_session_with_invalid_session(self, client: TestClient):
        response = client.delete(
            "/end_session",
            headers={"X-Session-Id": "non-existent-session-id"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "success"

    def test_full_session_lifecycle(self, client: TestClient):
        start_response = client.post("/start_session")
        assert start_response.status_code == 200
        session_id = start_response.json()["session_id"]

        end_response = client.delete(
            "/end_session",
            headers={"X-Session-Id": session_id},
        )
        assert end_response.status_code == 200


class TestContentEndpoint:
    """Tests for the /content endpoint."""

    def test_content_works_without_url(self, client: TestClient):
        response = client.post("/content", json={"steps": 0})
        assert response.status_code == 200

    def test_content_with_valid_url(self, client: TestClient):
        response = client.post("/content", json={"url": "https://example.com", "steps": 0})
        assert response.status_code == 200

    def test_content_default_output_is_text(self, client: TestClient):
        response = client.post("/content", json={"steps": 0})
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/plain; charset=utf-8"

    def test_content_output_html(self, client: TestClient):
        response = client.post(
            "/content",
            json={"url": "https://example.com", "output_action": "html", "steps": 0},
        )
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_content_output_screenshot(self, client: TestClient):
        response = client.post(
            "/content",
            json={"url": "https://example.com", "output_action": "screenshot", "steps": 0},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"

    def test_content_output_screenshot_full(self, client: TestClient):
        response = client.post(
            "/content",
            json={"url": "https://example.com", "output_action": "screenshot_full", "steps": 0},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"

    def test_content_with_scroll_steps(self, client: TestClient):
        """Verify content endpoint accepts scroll parameters."""
        response = client.post(
            "/content",
            json={
                "url": "https://example.com",
                "steps": 2,
                "step_delay": 0.01,
                "step_pixels": 500,
            },
        )
        assert response.status_code == 200


class TestSearchEndpoint:
    """Tests for the /search endpoint."""

    def test_search_requires_query(self, client: TestClient):
        response = client.post("/search", json={})
        assert response.status_code == 422

    def test_search_with_valid_query(self, client: TestClient):
        response = client.post("/search", json={"query": "test search"})
        assert response.status_code == 200
        data = response.json()
        assert "results" in data

    def test_search_with_count_parameter(self, client: TestClient):
        response = client.post("/search", json={"query": "test search", "count": 10})
        assert response.status_code == 200

    def test_search_with_idle_parameter(self, client: TestClient):
        response = client.post("/search", json={"query": "test", "idle": 0.1})
        assert response.status_code == 200

    def test_search_with_max_pages_parameter(self, client: TestClient):
        response = client.post("/search", json={"query": "test", "max_pages": 3})
        assert response.status_code == 200


class TestSelectorAction:
    """Tests for the selector action inside /interact."""

    def test_selector_click(self, client: TestClient):
        response = client.post(
            "/interact",
            json={
                "actions": [
                    {"action": "selector", "type": "css", "value": "button", "do": "click"},
                ],
            },
        )
        assert response.status_code == 200

    def test_selector_click_with_args(self, client: TestClient):
        response = client.post(
            "/interact",
            json={
                "actions": [
                    {"action": "selector", "value": "button", "do": "click", "args": {"nth": -1}},
                ],
            },
        )
        assert response.status_code == 200

    def test_selector_fill(self, client: TestClient):
        response = client.post(
            "/interact",
            json={
                "actions": [
                    {"action": "selector", "type": "css", "value": "input", "do": "fill", "args": {"value": "hello"}},
                ],
            },
        )
        assert response.status_code == 200

    def test_selector_fill_requires_value(self, client: TestClient):
        """FillArgs requires 'value' field."""
        response = client.post(
            "/interact",
            json={
                "actions": [
                    {"action": "selector", "value": "input", "do": "fill", "args": {}},
                ],
            },
        )
        assert response.status_code == 422

    def test_selector_fill_wrong_args_type(self, client: TestClient):
        """fill action must use FillArgs, not ClickArgs."""
        response = client.post(
            "/interact",
            json={
                "actions": [
                    {"action": "selector", "value": "input", "do": "fill", "args": {"nth": 0}},
                ],
            },
        )
        assert response.status_code == 422

    def test_selector_remove(self, client: TestClient):
        response = client.post(
            "/interact",
            json={
                "actions": [
                    {"action": "selector", "type": "css", "value": ".ad", "do": "remove", "args": {"nth": None}},
                ],
                "output_action": "text",
            },
        )
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]

    def test_selector_with_xpath(self, client: TestClient):
        response = client.post(
            "/interact",
            json={
                "actions": [
                    {"action": "selector", "type": "xml", "value": "//button", "do": "click"},
                ],
            },
        )
        assert response.status_code == 200

    def test_selector_requires_do(self, client: TestClient):
        """'do' field is required."""
        response = client.post(
            "/interact",
            json={
                "actions": [
                    {"action": "selector", "type": "css", "value": "h1"},
                ],
            },
        )
        assert response.status_code == 422

    def test_selector_invalid_do(self, client: TestClient):
        """Only click, fill, remove are valid 'do' values."""
        response = client.post(
            "/interact",
            json={
                "actions": [
                    {"action": "selector", "type": "css", "value": "h1", "do": "html"},
                ],
            },
        )
        assert response.status_code == 422

    def test_selector_mixed_with_other_actions(self, client: TestClient):
        """Selector can be mixed with other interact actions."""
        response = client.post(
            "/interact",
            json={
                "url": "https://example.com",
                "actions": [
                    {"action": "selector", "type": "css", "value": ".ad", "do": "remove", "args": {"nth": None}},
                    {"action": "idle", "duration": 0.01},
                ],
                "output_action": "html",
            },
        )
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_selector_default_type_is_css(self, client: TestClient):
        """Type defaults to 'css' if not provided."""
        response = client.post(
            "/interact",
            json={
                "actions": [
                    {"action": "selector", "value": "button", "do": "click"},
                ],
            },
        )
        assert response.status_code == 200

    def test_selector_output_action_controls_response(self, client: TestClient):
        """Selector actions are side effects; output_action controls the response."""
        response = client.post(
            "/interact",
            json={
                "actions": [
                    {"action": "selector", "type": "css", "value": ".ad", "do": "remove", "args": {"nth": None}},
                ],
                "output_action": "screenshot",
            },
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"


class TestRequestValidation:
    """Tests for Pydantic model validation."""

    def test_search_request_default_count(self, client: TestClient):
        response = client.post("/search", json={"query": "test"})
        assert response.status_code == 200

    def test_content_request_wait_until_options(self, client: TestClient):
        for option in ("commit", "domcontentloaded", "load", "networkidle"):
            response = client.post(
                "/content",
                json={"url": "https://example.com", "wait_until": option, "steps": 0},
            )
            assert response.status_code == 200, f"Failed for wait_until={option}"

    def test_content_request_invalid_wait_until(self, client: TestClient):
        response = client.post(
            "/content",
            json={"url": "https://example.com", "wait_until": "invalid"},
        )
        assert response.status_code == 422

    def test_content_request_invalid_output_action(self, client: TestClient):
        response = client.post(
            "/content",
            json={"url": "https://example.com", "output_action": "invalid"},
        )
        assert response.status_code == 422

    def test_selector_type_validation(self, client: TestClient):
        response = client.post(
            "/interact",
            json={
                "actions": [
                    {"action": "selector", "type": "invalid", "value": "div", "do": "click"},
                ],
            },
        )
        assert response.status_code == 422


class TestOpenAPISchema:
    """Tests for API documentation and schema."""

    def test_openapi_schema_available(self, client: TestClient):
        response = client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert schema["info"]["title"] == "Controller API"
        assert schema["info"]["version"] == "0.4.0"

    def test_docs_endpoint_available(self, client: TestClient):
        response = client.get("/docs")
        assert response.status_code == 200

    def test_redoc_endpoint_available(self, client: TestClient):
        response = client.get("/redoc")
        assert response.status_code == 200


class TestSessionIdHeader:
    """Tests for X-Session-Id header handling."""

    def test_content_creates_session_without_header(self, client: TestClient):
        response = client.post("/content", json={"url": "https://example.com", "steps": 0})
        assert response.status_code == 200

    def test_content_with_session_id_header(self, client: TestClient):
        session_response = client.post("/start_session")
        session_id = session_response.json()["session_id"]

        response = client.post(
            "/content",
            json={"url": "https://example.com", "steps": 0},
            headers={"X-Session-Id": session_id},
        )
        assert response.status_code == 200


class TestInteractEndpoint:
    """Tests for the /interact endpoint."""

    def test_interact_with_empty_body_returns_text(self, client: TestClient):
        """Empty body should use defaults: no actions, output_action=text."""
        response = client.post("/interact", json={})
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]

    def test_interact_with_empty_actions(self, client: TestClient):
        response = client.post("/interact", json={"actions": []})
        assert response.status_code == 200

    def test_interact_output_text(self, client: TestClient):
        response = client.post("/interact", json={"output_action": "text"})
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]

    def test_interact_output_html(self, client: TestClient):
        response = client.post("/interact", json={"output_action": "html"})
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_interact_output_screenshot(self, client: TestClient):
        response = client.post("/interact", json={"output_action": "screenshot"})
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"

    def test_interact_output_screenshot_full(self, client: TestClient):
        response = client.post(
            "/interact",
            json={"output_action": "screenshot_full"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"

    def test_interact_with_scroll_action(self, client: TestClient):
        response = client.post("/interact", json={"actions": [{"action": "scroll", "x": 0, "y": 500}]})
        assert response.status_code == 200

    def test_interact_with_idle_action(self, client: TestClient):
        response = client.post("/interact", json={"actions": [{"action": "idle", "duration": 0.1}]})
        assert response.status_code == 200

    def test_interact_with_login_action(self, client: TestClient):
        response = client.post(
            "/interact",
            json={"actions": [{"action": "login", "username": "testuser", "password": "testpass"}]},
        )
        assert response.status_code == 200

    def test_interact_login_with_empty_credentials_clears_auth(self, client: TestClient):
        response = client.post(
            "/interact",
            json={"actions": [{"action": "login", "username": "", "password": ""}]},
        )
        assert response.status_code == 200

    def test_interact_with_multiple_actions(self, client: TestClient):
        response = client.post(
            "/interact",
            json={
                "actions": [
                    {"action": "login", "username": "admin", "password": "secret"},
                    {"action": "idle", "duration": 0.1},
                ],
                "output_action": "html",
            },
        )
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_interact_with_url_navigation(self, client: TestClient):
        response = client.post(
            "/interact",
            json={"url": "https://example.com", "output_action": "html"},
        )
        assert response.status_code == 200

    def test_interact_login_requires_username(self, client: TestClient):
        response = client.post(
            "/interact",
            json={"actions": [{"action": "login", "password": "testpass"}]},
        )
        assert response.status_code == 422

    def test_interact_login_requires_password(self, client: TestClient):
        response = client.post(
            "/interact",
            json={"actions": [{"action": "login", "username": "testuser"}]},
        )
        assert response.status_code == 422

    def test_interact_invalid_output_action(self, client: TestClient):
        response = client.post(
            "/interact",
            json={"output_action": "invalid"},
        )
        assert response.status_code == 422


class TestBrowserManagement:
    """Tests for browser management endpoints (CDP connect/disconnect model)."""

    def test_list_browsers(self, client: TestClient):
        response = client.get("/browsers")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        if len(data) > 0:
            assert "browser_id" in data[0]
            assert "ws_url" in data[0]
            assert "session_count" in data[0]

    def test_connect_browser(self, client: TestClient):
        response = client.post(
            "/browsers",
            json={"browser_id": "my-browser", "ws_url": "ws://localhost:9222/devtools/browser/abc"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["browser_id"] == "my-browser"
        assert "ws_url" in data

    def test_connect_browser_requires_fields(self, client: TestClient):
        response = client.post("/browsers", json={})
        assert response.status_code == 422

    def test_disconnect_browser(self, client: TestClient):
        response = client.delete("/browsers/test-browser")
        assert response.status_code == 200


class TestAttributeEndpoint:
    """Tests for the /attribute endpoint."""

    def test_attribute_requires_selectors(self, client: TestClient):
        """selectors field is required."""
        response = client.post("/attribute", json={})
        assert response.status_code == 422

    def test_attribute_requires_at_least_one_selector(self, client: TestClient):
        """selectors list must not be empty."""
        response = client.post("/attribute", json={"selectors": []})
        assert response.status_code == 422

    def test_attribute_css_text_extraction(self, client: TestClient):
        """Extract text content from CSS selector."""
        response = client.post(
            "/attribute",
            json={
                "url": "https://example.com",
                "steps": 0,
                "selectors": [
                    {"name": "headings", "selector": "h1"},
                ],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["name"] == "headings"
        assert isinstance(data[0]["values"], list)

    def test_attribute_css_attribute_extraction(self, client: TestClient):
        """Extract attribute values from CSS selector."""
        response = client.post(
            "/attribute",
            json={
                "url": "https://example.com",
                "steps": 0,
                "selectors": [
                    {"name": "links", "selector": "a", "attribute": "href"},
                ],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data[0]["name"] == "links"

    def test_attribute_xpath_extraction(self, client: TestClient):
        """XPath selectors use lxml."""
        response = client.post(
            "/attribute",
            json={
                "url": "https://example.com",
                "steps": 0,
                "selectors": [
                    {"name": "title", "selector": "//h1", "type": "xpath"},
                ],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data[0]["name"] == "title"

    def test_attribute_multiple_selectors(self, client: TestClient):
        """Multiple selectors return multiple result groups."""
        response = client.post(
            "/attribute",
            json={
                "steps": 0,
                "selectors": [
                    {"name": "headings", "selector": "h1"},
                    {"name": "links", "selector": "a", "attribute": "href"},
                ],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["name"] == "headings"
        assert data[1]["name"] == "links"

    def test_attribute_without_url(self, client: TestClient):
        """Works on current page when no url is provided."""
        response = client.post(
            "/attribute",
            json={
                "steps": 0,
                "selectors": [{"name": "body", "selector": "body"}],
            },
        )
        assert response.status_code == 200

    def test_attribute_with_scroll(self, client: TestClient):
        """Accepts scroll parameters."""
        response = client.post(
            "/attribute",
            json={
                "url": "https://example.com",
                "steps": 2,
                "step_delay": 0.01,
                "step_pixels": 500,
                "selectors": [{"name": "body", "selector": "body"}],
            },
        )
        assert response.status_code == 200

    def test_attribute_default_type_is_css(self, client: TestClient):
        """type defaults to 'css'."""
        response = client.post(
            "/attribute",
            json={
                "steps": 0,
                "selectors": [{"name": "all_divs", "selector": "div"}],
            },
        )
        assert response.status_code == 200


class TestBsHelpers:
    """Unit tests for the BS4/lxml extraction helpers."""

    def test_css_text_extraction(self):
        from helpers.bs import _extract_css
        html = "<html><body><h1>Hello</h1><h1>World</h1></body></html>"
        result = _extract_css(html, "h1", None)
        assert result == ["Hello", "World"]

    def test_css_attribute_extraction(self):
        from helpers.bs import _extract_css
        html = '<html><body><a href="/a">A</a><a href="/b">B</a></body></html>'
        result = _extract_css(html, "a", "href")
        assert result == ["/a", "/b"]

    def test_css_missing_attribute(self):
        from helpers.bs import _extract_css
        html = '<html><body><a>No href</a></body></html>'
        result = _extract_css(html, "a", "href")
        assert result == []

    def test_xpath_text_extraction(self):
        from helpers.bs import _extract_xpath
        html = "<html><body><h1>Hello</h1></body></html>"
        result = _extract_xpath(html, "//h1", None)
        assert result == ["Hello"]

    def test_xpath_attribute_extraction(self):
        from helpers.bs import _extract_xpath
        html = '<html><body><a href="/a">A</a><a href="/b">B</a></body></html>'
        result = _extract_xpath(html, "//a", "href")
        assert result == ["/a", "/b"]

    def test_xpath_direct_attribute(self):
        """XPath can return attribute values directly with @attr."""
        from helpers.bs import _extract_xpath
        html = '<html><body><a href="/a">A</a><a href="/b">B</a></body></html>'
        result = _extract_xpath(html, "//a/@href", None)
        assert result == ["/a", "/b"]

    def test_css_empty_text_filtered(self):
        from helpers.bs import _extract_css
        html = "<html><body><span></span><span>Hello</span></body></html>"
        result = _extract_css(html, "span", None)
        assert result == ["Hello"]

    def test_extract_all_sync(self):
        from helpers.bs import _extract_all_sync
        html = '<html><body><h1>Title</h1><a href="/link">Link</a></body></html>'
        selectors = [
            {"name": "heading", "selector": "h1", "type": "css", "attribute": None},
            {"name": "urls", "selector": "a", "type": "css", "attribute": "href"},
        ]
        result = _extract_all_sync(html, selectors)
        assert len(result) == 2
        assert result[0] == {"name": "heading", "values": ["Title"]}
        assert result[1] == {"name": "urls", "values": ["/link"]}


class TestHealthzDriverLiveness:
    """The liveness endpoint must go red when the Node driver process dies —
    with an empty browsers dict the per-browser loop is vacuous, so only the
    driver check lets Kubernetes restart a wedged pod."""

    def test_healthz_ok_when_driver_alive(self, client: TestClient):
        response = client.get("/healthz")
        assert response.status_code == 200

    def test_healthz_503_when_driver_dead(self, client: TestClient):
        from unittest.mock import patch
        from core.browser import browser_manager

        with patch.object(browser_manager, "driver_alive", return_value=False):
            response = client.get("/healthz")
        assert response.status_code == 503
        assert "driver" in response.text.lower()

    def test_healthz_503_when_driver_dead_and_no_browsers(self, client: TestClient):
        from unittest.mock import patch
        from core.browser import browser_manager

        browser_manager.browsers.clear()
        with patch.object(browser_manager, "driver_alive", return_value=False):
            response = client.get("/healthz")
        assert response.status_code == 503
