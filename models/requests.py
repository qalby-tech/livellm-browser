from pydantic import BaseModel, Field
from typing import Literal, Optional, List, Union, Annotated
from pydantic import Discriminator


class SearchRequest(BaseModel):
    query: str = Field(..., description="The search query string")
    count: int = Field(default=5, description="The maximum number of search results requested")


class GetHtmlRequest(BaseModel):
    url: Optional[str] = Field(default=None, description="The URL to get the HTML from. If not provided, uses current page.")
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = Field(default="commit", description="The wait until event to wait for")
    timeout: float = Field(default=3600, description="The timeout in milliseconds")
    idle: float = Field(default=1.5, description="Idle time in milliseconds to wait after page loaded")
    return_html: bool = Field(default=True, description="If True, return HTML content. If False, return only inner text")


# ==================== Action Models ====================

class Action(BaseModel):
    """
    Base action model. All specific actions inherit from this.
    
    Action types:
    - html: Returns the outer HTML of elements (default behavior)
    - text: Returns only the text content of elements  
    - click: Clicks on the element(s) - supports nth parameter
    - fill: Fills input element(s) with a value - supports nth parameter
    - attribute: Gets the value of a specific attribute
    """
    action: str = Field(..., description="Action type identifier")


class HtmlAction(Action):
    """Get outer HTML of selected elements"""
    action: Literal["html"] = Field(default="html", description="Returns outer HTML of elements")


class TextAction(Action):
    """Get text content of selected elements"""
    action: Literal["text"] = Field(default="text", description="Returns text content of elements")


class ClickAction(Action):
    """
    Click on selected element(s).
    
    The nth parameter controls which element to click:
    - nth=0 (default): click only the first element
    - nth=-1: click only the last element
    - nth=None: click all elements
    """
    action: Literal["click"] = Field(default="click", description="Clicks on element(s)")
    nth: Optional[int] = Field(
        default=0, 
        description="Which element to click: 0=first (default), -1=last, None=all"
    )


class FillAction(Action):
    """
    Fill input element(s) with a value.
    
    The nth parameter controls which element to fill:
    - nth=0 (default): fill only the first element
    - nth=-1: fill only the last element  
    - nth=None: fill all elements
    """
    action: Literal["fill"] = Field(default="fill", description="Fills input element(s) with a value")
    value: str = Field(..., description="Value to fill into the input element(s)")
    nth: Optional[int] = Field(
        default=0, 
        description="Which element to fill: 0=first (default), -1=last, None=all"
    )


class AttributeAction(Action):
    """
    Get attribute value from selected elements.
    
    Examples:
    - CSS: selector="a.link", attribute_name="href"
    - CSS: selector="div[data-id]", attribute_name="data-id"
    - CSS: selector="img.thumbnail", attribute_name="src"
    - XPath: selector="//a/@href" (returns attribute values directly)
    - XPath: selector="//img", attribute_name="src"
    """
    action: Literal["attribute"] = Field(default="attribute", description="Gets attribute value from elements")
    name: str = Field(..., description="Attribute name to extract (e.g., 'href', 'src', 'data-id')")


class RemoveAction(Action):
    """
    Remove selected element(s) from the DOM.
    
    The nth parameter controls which element to remove:
    - nth=0 (default): remove only the first element
    - nth=-1: remove only the last element
    - nth=None: remove all matching elements
    
    Useful for cleaning up pages before extracting content (e.g., removing ads, popups, etc.)
    """
    action: Literal["remove"] = Field(default="remove", description="Removes element(s) from the DOM")
    nth: Optional[int] = Field(
        default=0, 
        description="Which element to remove: 0=first (default), -1=last, None=all"
    )


# ==================== Interact Actions ====================

class ScreenshotAction(Action):
    """Take a screenshot of the page"""
    action: Literal["screenshot"] = Field(default="screenshot", description="Takes a screenshot of the page")
    full_page: bool = Field(default=False, description="If True, capture full scrollable page")


