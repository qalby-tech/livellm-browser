from pydantic import BaseModel, Field
from typing import Literal, List, Any, Optional

class PingResponse(BaseModel):
    status: Literal["ok", "error"] = Field("ok", description="The status of the API")
    message: str = Field("Controller API is running", description="A message describing the status")

class SearchResult(BaseModel):
    link: str = Field(..., description="URL of the search result")
    title: str = Field(..., description="Title of the search result page")
    snippet: str = Field(..., description="A brief description or snippet from the search result page")
    image: Optional[str] = Field(default=None, description="Base64-encoded thumbnail image data URL if available")


class ActionResult(BaseModel):
    """Result of a single action performed on selector elements"""
    action: str = Field(..., description="The action that was performed (html, text, click, fill, attribute)")
    values: List[str] = Field(..., description="List of results from the action")


class SelectorResult(BaseModel):
    """
    Result of selector execution with action results.
    
    Example response:
    {
        "name": "links",
        "results": [
            {"action": "text", "values": ["Home", "About", "Contact"]},
            {"action": "click", "values": ["clicked", "clicked", "clicked"]}
        ]
    }
    """
    name: str = Field(..., description="Unique identifier matching the selector name")
    results: List[ActionResult] = Field(..., description="List of action results performed on the selector")