from pydantic import BaseModel, Field
from typing import Literal, List, Optional


class PingResponse(BaseModel):
    status: Literal["ok", "error"] = Field("ok", description="API status")
    message: str = Field("Controller API is running", description="Status message")


class BrowserResponse(BaseModel):
    browser_id: str
    profile_path: Optional[str]
    session_count: int


class RatingMetadata(BaseModel):
    rating: Optional[float] = Field(None, description="Rating value (e.g. 4.9)")
    reviews: Optional[int] = Field(None, description="Number of reviews")
    description: Optional[str] = Field(None, description="Full rating description")


class SearchMetadata(BaseModel):
    rating: Optional[RatingMetadata] = None
    thumbnail: Optional[str] = Field(None, description="Base64-encoded thumbnail data URL")


class SearchResult(BaseModel):
    link: str
    title: str
    snippet: str
    favicon: Optional[str] = Field(None, description="Base64-encoded favicon data URL")
    metadata: Optional[SearchMetadata] = None


class WikiResult(BaseModel):
    desc: str = Field(..., description="Combined text from wiki panel")
    wiki_links: List[str] = Field(default_factory=list)
    related_links: List[str] = Field(default_factory=list)
    misc_links: List[str] = Field(default_factory=list)


class AiReview(BaseModel):
    summary: str
    sources: List[str]


class SearchResponse(BaseModel):
    ai_review: Optional[AiReview] = None
    wiki: Optional[WikiResult] = None
    results: List[SearchResult]


class NewsResult(BaseModel):
    link: str
    title: str
    snippet: str
    favicon: Optional[str] = Field(None, description="Base64-encoded favicon data URL")
    thumbnail: Optional[str] = Field(None, description="Base64-encoded thumbnail data URL")


class NewsResponse(BaseModel):
    results: List[NewsResult]


class ImageResult(BaseModel):
    link: str
    title: str
    icon: Optional[str] = Field(None, description="Base64-encoded icon/thumbnail data URL")


class ImagesResponse(BaseModel):
    results: List[ImageResult]