class ScrollAction(Action):
    """Scroll the page by specified delta"""
    action: Literal["scroll"] = Field(default="scroll", description="Scrolls the page by delta")
    x: float = Field(default=0, description="Horizontal scroll delta")
    y: float = Field(default=0, description="Vertical scroll delta (positive = down, negative = up)")


class MoveAction(Action):
    """Move mouse cursor to coordinates"""
    action: Literal["move"] = Field(default="move", description="Moves mouse to coordinates")
    x: float = Field(..., description="X coordinate to move to")
    y: float = Field(..., description="Y coordinate to move to")
    steps: int = Field(default=10, description="Number of intermediate steps for smooth movement")


class MouseClickAction(Action):
    """
    Click at specific coordinates on the page.
    
    Different from ClickAction which clicks on selected elements.
    This action clicks at absolute x,y coordinates.
    """
    action: Literal["mouse_click"] = Field(default="mouse_click", description="Clicks at coordinates")
    x: float = Field(..., description="X coordinate to click")
    y: float = Field(..., description="Y coordinate to click")
    button: Literal["left", "right", "middle"] = Field(default="left", description="Mouse button to click")
    click_count: int = Field(default=1, description="Number of clicks (1 for single, 2 for double)")
    delay: float = Field(default=0, description="Delay between mousedown and mouseup in milliseconds")


class IdleAction(Action):
    """
    Wait for a specified duration.
    
    Useful for adding delays between actions, waiting for animations,
    or giving the page time to load dynamic content.
    """
    action: Literal["idle"] = Field(default="idle", description="Waits for specified duration")
    duration: float = Field(..., description="Duration to wait in seconds")


class LoginAction(Action):
    """
    Set HTTP Basic Authentication credentials for the browser context.
    
    This sets the Authorization header for all subsequent requests in the session.
    Use this when accessing pages protected by HTTP Basic Authentication.
    
    To clear credentials, call with empty username and password.
    """
    action: Literal["login"] = Field(default="login", description="Sets HTTP authentication credentials")
    username: str = Field(..., description="HTTP authentication username")
    password: str = Field(..., description="HTTP authentication password")


# Union type for selector actions
SelectorAction = Annotated[
    Union[HtmlAction, TextAction, ClickAction, FillAction, AttributeAction, RemoveAction],
    Discriminator("action")
]

# Union type for interact actions (reuses HtmlAction and TextAction from selector actions)
InteractAction = Annotated[
    Union[ScreenshotAction, ScrollAction, MoveAction, MouseClickAction, IdleAction, HtmlAction, TextAction, LoginAction],
    Discriminator("action")
]


class Selector(BaseModel):
    """
    Selector definition with optional actions.
    
    Usage examples:
    
    1. Basic HTML extraction (default):
       {"name": "links", "type": "css", "value": "a.nav-link"}
    
    2. Extract text only:
       {"name": "titles", "type": "css", "value": "h1", "actions": [{"action": "text"}]}
    
    3. Click on first element (default):
       {"name": "button", "type": "css", "value": "#submit-btn", "actions": [{"action": "click"}]}
    
    4. Click on last element:
       {"name": "button", "type": "css", "value": ".item", "actions": [{"action": "click", "nth": -1}]}
    
    5. Click on all elements:
       {"name": "buttons", "type": "css", "value": ".btn", "actions": [{"action": "click", "nth": null}]}
    
    6. Fill first input (default):
       {"name": "search", "type": "css", "value": "input[name='q']", "actions": [{"action": "fill", "value": "hello"}]}
    
    7. Fill all inputs:
       {"name": "fields", "type": "css", "value": "input.field", "actions": [{"action": "fill", "value": "test", "nth": null}]}
    
    8. Get attribute value:
       {"name": "hrefs", "type": "css", "value": "a", "actions": [{"action": "attribute", "name": "href"}]}
    
    9. Multiple actions (get text then click first):
       {"name": "menu", "type": "css", "value": ".dropdown", "actions": [{"action": "text"}, {"action": "click"}]}
    
    10. XPath to get attribute directly:
        {"name": "links", "type": "xml", "value": "//a/@href"}
    
    11. XPath with attribute action:
        {"name": "images", "type": "xml", "value": "//img", "actions": [{"action": "attribute", "name": "src"}]}
    
    12. Remove all matching elements (e.g., ads):
        {"name": "ads", "type": "css", "value": ".advertisement", "actions": [{"action": "remove", "nth": null}]}
    
    13. Remove first popup:
        {"name": "popup", "type": "css", "value": ".modal", "actions": [{"action": "remove"}]}
    """
    name: str = Field(..., description="Unique identifier for the selector")
    type: Literal["css", "xml"] = Field(..., description="Type of selector: css or xml (xpath)")
    value: str = Field(..., description="The selector string")
    actions: List[SelectorAction] = Field(
        default_factory=lambda: [HtmlAction()],
        description="List of actions to perform on selected elements. Default is [{'action': 'html'}]"
    )


