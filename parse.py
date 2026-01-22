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

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class SessionPool:
    """Pool of persistent browser sessions for reuse."""
    
    def __init__(self, client: httpx.AsyncClient, api_base: str, browser_uid: str, size: int):
        self.client = client
        self.api_base = api_base
        self.browser_uid = browser_uid
        self.size = size
        self._available: asyncio.Queue[str] = asyncio.Queue()
        self._all_sessions: List[str] = []
        self._headers: Dict[str, str] = {}
        if browser_uid != "default":
            self._headers["X-Browser-Id"] = browser_uid
    
    async def initialize(self):
        """Create all sessions in the pool."""
        logger.info(f"Initializing session pool with {self.size} sessions...")
        for i in range(self.size):
            try:
                resp = await self.client.post(
                    f"{self.api_base}/start_session",
                    headers=self._headers,
                    timeout=30.0
                )
                if resp.status_code == 200:
                    session_data = resp.json()
                    session_id = session_data["session_id"]
                    self._all_sessions.append(session_id)
                    await self._available.put(session_id)
                    logger.info(f"Created session {i+1}/{self.size}: {session_id[:8]}...")
                else:
                    logger.error(f"Failed to create session {i+1}: {resp.text}")
            except Exception as e:
                logger.error(f"Error creating session {i+1}: {e}")
        
        logger.info(f"Session pool ready with {len(self._all_sessions)} sessions")
    
    async def acquire(self) -> str:
        """Acquire a session from the pool (blocks until one is available)."""
        return await self._available.get()
    
    async def release(self, session_id: str):
        """Return a session to the pool for reuse."""
        await self._available.put(session_id)
    
    def get_headers(self, session_id: str) -> Dict[str, str]:
        """Get headers for a request with the given session."""
        headers = self._headers.copy()
        headers["X-Session-Id"] = session_id
        return headers
    
    async def shutdown(self):
        """Close all sessions in the pool."""
        logger.info("Shutting down session pool...")
        for session_id in self._all_sessions:
            try:
                headers = self.get_headers(session_id)
                await self.client.delete(
                    f"{self.api_base}/end_session",
                    headers=headers,
                    timeout=10.0
                )
                logger.info(f"Closed session {session_id[:8]}...")
            except Exception as e:
                logger.warning(f"Error closing session {session_id[:8]}: {e}")
        self._all_sessions.clear()
        logger.info("Session pool shutdown complete")


