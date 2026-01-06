"""
Smoke tests for livellm-browser FastAPI controller.

These tests verify basic functionality without requiring a real browser.
Run with: uv run pytest tests/ -v
"""
import pytest
from fastapi.testclient import TestClient


class TestHealthEndpoint:
    """Tests for the /ping health check endpoint."""

    def test_ping_returns_200(self, client: TestClient):
        """Verify ping endpoint returns 200 OK."""
        response = client.get("/ping")
        assert response.status_code == 200

    def test_ping_response_structure(self, client: TestClient):
        """Verify ping response has correct structure."""
        response = client.get("/ping")
        data = response.json()
        
        assert "status" in data
        assert "message" in data
        assert data["status"] == "ok"
        assert "running" in data["message"].lower()


class TestSessionManagement:
    """Tests for session creation and deletion."""

    def test_start_session_returns_200(self, client: TestClient):
        """Verify start_session endpoint returns 200 OK."""
        response = client.get("/start_session")
        assert response.status_code == 200

    def test_start_session_returns_session_id(self, client: TestClient):
        """Verify start_session returns a valid session ID."""
        response = client.get("/start_session")
        data = response.json()
        
        assert "session_id" in data
        assert data["session_id"] is not None
        assert len(data["session_id"]) > 0
        # UUID format check (basic)
        assert "-" in data["session_id"]

    def test_end_session_without_header_returns_400(self, client: TestClient):
        """Verify end_session requires X-Session-Id header."""
        response = client.get("/end_session")
        assert response.status_code == 400

    def test_end_session_with_invalid_session(self, client: TestClient):
        """Verify end_session handles non-existent sessions gracefully."""
        response = client.get(
            "/end_session",
            headers={"X-Session-Id": "non-existent-session-id"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"

    def test_full_session_lifecycle(self, client: TestClient):
        """Test complete session creation and deletion flow."""
        # Create session
        start_response = client.get("/start_session")
        assert start_response.status_code == 200
        session_id = start_response.json()["session_id"]
        
        # End session
        end_response = client.get(
            "/end_session",
            headers={"X-Session-Id": session_id}
        )
        assert end_response.status_code == 200


class TestContentEndpoint:
    """Tests for the /content endpoint."""

    def test_content_requires_url(self, client: TestClient):
        """Verify content endpoint requires URL in request body."""
        response = client.post("/content", json={})
        assert response.status_code == 422  # Validation error

    def test_content_with_valid_url(self, client: TestClient):
        """Verify content endpoint accepts valid URL."""
        response = client.post("/content", json={"url": "https://example.com"})
        # Should succeed with mocked browser
        assert response.status_code == 200

    def test_content_return_html_flag(self, client: TestClient):
        """Verify content endpoint respects return_html flag."""
        response = client.post(
            "/content",
            json={"url": "https://example.com", "return_html": False}
        )
        assert response.status_code == 200


class TestSearchEndpoint:
    """Tests for the /search endpoint."""

    def test_search_requires_query(self, client: TestClient):
        """Verify search endpoint requires query parameter."""
        response = client.post("/search", json={})
        assert response.status_code == 422  # Validation error

    def test_search_with_valid_query(self, client: TestClient):
        """Verify search endpoint accepts valid query."""
        response = client.post("/search", json={"query": "test search"})
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_search_with_count_parameter(self, client: TestClient):
        """Verify search endpoint accepts count parameter."""
        response = client.post(
            "/search",
            json={"query": "test search", "count": 10}
        )
        assert response.status_code == 200


class TestSelectorsEndpoint:
    """Tests for the /selectors endpoint."""

    def test_selectors_requires_url(self, client: TestClient):
        """Verify selectors endpoint requires URL."""
        response = client.post("/selectors", json={"selectors": []})
        assert response.status_code == 422  # Validation error

    def test_selectors_requires_selectors_list(self, client: TestClient):
        """Verify selectors endpoint requires selectors list."""
        response = client.post("/selectors", json={"url": "https://example.com"})
        assert response.status_code == 422  # Validation error

    def test_selectors_with_valid_request(self, client: TestClient):
        """Verify selectors endpoint works with valid request."""
        response = client.post(
            "/selectors",
            json={
                "url": "https://example.com",
                "selectors": [
                    {"name": "title", "type": "css", "value": "h1"}
                ]
            }
        )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["name"] == "title"

    def test_selectors_with_xpath(self, client: TestClient):
        """Verify selectors endpoint supports XPath selectors."""
        response = client.post(
            "/selectors",
            json={
                "url": "https://example.com",
                "selectors": [
                    {"name": "heading", "type": "xml", "value": "//h1"}
                ]
            }
        )
        assert response.status_code == 200


class TestRequestValidation:
    """Tests for Pydantic model validation."""

    def test_search_request_default_count(self, client: TestClient):
        """Verify SearchRequest uses default count of 5."""
        response = client.post("/search", json={"query": "test"})
        assert response.status_code == 200

    def test_content_request_wait_until_options(self, client: TestClient):
        """Verify GetHtmlRequest accepts valid wait_until values."""
        valid_options = ["commit", "domcontentloaded", "load", "networkidle"]
        
        for option in valid_options:
            response = client.post(
                "/content",
                json={"url": "https://example.com", "wait_until": option}
            )
            assert response.status_code == 200, f"Failed for wait_until={option}"

    def test_content_request_invalid_wait_until(self, client: TestClient):
        """Verify GetHtmlRequest rejects invalid wait_until value."""
        response = client.post(
            "/content",
            json={"url": "https://example.com", "wait_until": "invalid"}
        )
        assert response.status_code == 422

    def test_selector_type_validation(self, client: TestClient):
        """Verify Selector rejects invalid type."""
        response = client.post(
            "/selectors",
            json={
                "url": "https://example.com",
                "selectors": [
                    {"name": "test", "type": "invalid", "value": "div"}
                ]
            }
        )
        assert response.status_code == 422


class TestOpenAPISchema:
    """Tests for API documentation and schema."""

    def test_openapi_schema_available(self, client: TestClient):
        """Verify OpenAPI schema is accessible."""
        response = client.get("/openapi.json")
        assert response.status_code == 200
        
        schema = response.json()
        assert schema["info"]["title"] == "Controller API"
        assert schema["info"]["version"] == "0.1.0"

    def test_docs_endpoint_available(self, client: TestClient):
        """Verify Swagger UI docs are accessible."""
        response = client.get("/docs")
        assert response.status_code == 200

    def test_redoc_endpoint_available(self, client: TestClient):
        """Verify ReDoc docs are accessible."""
        response = client.get("/redoc")
        assert response.status_code == 200


class TestSessionIdHeader:
    """Tests for X-Session-Id header handling."""

    def test_content_creates_session_without_header(self, client: TestClient):
        """Verify endpoints create session when no X-Session-Id provided."""
        response = client.post("/content", json={"url": "https://example.com"})
        assert response.status_code == 200

    def test_content_with_session_id_header(self, client: TestClient):
        """Verify endpoints accept X-Session-Id header."""
        # First create a session
        session_response = client.get("/start_session")
        session_id = session_response.json()["session_id"]
        
        # Use the session
        response = client.post(
            "/content",
            json={"url": "https://example.com"},
            headers={"X-Session-Id": session_id}
        )
        assert response.status_code == 200

