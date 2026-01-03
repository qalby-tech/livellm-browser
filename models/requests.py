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