from enum import Enum

from pydantic import BaseModel, Discriminator, Field, model_validator
from typing import Annotated, Literal, List, Optional, Union


# ==================== Enums ====================

class OutputAction(str, Enum):
    """Determines the response format after actions are executed."""
    text = "text"
    html = "html"
    screenshot = "screenshot"
    screenshot_full = "screenshot_full"


# ==================== API Request Models ====================

class SearchRequest(BaseModel):
    """Google search with structured result parsing and optional wiki panel extraction."""
    query: str = Field(..., description="The search query string")
    count: int = Field(default=5, description="Maximum number of search results")
    idle: float = Field(default=3.0, description="Idle time in seconds after page loads before parsing results")
    max_pages: int = Field(default=10, ge=1, description="Maximum number of Google result pages to paginate through")


class SearchHintsRequest(BaseModel):
    """Get Google autocomplete suggestions (search hints) for a query."""
    query: str = Field(..., description="The search query to get hints for")
    idle: float = Field(default=1.0, description="Idle time in seconds after page loads")
    wait: float = Field(default=1.5, description="Time in seconds to wait for suggestions to appear after typing")


class ContentRequest(BaseModel):
    """
    Get page content with automatic scrolling.

    Shortcut for: navigate → idle → scroll_to_bottom → output.
    The scroll timeout is calculated as ``steps × step_delay``.
    """
    url: Optional[str] = Field(default=None, description="URL to navigate to. If not provided, uses current page.")
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = Field(
        default="commit", description="Navigation wait condition",
    )
    timeout: float = Field(default=30000, description="Navigation timeout in milliseconds")
    idle: float = Field(default=2, description="Idle time in seconds after page loads")
    output_action: OutputAction = Field(
        default=OutputAction.text,
        description="Output format: 'text', 'html', 'screenshot', or 'screenshot_full'",
    )
    steps: int = Field(default=8, ge=0, description="Number of scroll steps (0 = no scroll, 4-12 recommended)")
    step_delay: float = Field(default=1.5, description="Delay between scroll steps in seconds")
    step_pixels: int = Field(default=1500, description="Pixels to scroll per step")


class ProxySettings(BaseModel):
    """Proxy configuration for a browser instance."""
    server: str = Field(..., description="Proxy server URL (e.g., 'http://myproxy.com:3128')")
    username: Optional[str] = Field(default=None, description="Proxy authentication username")
    password: Optional[str] = Field(default=None, description="Proxy authentication password")
    bypass: Optional[str] = Field(default=None, description="Comma-separated hosts to bypass proxy")


class CreateBrowserRequest(BaseModel):
    """Create a new browser instance with optional persistent profile and proxy."""
    profile_uid: Optional[str] = Field(
        default=None,
        description="Profile UID for persistent profile in profiles/{uid}. If omitted, creates an ephemeral session.",
    )
    proxy: Optional[ProxySettings] = Field(default=None, description="Proxy settings for the browser.")


class StartSessionRequest(BaseModel):
    """Start a new session (browser tab) in a specific browser."""
    browser_id: Optional[str] = Field(
        default=None, description="Browser to create session in. Defaults to default browser.",
    )


# ==================== Action Models ====================

class Action(BaseModel):
    """Base action model. All specific actions inherit from this."""
    action: str = Field(..., description="Action type identifier")


class ScrollAction(Action):
    """Scroll the page by specified delta."""
    action: Literal["scroll"] = Field(default="scroll")
    x: float = Field(default=0, description="Horizontal scroll delta")
    y: float = Field(default=0, description="Vertical scroll delta (positive = down)")


class ScrollToBottomAction(Action):
    """Scroll to bottom in steps until timeout is reached (duration-based)."""
    action: Literal["scroll_to_bottom"] = Field(default="scroll_to_bottom")
    step_pixels: int = Field(default=500, description="Pixels per scroll step")
    step_delay: float = Field(default=0.2, description="Delay between steps in seconds")
    timeout: float = Field(default=30.0, description="Maximum scroll time in seconds")


