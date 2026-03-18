import asyncio
import logging
from typing import List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from patchright.async_api import Page, ElementHandle

from core.dependencies import PageDep
from models.requests import SearchRequest, SearchHintsRequest
from models.responses import (
    SearchResponse, SearchResult, SearchMetadata,
    RatingMetadata, WikiResult, AiReview,
    NewsResponse, NewsResult,
    ImagesResponse, VideosResponse, MediaResult, MediaTags,
    SearchHintsResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Search"])


# ==================== Helpers ====================

def _is_valid_result_link(href: Optional[str]) -> bool:
    """Check if a search result link is a real external URL (not #, /search, etc.)."""
    if not href:
        return False
    if href in ("#", ""):
        return False
    if href.startswith("/search"):
        return False
    if href.startswith("/"):
        return False
    parsed = urlparse(href)
    return parsed.scheme in ("http", "https")


# ==================== Search Helpers ====================

async def _extract_rating(result_div: ElementHandle) -> Optional[RatingMetadata]:
    """Extract rating metadata from a search result div."""
    try:
        rating_container = await result_div.query_selector('div[data-sncf="2"]')
        if not rating_container:
            return None

        description = None
        labeled_element = await rating_container.query_selector('[aria-label]')
        if labeled_element:
            description = await labeled_element.get_attribute('aria-label')

        spans = await rating_container.query_selector_all('span[aria-hidden="true"]')
        valid_texts = []
        for span in spans:
            text = (await span.inner_text()).strip()
            if text:
                valid_texts.append(text)

        rating = None
        reviews = None
        if len(valid_texts) >= 1:
            try:
                rating = float(valid_texts[0].replace(',', '.'))
            except ValueError:
                pass
        if len(valid_texts) >= 2:
            try:
                digits = "".join(c for c in valid_texts[1] if c.isdigit())
                if digits:
                    reviews = int(digits)
            except ValueError:
                pass

        if rating is None and reviews is None:
            return None
        return RatingMetadata(rating=rating, reviews=reviews, description=description)
    except Exception:
        return None


async def _extract_wiki(page: Page) -> Optional[WikiResult]:
    """Extract wiki panel from the search page."""
    try:
        wiki_div = await page.query_selector('div[data-spe]')
        if not wiki_div:
            return None

        rpos_divs = await wiki_div.query_selector_all('div[data-rpos]')
        descriptions = []
        raw_links = []

        for div in rpos_divs:
            text = await div.inner_text()
            if text:
                descriptions.append(text.strip())
            for a in await div.query_selector_all('a'):
                href = await a.get_attribute('href')
                if href and href != '#':
                    raw_links.append(href)

        if not descriptions and not raw_links:
            return None

        # Deduplicate links
        seen: set = set()
        unique_links = []
        for link in raw_links:
            if link not in seen:
                seen.add(link)
                unique_links.append(link)

        wiki_links, related_links, misc_links = [], [], []
        for link in unique_links:
            if 'wikipedia.org' in link:
                wiki_links.append(link)
            elif link.startswith('/search'):
                related_links.append(f"https://www.google.com{link}")
            else:
                misc_links.append(link)

        return WikiResult(
            desc="\n".join(descriptions),
            wiki_links=wiki_links,
            related_links=related_links,
            misc_links=misc_links,
        )
    except Exception:
        return None


async def _extract_ai_review(page: Page) -> Optional[AiReview]:
    """Extract AI review panel from the search page."""
    try:
        review_div = await page.query_selector('div[data-mcpr]')
        if not review_div:
            return None

        summary = await review_div.inner_text()
        if not summary:
            return None
        
        summary = summary.strip()

        raw_links = []
        for a in await review_div.query_selector_all('a'):
            href = await a.get_attribute('href')
            if _is_valid_result_link(href):
                raw_links.append(href)

        # Deduplicate links
        seen: set = set()
        sources = []
        for link in raw_links:
            if link not in seen:
                seen.add(link)
                sources.append(link)

        return AiReview(summary=summary, sources=sources)
    except Exception:
        return None


async def _parse_search_results(
    page: Page, results: List[SearchResult], seen_links: set, count: int,
) -> List[SearchResult]:
    """Parse search results from the current Google search page."""
    result_divs = await page.query_selector_all('div[data-rpos]')

    for result_div in result_divs:
        if len(results) >= count:
            break
        try:
            span_elements = await result_div.query_selector_all('span')
            link_element = None
            for span in span_elements:
                a = await span.query_selector('a')
                if a:
                    link_element = a
                    break

            if not link_element:
                continue

            href = await link_element.get_attribute('href')

            # Skip invalid links (#, /search, relative paths, etc.)
            if not _is_valid_result_link(href):
                continue

            if href in seen_links:
                continue
            seen_links.add(href)

            title = await link_element.inner_text()

            snippet_texts = []
            for span in span_elements:
                html = await span.inner_html()
                if '<em>' in html:
                    snippet_texts.append(await span.inner_text())

            # Extract images
            favicon_data = thumbnail_data = None
            try:
                images = await result_div.query_selector_all('img')
                valid_images = []
                for img in images:
                    src = await img.get_attribute('src')
                    if src and src.startswith('data:image/'):
                        valid_images.append(src)
                if valid_images:
                    favicon_data = valid_images[0]
                    if len(valid_images) > 1:
                        thumbnail_data = valid_images[1]
            except Exception:
                pass

            rating_metadata = await _extract_rating(result_div)
            metadata = None
            if rating_metadata or thumbnail_data:
                metadata = SearchMetadata(rating=rating_metadata, thumbnail=thumbnail_data)

            results.append(SearchResult(
                link=href,
                title=title,
                snippet='\n'.join(snippet_texts),
                favicon=favicon_data,
                metadata=metadata,
            ))
        except Exception:
            continue

    return results


async def _parse_news_results(
    page: Page, results: List[NewsResult], seen_links: set, count: int,
) -> List[NewsResult]:
    """Parse news results from the current Google search page."""
    result_divs = await page.query_selector_all('div[data-news-doc-id]')

    for result_div in result_divs:
        if len(results) >= count:
            break
        try:
            link_element = await result_div.query_selector('a[href]')
            if not link_element:
                continue

            href = await link_element.get_attribute('href')
            if not _is_valid_result_link(href):
                continue

            if href in seen_links:
                continue
            seen_links.add(href)

            title_element = await result_div.query_selector('div[role="heading"]')
            title = await title_element.inner_text() if title_element else ""

            snippet = await result_div.evaluate(
                '(element) => {'
                '  const heading = element.querySelector("div[role=heading]");'
                '  if (heading && heading.nextElementSibling) {'
                '    return heading.nextElementSibling.innerText;'
                '  }'
                '  return "";'
                '}'
            )

            # Extract images
            thumbnail_data = None
            favicon_data = None
            
            images = await result_div.query_selector_all('img[src]')
            for img in images:
                src = await img.get_attribute('src')
                if not src:
                    continue
                # First image is usually the thumbnail
                if not thumbnail_data:
                    thumbnail_data = src
                elif not favicon_data:
                    favicon_data = src
            
            results.append(NewsResult(
                link=href,
                title=title,
                snippet=snippet or "",
                favicon=favicon_data,
                thumbnail=thumbnail_data,
            ))
        except Exception:
            continue

    return results


async def _parse_image_results(
    page: Page, results: List[MediaResult], seen_links: set, count: int,
) -> List[MediaResult]:
    """Parse image results from the current Google search page."""
    result_divs = await page.query_selector_all('div[data-lpage]')

    for result_div in result_divs:
        if len(results) >= count:
            break
        try:
            link = await result_div.get_attribute('data-lpage')
            if not link or not _is_valid_result_link(link):
                continue

            if link in seen_links:
                continue
            seen_links.add(link)

            # Find the img element deep down
            img_element = await result_div.query_selector('img[src]')
            if not img_element:
                continue

            icon = await img_element.get_attribute('src')
            title = await img_element.get_attribute('alt') or ""

            results.append(MediaResult(
                link=link,
                title=title,
                icon=icon,
            ))
        except Exception:
            continue

    return results


async def _parse_video_results(
    page: Page, results: List[MediaResult], seen_links: set, count: int,
) -> List[MediaResult]:
    """Parse video results from the current Google search page."""
    result_divs = await page.query_selector_all('div[data-vid]')

    for result_div in result_divs:
        if len(results) >= count:
            break
        try:
            link_element = await result_div.query_selector('a[href]')
            if not link_element:
                continue
            
            link = await link_element.get_attribute('href')
            if not link or not _is_valid_result_link(link):
                continue

            if link in seen_links:
                continue
            seen_links.add(link)

            title_element = await result_div.query_selector('h3')
            title = await title_element.inner_text() if title_element else ""

            icon_element = await result_div.query_selector('img[src]')
            icon = await icon_element.get_attribute('src') if icon_element else None

            tags_data = await result_div.evaluate(
                '(el) => {'
                '  const cite = el.querySelector("cite");'
                '  if (!cite) return null;'
                '  const text = cite.closest("span")?.parentElement?.innerText || cite.innerText;'
                '  if (!text) return null;'
                '  const clean = text.split("·").map(t => t.trim()).filter(t => t);'
                '  if (clean.length === 0) return null;'
                '  if (clean.length === 1) return { source: clean[0], author: null, date: null };'
                '  if (clean.length === 2) return { source: clean[0], author: null, date: clean[1] };'
                '  return { source: clean[0], author: clean[1], date: clean[2] };'
                '}'
            )
            tags = MediaTags(**tags_data) if tags_data else None

            results.append(MediaResult(
                link=link,
                title=title,
                icon=icon,
                tags=tags,
            ))
        except Exception:
            continue

    return results


# ==================== Endpoint ====================

@router.post("/search")
async def search(request: SearchRequest, page: PageDep) -> SearchResponse:
    """
    Search the web using Google and return structured results.

    Navigates to Google, waits ``idle`` seconds, then parses up to ``count``
    results across up to ``max_pages`` pages. Includes optional wiki panel data.
    """
    try:
        await page.goto(
            f"https://www.google.com/search?q={request.query}&num={request.count}",
            wait_until="commit",
        )
        await asyncio.sleep(request.idle)

        results: List[SearchResult] = []
        seen_links: set = set()

        ai_review = await _extract_ai_review(page)
        wiki_result = await _extract_wiki(page)
        results = await _parse_search_results(page, results, seen_links, request.count)

        # Pagination
        current_page = 1
        while len(results) < request.count and current_page < request.max_pages:
            next_button = await page.query_selector('a#pnnext')
            if not next_button:
                logger.info(f"No next page after page {current_page}. Got {len(results)} results.")
                break

            await next_button.click()
            await asyncio.sleep(1)
            current_page += 1
            previous_count = len(results)
            results = await _parse_search_results(page, results, seen_links, request.count)

            if len(results) == previous_count:
                logger.info(f"No new results on page {current_page}. Stopping.")
                break
            logger.info(f"Page {current_page}: {len(results)}/{request.count} results")

        return SearchResponse(ai_review=ai_review, wiki=wiki_result, results=results)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to search: {str(e)}")


@router.post("/search_news")
async def search_news(request: SearchRequest, page: PageDep) -> NewsResponse:
    """
    Search for news using Google and return structured results.
    """
    try:
        await page.goto(
            f"https://www.google.com/search?q={request.query}&num={request.count}&tbm=nws",
            wait_until="commit",
        )
        await asyncio.sleep(request.idle)

        results: List[NewsResult] = []
        seen_links: set = set()

        results = await _parse_news_results(page, results, seen_links, request.count)

        # Pagination
        current_page = 1
        while len(results) < request.count and current_page < request.max_pages:
            next_button = await page.query_selector('a#pnnext')
            if not next_button:
                logger.info(f"No next page after page {current_page}. Got {len(results)} results.")
                break

            await next_button.click()
            await asyncio.sleep(1)
            current_page += 1
            previous_count = len(results)
            results = await _parse_news_results(page, results, seen_links, request.count)

            if len(results) == previous_count:
                logger.info(f"No new results on page {current_page}. Stopping.")
                break
            logger.info(f"Page {current_page}: {len(results)}/{request.count} results")

        return NewsResponse(results=results)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to search news: {str(e)}")


@router.post("/search_images")
async def search_images(request: SearchRequest, page: PageDep) -> ImagesResponse:
    """
    Search for images using Google and return structured results.
    """
    try:
        scroll_y = 1000
        await page.goto(
            f"https://www.google.com/search?q={request.query}&num={request.count}&udm=2",
            wait_until="commit",
        )
        await asyncio.sleep(request.idle)

        results: List[MediaResult] = []
        seen_links: set = set()

        results = await _parse_image_results(page, results, seen_links, request.count)

        # Pagination (Google Images usually has infinite scroll)
        current_scroll = 0
        while len(results) < request.count and current_scroll < request.max_pages: # reusing max_pages as scroll attempts
            await page.evaluate(f"window.scrollBy(0, {scroll_y})")
            await asyncio.sleep(1)
            current_scroll += 1
            
            previous_count = len(results)
            results = await _parse_image_results(page, results, seen_links, request.count)
            
            if len(results) == previous_count:
                break

        return ImagesResponse(results=results)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to search images: {str(e)}")


@router.post("/search_hints")
async def search_hints(request: SearchHintsRequest, page: PageDep) -> SearchHintsResponse:
    """
    Get Google autocomplete suggestions (search hints) for a query.

    Navigates to Google, types the query into the search box, waits for
    autocomplete suggestions to appear, and extracts them.
    """
    try:
        await page.goto("https://www.google.com", wait_until="commit")
        await asyncio.sleep(request.idle)

        search_input = page.locator('textarea[name="q"], input[name="q"]').first
        await search_input.click()
        await search_input.fill(request.query)

        await asyncio.sleep(request.wait)

        hints = await page.evaluate("""
            () => {
                const hints = [];

                const options = document.querySelectorAll('[role="listbox"] [role="option"]');
                for (const opt of options) {
                    const label = opt.getAttribute('aria-label');
                    if (label) {
                        hints.push(label);
                        continue;
                    }
                    const spans = opt.querySelectorAll('span');
                    const texts = [];
                    for (const span of spans) {
                        if (span.closest('[role="img"]') || span.querySelector('span')) continue;
                        const t = span.textContent.trim();
                        if (t && !texts.includes(t)) texts.push(t);
                    }
                    if (texts.length > 0) {
                        hints.push(texts.join(' '));
                    }
                }

                if (hints.length > 0) return hints;

                const datalist = document.querySelector('datalist');
                if (datalist) {
                    for (const opt of datalist.querySelectorAll('option')) {
                        const val = opt.getAttribute('value') || opt.textContent.trim();
                        if (val) hints.push(val);
                    }
                }

                return hints;
            }
        """)

        return SearchHintsResponse(query=request.query, hints=hints)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get search hints: {str(e)}")


@router.post("/search_videos")
async def search_videos(request: SearchRequest, page: PageDep) -> VideosResponse:
    """
    Search for videos using Google and return structured results.
    """
    try:
        scroll_y = 1000
        await page.goto(
            f"https://www.google.com/search?q={request.query}&num={request.count}&udm=7",
            wait_until="commit",
        )
        await asyncio.sleep(request.idle)

        results: List[MediaResult] = []
        seen_links: set = set()

        results = await _parse_video_results(page, results, seen_links, request.count)

        # Pagination (Google Videos usually has infinite scroll)
        current_scroll = 0
        while len(results) < request.count and current_scroll < request.max_pages: # reusing max_pages as scroll attempts
            await page.evaluate(f"window.scrollBy(0, {scroll_y})")
            await asyncio.sleep(1)
            current_scroll += 1
            
            previous_count = len(results)
            results = await _parse_video_results(page, results, seen_links, request.count)
            
            if len(results) == previous_count:
                break

        return VideosResponse(results=results)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to search videos: {str(e)}")