class Crawler:
    def __init__(self, home_url: str, depth: int, browser_uid: str, parallel: int, openai_key: str, api_base: str, output_file: str, openai_base_url: str = None):
        self.home_url = home_url
        self.max_depth = depth
        self.browser_uid = browser_uid
        self.parallel = parallel
        self.api_base = api_base
        self.output_file = output_file
        self.domain = urlparse(home_url).netloc
        self.visited_urls: Set[str] = set()
        self.results: List[Dict[str, Any]] = []
        self.session_pool: SessionPool = None
        self._file_lock = asyncio.Lock()
        
        # Initialize OpenAI client
        if not openai_key:
            raise ValueError("OPENAI_API_KEY is required")
        
        self.openai = AsyncOpenAI(
            api_key=openai_key,
            base_url=openai_base_url
        )
        
        # Initialize output file with empty array if it doesn't exist or is invalid
        self._init_output_file()

    def _init_output_file(self):
        """Initialize output file - load existing results or create empty array."""
        try:
            if os.path.exists(self.output_file):
                with open(self.output_file, 'r') as f:
                    existing = json.load(f)
                    if isinstance(existing, list):
                        self.results = existing
                        logger.info(f"Loaded {len(self.results)} existing results from {self.output_file}")
                        return
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Could not load existing results: {e}")
        
        # Create new empty file
        with open(self.output_file, 'w') as f:
            json.dump([], f)
        logger.info(f"Initialized empty output file: {self.output_file}")

    async def _save_result(self, result: Dict[str, Any]):
        """Save a single result to the output file with locking."""
        async with self._file_lock:
            self.results.append(result)
            try:
                with open(self.output_file, 'w') as f:
                    json.dump(self.results, f, ensure_ascii=False, indent=2)
                logger.debug(f"Saved result to {self.output_file} (total: {len(self.results)})")
            except IOError as e:
                logger.error(f"Failed to save result: {e}")

    def is_same_domain(self, url: str) -> bool:
        return urlparse(url).netloc == self.domain

    def normalize_url(self, url: str) -> str:
        # Remove fragments and query parameters for deduplication
        # But keep path!
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    async def get_page_links(self, url: str) -> Set[str]:
        """Extract all same-domain links from a page using a pooled session."""
        logger.info(f"Extracting links from {url}")
        
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
            "timeout": 60000
        }

        session_id = await self.session_pool.acquire()
        try:
            headers = self.session_pool.get_headers(session_id)
            resp = await self.client.post(
                f"{self.api_base}/selectors", 
                json=payload, 
                headers=headers, 
                timeout=1200.0
            )
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
        finally:
            await self.session_pool.release(session_id)

    async def extract_product_data(self, url: str) -> Dict[str, Any]:
        """Scroll page, get text, and parse with OpenAI using a pooled session."""
        logger.info(f"Processing product data from {url}")
        
        # 1. Get page text with scrolling
        payload = {
            "url": url,
            "actions": [
                {
                    "action": "scroll_to_bottom",
                    "step_pixels": 500,
                    "step_delay": 1.5,
                    "timeout": 10
                },
                {
                    "action": "text"
                }
            ]
        }

        text_content = ""
        session_id = await self.session_pool.acquire()
        try:
            headers = self.session_pool.get_headers(session_id)
            interact_resp = await self.client.post(
                f"{self.api_base}/interact", 
                json=payload, 
                headers=headers, 
                timeout=60.0
            )
            if interact_resp.status_code == 200:
                ctype = interact_resp.headers.get("Content-Type", "")
                if "application/json" not in ctype:
                    text_content = interact_resp.text
            else:
                logger.error(f"Failed to interact with {url}: {interact_resp.text}")
        except Exception as e:
            logger.error(f"Error scraping {url}: {e}")
            return {}
        finally:
            await self.session_pool.release(session_id)

        if not text_content:
            return {}

        # 2. Extract with OpenAI
        try:
            completion = await self.openai.chat.completions.create(
                model="google/gemini-2.5-flash-lite", # Use a cheaper/faster model for basic extraction
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
            
            # Save immediately to backup file
            await self._save_result(data)
            
            return data
            
        except Exception as e:
            logger.error(f"OpenAI extraction failed for {url}: {e}")
            return {}

    async def crawl(self):
        async with httpx.AsyncClient(timeout=30.0) as client:
            self.client = client
            
            # Initialize the session pool
            self.session_pool = SessionPool(
                client=client,
                api_base=self.api_base,
                browser_uid=self.browser_uid,
                size=self.parallel
            )
            await self.session_pool.initialize()
            
            try:
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
                        crawl_tasks.append(self.extract_product_data(url))

                    # Run extraction in parallel (limited by session pool size)
                    # Results are saved incrementally in extract_product_data via _save_result
                    results = await tqdm_asyncio.gather(*crawl_tasks, desc=f"Scanning depth {d+1}")
                    
                    for res in results:
                        if res:
                            logger.info(f"Found product: {res.get('name', 'Unknown')}")

                    # If we need to go deeper, extract links from all current pages
                    if d < self.max_depth - 1:
                        logger.info("Extracting links for next level...")
                        link_tasks = [self.get_page_links(url) for url in current_urls]
                        link_sets = await tqdm_asyncio.gather(*link_tasks, desc="Extracting links")
                        
                        for links in link_sets:
                            for link in links:
                                if link not in self.visited_urls:
                                    self.visited_urls.add(link)
                                    next_urls.add(link)
                        
                        current_urls = next_urls
                        if not current_urls:
                            break
            finally:
                # Always cleanup sessions
                await self.session_pool.shutdown()


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
        output_file=args.output,
        openai_base_url=args.openai_base
    )
    
    await crawler.crawl()
    logger.info(f"Crawl complete. {len(crawler.results)} results saved to {args.output}")

if __name__ == "__main__":
    # Ensure dependencies are installed:
    # pip install httpx openai tqdm
    asyncio.run(main())