class MoveAction(Action):
    """Move mouse cursor to coordinates."""
    action: Literal["move"] = Field(default="move")
    x: float = Field(..., description="X coordinate")
    y: float = Field(..., description="Y coordinate")
    steps: int = Field(default=10, description="Intermediate steps for smooth movement")


class MouseClickAction(Action):
    """Click at specific x,y coordinates on the page."""
    action: Literal["mouse_click"] = Field(default="mouse_click")
    x: float = Field(..., description="X coordinate")
    y: float = Field(..., description="Y coordinate")
    button: Literal["left", "right", "middle"] = Field(default="left")
    click_count: int = Field(default=1, description="Number of clicks (2 for double-click)")
    delay: float = Field(default=0, description="Delay between mousedown and mouseup in ms")


class IdleAction(Action):
    """Wait for a specified duration."""
    action: Literal["idle"] = Field(default="idle")
    duration: float = Field(..., description="Duration to wait in seconds")


class LoginAction(Action):
    """Set HTTP Basic Authentication credentials for the browser context."""
    action: Literal["login"] = Field(default="login")
    username: str = Field(..., description="HTTP auth username")
    password: str = Field(..., description="HTTP auth password")


class ClickArgs(BaseModel):
    """Arguments for clicking matched elements."""
    nth: Optional[int] = Field(default=0, description="Which element: 0=first, -1=last, null=all")


class FillArgs(BaseModel):
    """Arguments for filling matched input elements."""
    value: str = Field(..., description="Value to fill into the input element(s)")
    nth: Optional[int] = Field(default=0, description="Which element: 0=first, -1=last, null=all")


class RemoveArgs(BaseModel):
    """Arguments for removing matched elements from the DOM."""
    nth: Optional[int] = Field(default=0, description="Which element: 0=first, -1=last, null=all")


class SelectAction(Action):
    """
    Perform a DOM action on elements matching a CSS or XPath selector.

    One selector = one operation. Chain multiple selectors for multi-step workflows.
    The ``args`` field must match the ``do`` action type.

    Examples::

        {"action": "selector", "type": "css", "value": ".ad", "do": "remove", "args": {"nth": null}}
        {"action": "selector", "type": "css", "value": "input.email", "do": "fill", "args": {"value": "me@x.com"}}
        {"action": "selector", "value": "button.submit", "do": "click"}
    """
    action: Literal["selector"] = Field(default="selector")
    type: Literal["css", "xml"] = Field(default="css", description="Selector type: css or xml (xpath)")
    value: str = Field(..., description="The selector string")
    do: Literal["click", "fill", "remove"] = Field(..., description="Action to perform on matched elements")
    args: Union[ClickArgs, FillArgs, RemoveArgs] = Field(
        default_factory=ClickArgs,
        description="Arguments for the action. Must match 'do' type: ClickArgs, FillArgs, or RemoveArgs.",
    )

    @model_validator(mode="before")
    @classmethod
    def coerce_args_to_type(cls, data):
        """Parse ``args`` dict into the correct type based on ``do``."""
        if isinstance(data, dict):
            do = data.get("do")
            args = data.get("args", {})
            if isinstance(args, dict):
                type_map = {"click": ClickArgs, "fill": FillArgs, "remove": RemoveArgs}
                if do in type_map:
                    data["args"] = type_map[do](**args)
        return data


# ==================== Discriminated Union ====================

InteractAction = Annotated[
    Union[
        ScrollAction, ScrollToBottomAction, MoveAction,
        MouseClickAction, IdleAction, LoginAction, SelectAction,
    ],
    Discriminator("action"),
]


# ==================== Compound Request Models ====================

