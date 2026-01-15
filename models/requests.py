from pydantic import BaseModel, Field
from typing import Literal

class SearchRequest(BaseModel):
    query: str = Field(..., description="The search query string")
    count: int = Field(default=5, description="The maximum number of search results requested")


class GetHtmlRequest(BaseModel):
    url: str = Field(..., description="The URL to get the HTML from")
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = Field(default="commit", description="The wait until event to wait for")
    timeout: float = Field(default=3600, description="The timeout in milliseconds")
    idle: float = Field(default=1.5, description="Idle time in milliseconds to wait after page loaded")
    return_html: bool = Field(default=True, description="If True, return HTML content. If False, return only inner text")


class Selector(BaseModel):
    name: str = Field(..., description="Unique identifier for the selector")
    type: Literal["css", "xml"] = Field(..., description="Type of selector: css or xml (xpath)")
    value: str = Field(..., description="The selector string")


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