class SelectorRequest(BaseModel):
    url: Optional[str] = Field(default=None, description="The URL to execute selectors on. If not provided, uses current page.")
    selectors: list[Selector] = Field(..., description="List of selectors to execute")
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = Field(default="commit", description="The wait until event to wait for")
    timeout: float = Field(default=3600, description="The timeout in milliseconds")
    idle: float = Field(default=1.5, description="Idle time in milliseconds to wait after page loaded")


class InteractRequest(BaseModel):
    """
    Unified request for page interactions using actions list.
    
    If URL is provided, navigates to it first. Otherwise, uses current page.
    Actions are executed in the order they appear in the list.
    
    Available actions:
    - screenshot: Take a screenshot (returns PNG image)
    - scroll: Scroll the page by delta
    - move: Move mouse to coordinates
    - mouse_click: Click at coordinates
    - idle: Wait for specified duration
    - html: Get page HTML content
    - text: Get page text content
    
    Usage examples:
    
    1. Take screenshot of current page:
       {"actions": [{"action": "screenshot"}]}
       
    2. Take full page screenshot of URL:
       {"url": "https://example.com", "actions": [{"action": "screenshot", "full_page": true}]}
       
    3. Click at coordinates:
       {"actions": [{"action": "mouse_click", "x": 100, "y": 200}]}
       
    4. Double-click with right button:
       {"actions": [{"action": "mouse_click", "x": 100, "y": 200, "button": "right", "click_count": 2}]}
       
    5. Move mouse to position:
       {"actions": [{"action": "move", "x": 300, "y": 400, "steps": 20}]}
       
    6. Scroll down:
       {"actions": [{"action": "scroll", "y": 500}]}
       
    7. Navigate and scroll:
       {"url": "https://example.com", "actions": [{"action": "scroll", "y": 1000}]}
       
    8. Multiple actions (move, click, then screenshot):
       {"actions": [
           {"action": "move", "x": 100, "y": 200},
           {"action": "mouse_click", "x": 100, "y": 200},
           {"action": "screenshot"}
       ]}
       
    9. Scroll and take screenshot:
       {"actions": [
           {"action": "scroll", "y": 500},
           {"action": "screenshot", "full_page": true}
       ]}
       
    10. Wait between actions:
        {"actions": [
            {"action": "mouse_click", "x": 100, "y": 200},
            {"action": "idle", "duration": 2},
            {"action": "screenshot"}
        ]}
        
    11. Get page HTML:
        {"actions": [{"action": "html"}]}
        
    12. Get page text:
        {"actions": [{"action": "text"}]}
        
    13. Navigate, scroll, then get text:
        {"url": "https://example.com", "actions": [
            {"action": "scroll", "y": 500},
            {"action": "text"}
        ]}
    """
    url: Optional[str] = Field(default=None, description="URL to navigate to. If not provided, uses current page.")
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = Field(default="commit", description="Wait until event when navigating")
    timeout: float = Field(default=30000, description="Navigation timeout in milliseconds")
    idle: float = Field(default=0, description="Idle time in seconds to wait after page loaded")
    actions: List[InteractAction] = Field(..., description="List of actions to perform in order")