class InteractRequest(BaseModel):
    """
    Unified endpoint for page interactions.

    1. Navigate to ``url`` (if provided) and wait ``idle`` seconds.
    2. Execute all ``actions`` in order (scroll, click, move, idle, login, selector).
    3. Return result based on ``output_action``: text / html / screenshot / screenshot_full.

    Available actions: scroll, scroll_to_bottom, move, mouse_click, idle, login, selector.

    Examples::

        {"output_action": "screenshot_full"}
        {"url": "https://example.com", "actions": [{"action": "scroll_to_bottom", "timeout": 10}], "output_action": "html"}
        {"actions": [{"action": "selector", "type": "css", "value": ".ad", "do": "remove", "args": {"nth": null}}], "output_action": "text"}
        {"actions": [{"action": "selector", "value": "input", "do": "fill", "args": {"value": "hello"}}, {"action": "selector", "value": "button", "do": "click"}], "output_action": "screenshot"}
    """
    url: Optional[str] = Field(default=None, description="URL to navigate to. If not provided, uses current page.")
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = Field(default="commit")
    timeout: float = Field(default=30000, description="Navigation timeout in milliseconds")
    idle: float = Field(default=0, description="Idle time in seconds after page loads")
    actions: List[InteractAction] = Field(
        default_factory=list,
        description="List of actions to perform in order (scroll, click, move, idle, login, selector)",
    )
    output_action: OutputAction = Field(
        default=OutputAction.text,
        description="Output format: 'text', 'html', 'screenshot', or 'screenshot_full'",
    )


# ==================== Attribute Endpoint Models ====================

class AttributeSelector(BaseModel):
    """
    A single selector for extracting data from the page HTML.

    - **selector**: CSS selector string (or XPath if ``type`` is ``"xpath"``).
    - **attribute**: If set, extract this attribute from each matched element.
      If ``None``, extract the text content of each matched element.

    Examples::

        {"name": "links", "selector": "a.product-link", "attribute": "href"}
        {"name": "titles", "selector": "h2.title"}
        {"name": "images", "selector": "img.thumb", "attribute": "src"}
        {"name": "header", "selector": "//h1", "type": "xpath"}
    """
    name: str = Field(..., description="Identifier for this selector result")
    selector: str = Field(..., description="CSS or XPath selector string")
    type: Literal["css", "xpath"] = Field(
        default="css", description="Selector type: css or xpath",
    )
    attribute: Optional[str] = Field(
        default=None,
        description="Attribute to extract (e.g. 'href', 'src'). If null, extracts text content.",
    )


class AttributeRequest(BaseModel):
    """
    Extract structured data from a page using CSS or XPath selectors.

    Works like ``/content`` (navigate → idle → scroll) but instead of returning
    the full page, it uses BeautifulSoup / lxml to efficiently extract specific
    elements or attributes defined by the ``selectors`` list.

    Returns a JSON list of ``{name, values}`` objects.

    Example request::

        {
            "url": "https://example.com/products",
            "selectors": [
                {"name": "titles",  "selector": "h2.product-title"},
                {"name": "prices",  "selector": "span.price"},
                {"name": "links",   "selector": "a.product-link", "attribute": "href"},
                {"name": "header",  "selector": "//h1", "type": "xpath"}
            ]
        }
    """
    url: Optional[str] = Field(default=None, description="URL to navigate to. If not provided, uses current page.")
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = Field(
        default="commit", description="Navigation wait condition",
    )
    timeout: float = Field(default=30000, description="Navigation timeout in milliseconds")
    idle: float = Field(default=2, description="Idle time in seconds after page loads")
    steps: int = Field(default=8, ge=0, description="Number of scroll steps (0 = no scroll, 4-12 recommended)")
    step_delay: float = Field(default=1.5, description="Delay between scroll steps in seconds")
    step_pixels: int = Field(default=1500, description="Pixels to scroll per step")
    selectors: List[AttributeSelector] = Field(
        ..., min_length=1, description="List of selectors to extract data with",
    )
