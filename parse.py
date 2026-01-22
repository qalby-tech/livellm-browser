import asyncio
import httpx
import argparse
import json
import logging
import os
from urllib.parse import urlparse, urljoin
from typing import Set, List, Dict, Any
from openai import AsyncOpenAI
from tqdm.asyncio import tqdm_asyncio
import tqdm

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class Crawler:
    def __init__(self, home_url: str, depth: int, browser_uid: str, parallel: int, openai_key: str, api_base: str, openai_base_url: str = None):
        self.home_url = home_url
        self.max_depth = depth
        self.browser_uid = browser_uid
        self.parallel = parallel
        self.api_base = api_base
        self.domain = urlparse(home_url).netloc
        self.visited_urls: Set[str] = set()
        self.results: List[Dict[str, Any]] = []
        
        # Initialize OpenAI client
        if not openai_key:
            raise ValueError("OPENAI_API_KEY is required")
        
        self.openai = AsyncOpenAI(
            api_key=openai_key,
            base_url=openai_base_url
        )
        
        # Semaphore for parallel requests
        self.sem = asyncio.Semaphore(parallel)

    def is_same_domain(self, url: str) -> bool:
        return urlparse(url).netloc == self.domain

    def normalize_url(self, url: str) -> str:
        # Remove fragments and query parameters for deduplication
        # But keep path!
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    async def get_page_links(self, client: httpx.AsyncClient, url: str) -> Set[str]:
        """Extract all same-domain links from a page."""
        logger.info(f"Extracting links from {url}")
        
        # Use execute_script instead of selectors to get all links at once efficiently
        # This avoids transferring large JSONs if we filter client-side
        # Actually, let's keep using selectors but optimize the filter
        
        payload = {
            "url": url,
            "selectors": [
                {
                    "name": "links",
                    "type": "css",
                    "value": "a",
                    "actions": [{"action": "attribute", "name": "href"}]
                }
            ],
            "wait_until": "commit",
            "timeout": 60000 # Increased timeout
        }

        # Create ad-hoc session for extraction (or use persistent browser)
        headers = {}
        if self.browser_uid != "default":
             headers["X-Browser-Id"] = self.browser_uid

        try:
            # Increased read timeout for client
            resp = await client.post(f"{self.api_base}/selectors", json=payload, headers=headers, timeout=1200.0)
            if resp.status_code != 200:
                logger.error(f"Failed to get links from {url}: {resp.text}")
                return set()
            
            data = resp.json()
            links = set()
            
            # Parse selector results
            for selector in data:
                if selector["name"] == "links":
                    for result in selector["results"]:
                        for href in result["values"]:
                            if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
                                continue
                            
                            # Handle relative paths
                            full_url = urljoin(url, href)
                            
                            if self.is_same_domain(full_url):
                                normalized = self.normalize_url(full_url)
                                links.add(normalized)
                                
            return links
        except Exception as e:
            logger.error(f"Error getting links from {url}: {e}")
            return set()

    async def extract_product_data(self, client: httpx.AsyncClient, url: str) -> Dict[str, Any]:
        """Scroll page, get text, and parse with OpenAI."""
        logger.info(f"Processing product data from {url}")
        
        # 1. Get page text with scrolling
        payload = {
            "url": url,
            "actions": [
                {
                    "action": "scroll_to_bottom",
                    "step_pixels": 500,
                    "step_delay": 1.5, # Faster scroll for speed
                    "timeout": 10      # Max 10s scroll
                },
                {
                    "action": "text"
                }
            ]
        }
        
        headers = {}
        if self.browser_uid != "default":
             headers["X-Browser-Id"] = self.browser_uid

        text_content = ""
        try:
            async with self.sem: # Limit concurrent browser sessions
                # Start a dedicated session (tab) to ensure isolation
                start_resp = await client.post(f"{self.api_base}/start_session", headers=headers, timeout=10.0)
                if start_resp.status_code != 200:
                    logger.error(f"Failed to start session: {start_resp.text}")
                    return {}
                session_data = start_resp.json()
                session_id = session_data["session_id"]
                
                req_headers = headers.copy()
                req_headers["X-Session-Id"] = session_id
                
                try:
                    interact_resp = await client.post(f"{self.api_base}/interact", json=payload, headers=req_headers, timeout=60.0)
                    if interact_resp.status_code == 200:
                        # Response can be text or json depending on accept header, 
                        # but the endpoint returns plain text for "text" action if it's the only one returning content?
                        # Actually /interact returns text/plain if only one content result, or json if mixed.
                        # Let's read based on content-type
                        ctype = interact_resp.headers.get("Content-Type", "")
                        if "application/json" in ctype:
                            data = interact_resp.json()
                            # Try to find the text result
                            # NOTE: The current API implementation for /interact returns text/plain directly 
                            # if a content result is found (lines 958-959 in main.py). 
                            # If not, it returns JSON status.
                            # Wait, looking at main.py:
                            # if content_result is not None: return Response(content=content_result, media_type="text/plain")
                            # So if we asked for text, we get raw text.
                            pass
                        else:
                            text_content = interact_resp.text
                    else:
                        logger.error(f"Failed to interact with {url}: {interact_resp.text}")
                finally:
                        # Cleanup session
                        await client.delete(f"{self.api_base}/end_session", headers=req_headers, timeout=10.0)
        
        except Exception as e:
            logger.error(f"Error scraping {url}: {e}")
            return {}

        if not text_content:
            return {}

        # 2. Extract with OpenAI
        try:
            completion = await self.openai.chat.completions.create(
                model="gpt-4o-mini", # Use a cheaper/faster model for basic extraction
                messages=[
                    {
                        "role": "system", 
                        "content": """You are a product data extractor. Extract the following fields from the provided text into a JSON object:
- name: Product name
- price: Price (number or string with currency)
- metadata: Any other relevant technical specs or details
If the text does not contain product information, return an empty JSON object {}."""
                    },
                    {"role": "user", "content": text_content[:30_000]} # Truncate to avoid token limits
                ],
                response_format={"type": "json_object"}
            )
            
            result_text = completion.choices[0].message.content
            data = json.loads(result_text)
            
            # Filter out empty results
            if not data or (not data.get("name") and not data.get("price")):
                return {}
                
            data["url"] = url
            return data
            
        except Exception as e:
            logger.error(f"OpenAI extraction failed for {url}: {e}")
            return {}

    async def crawl(self):
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Level 0: Home URL
            current_urls = {self.home_url}
            self.visited_urls.add(self.home_url)
            
            for d in range(self.max_depth):
                logger.info(f"--- Depth {d+1} (Processing {len(current_urls)} URLs) ---")
                
                next_urls = set()
                crawl_tasks = []
                
                # Process current level URLs
                for url in current_urls:
                    # Task 1: Parse data from this URL
                    crawl_tasks.append(self.extract_product_data(client, url))
                    
                    # Task 2: If we are not at max depth, find more links
                    if d < self.max_depth - 1:
                        # We need to await this to build the next level
                        # Ideally we do this in parallel too, but for simplicity let's do it per batch
                        pass

                # Run extraction in parallel
                # results = await asyncio.gather(*crawl_tasks)
                results = await tqdm_asyncio.gather(*crawl_tasks, desc=f"Scanning depth {d+1}")
                
                for res in results:
                    if res:
                        self.results.append(res)
                        logger.info(f"Found product: {res.get('name', 'Unknown')}")

                # If we need to go deeper, extract links from all current pages
                if d < self.max_depth - 1:
                    logger.info("Extracting links for next level...")
                    link_tasks = [self.get_page_links(client, url) for url in current_urls]
                    link_sets = await tqdm_asyncio.gather(*link_tasks, desc="Extracting links")
                    
                    for links in link_sets:
                        for link in links:
                            if link not in self.visited_urls:
                                self.visited_urls.add(link)
                                next_urls.add(link)
                    
                    current_urls = next_urls
                    if not current_urls:
                        break

    def save_results(self, filename: str):
        with open(filename, 'w') as f:
            json.dump(self.results, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved {len(self.results)} items to {filename}")

async def main():
    parser = argparse.ArgumentParser(description="Parallel Crawler with LLM Extraction")
    parser.add_argument("home_url", help="Starting URL")
    parser.add_argument("--depth", type=int, default=2, help="Crawling depth (1 = just home, 2 = home + links)")
    parser.add_argument("--browser_uid", default="default", help="Browser profile UID")
    parser.add_argument("--parallel", type=int, default=5, help="Number of parallel tabs")
    parser.add_argument("--output", default="result.json", help="Output file")
    parser.add_argument("--api_base", default="http://localhost:8000", help="API Base URL")
    parser.add_argument("--openai_base", default=None, help="OpenAI Base URL")
    parser.add_argument("--openai_key", default=None, help="OpenAI API Key (overrides env var)")
    
    args = parser.parse_args()
    
    api_key = args.openai_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("Please set OPENAI_API_KEY environment variable or use --openai_key")
        return

    crawler = Crawler(
        home_url=args.home_url,
        depth=args.depth,
        browser_uid=args.browser_uid,
        parallel=args.parallel,
        openai_key=api_key,
        api_base=args.api_base,
        openai_base_url=args.openai_base
    )
    
    await crawler.crawl()
    crawler.save_results(args.output)

if __name__ == "__main__":
    # Ensure dependencies are installed:
    # pip install httpx openai tqdm
    asyncio.run(main())

