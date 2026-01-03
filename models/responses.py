from pydantic import BaseModel, Field
from typing import Literal, List

class PingResponse(BaseModel):
    status: Literal["ok", "error"] = Field("ok", description="The status of the API")
    message: str = Field("Controller API is running", description="A message describing the status")

class SearchResult(BaseModel):
    link: str = Field(..., description="URL of the search result")
    title: str = Field(..., description="Title of the search result page")
    snippet: str = Field(..., description="A brief description or snippet from the search result page")


class SelectorResult(BaseModel):
    name: str = Field(..., description="Unique identifier matching the selector name")
    value: List[str] = Field(..., description="List of extracted HTML elements from the selector")