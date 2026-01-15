from pydantic import BaseModel, Field
from typing import Literal, Optional, List, Union, Annotated
from pydantic import Discriminator


class SearchRequest(BaseModel):
    query: str = Field(..., description="The search query string")
    count: int = Field(default=5, description="The maximum number of search results requested")


class GetHtmlRequest(BaseModel):
    url: str = Field(..., description="The URL to get the HTML from")
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


# Union type for all possible actions with discriminator
SelectorAction = Annotated[
    Union[HtmlAction, TextAction, ClickAction, FillAction, AttributeAction],
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
    """
    name: str = Field(..., description="Unique identifier for the selector")
    type: Literal["css", "xml"] = Field(..., description="Type of selector: css or xml (xpath)")
    value: str = Field(..., description="The selector string")
    actions: List[SelectorAction] = Field(
        default_factory=lambda: [HtmlAction()],
        description="List of actions to perform on selected elements. Default is [{'action': 'html'}]"
    )


class SelectorRequest(BaseModel):
    url: str = Field(..., description="The URL to execute selectors on")
    selectors: list[Selector] = Field(..., description="List of selectors to execute")
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = Field(default="commit", description="The wait until event to wait for")
    timeout: float = Field(default=3600, description="The timeout in milliseconds")
    idle: float = Field(default=1.5, description="Idle time in milliseconds to wait after page loaded")


class ScreenshotRequest(BaseModel):
    url: str = Field(..., description="The URL to take a screenshot of")
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = Field(default="load", description="The wait until event to wait for")
    timeout: float = Field(default=30000, description="The timeout in milliseconds")
    idle: float = Field(default=1.0, description="Idle time in seconds to wait after page loaded")
    full_page: bool = Field(default=False, description="If True, capture full scrollable page")


class MouseMoveRequest(BaseModel):
    url: str = Field(..., description="The URL to navigate to")
    x: float = Field(..., description="The x coordinate to move the mouse to")
    y: float = Field(..., description="The y coordinate to move the mouse to")
    steps: int = Field(default=10, description="Number of intermediate steps for smooth movement")


class ClickRequest(BaseModel):
    url: str = Field(..., description="The URL to navigate to")
    x: float = Field(..., description="The x coordinate to click")
    y: float = Field(..., description="The y coordinate to click")
    button: Literal["left", "right", "middle"] = Field(default="left", description="Mouse button to click")
    click_count: int = Field(default=1, description="Number of clicks (1 for single, 2 for double)")
    delay: float = Field(default=0, description="Delay between mousedown and mouseup in milliseconds")


class ScrollRequest(BaseModel):
    url: str = Field(..., description="The URL to navigate to")
    x: float = Field(default=0, description="Horizontal scroll delta")
    y: float = Field(default=0, description="Vertical scroll delta (positive = down, negative = up)")