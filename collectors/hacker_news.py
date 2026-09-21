import httpx
import asyncio
import time
from bs4 import BeautifulSoup
from collectors.base import BaseCollector
from config import MAX_POSTS_PER_PLATFORM, MAX_CONCURRENT_SCRAPES, JUNK_PATTERNS

# Maximum bytes of HTML we are willing to parse from an external page.
MAX_PAGE_BYTES = 2_000_000


class HackerNewsCollector(BaseCollector):
    """
    Collector for Hacker News (Y Combinator).

    Architecture Note:
    Hacker News API only provides links to external websites. This collector acts as a
    mini web-crawler, visiting each external link concurrently to scrape the actual
    readable text for our NLP engine.

    Discovery uses the Algolia HN search index rather than the global `topstories` feed:
    topstories is subject-agnostic, so we were paying for 50 external crawls and then
    discarding most of them at the AI filter. Algolia lets us ask for AI stories above a
    score threshold directly, and returns full story records in one request each.
    `topstories` remains as a fallback if the search index is unreachable.
    """

    SEARCH_QUERIES = ["LLM", "AI model", "GPT", "machine learning", "AI agents"]

    def __init__(self):
        super().__init__("Hacker News")
        self.top_stories_url = "https://hacker-news.firebaseio.com/v0/topstories.json"
        self.item_url = "https://hacker-news.firebaseio.com/v0/item/{}.json"
        self.search_url = "https://hn.algolia.com/api/v1/search"
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_SCRAPES)

    async def scrape_external_link(self, client, url):
        """
        Crawls an external website and extracts clean, readable text.

        Args:
            client (httpx.AsyncClient): The shared async HTTP client.
            url (str): The external target URL.

        Returns:
            str: Sanitized text content, stripped of HTML structure.
        """
        # Skip self-referential HN discussion pages (they lack external article content)
        if not url or "news.ycombinator.com" in url:
            return ""

        try:
            # Strict timeout of 5.0 seconds to prevent slow external servers
            # from degrading our system's overall performance.
            # follow_redirects is explicit: a large share of submitted links are 301s,
            # and httpx does not follow them by default.
            async with self.semaphore:
                resp = await client.get(url, timeout=5.0, follow_redirects=True)

            if resp.status_code != 200:
                return ""

            # Only parse HTML. Feeding a PDF or an image to BeautifulSoup yields
            # binary garbage that then lands in the database as "content".
            content_type = resp.headers.get('content-type', '').lower()
            if 'html' not in content_type:
                return ""
            if len(resp.content) > MAX_PAGE_BYTES:
                return ""

            # Parse the raw HTML into a DOM tree
            soup = BeautifulSoup(resp.text, 'html.parser')

            # Strip out functional/visual elements that pollute NLP analysis
            for s in soup(["script", "style", "nav", "footer", "header",
                           "aside", "form", "noscript"]):
                s.decompose()

            # Prefer the semantic main-content container. Falling back to whole-page
            # text mixes menus and cookie banners into the article body.
            main = soup.find('article') or soup.find('main')
            if main is not None:
                text = main.get_text(separator=' ')
            else:
                paragraphs = soup.find_all('p')
                text = ' '.join(p.get_text(separator=' ') for p in paragraphs) \
                    if paragraphs else soup.get_text(separator=' ')

            text = ' '.join(text.split())

            # Bot walls and consent screens return HTTP 200 with no article behind them
            lowered = text[:400].lower()
            if any(pattern in lowered for pattern in JUNK_PATTERNS):
                return ""

            # Truncate to 2000 characters to save memory
            return text[:2000]
        except Exception:
            # External scraping is volatile; fail silently if a website blocks us
            return ""

    async def process_hit(self, client, hit):
        """
        Pipeline for a single Algolia search hit: crawling the linked page and
        normalizing the extracted data.
        """
        try:
            sid = hit.get('objectID')
            url = hit.get('url') or ''
            title = hit.get('title') or ''

            # Trigger the external crawler
            external_text = await self.scrape_external_link(client, url)

            # Ask HN / Show HN text posts have no external URL; use their own body.
            content = external_text or hit.get('story_text') or hit.get('comment_text') or title

            post = {
                'source_platform': self.platform_name,
                'external_id': str(sid),
                'title': title,
                'content': content,
                'author': hit.get('author', 'unknown'),
                'url': url if url else f"https://news.ycombinator.com/item?id={sid}",
                'raw_score': hit.get('points', 0) or 0,
                'sentiment': self.analyze_sentiment(content),
                'published_at': hit.get('created_at', '')
            }

            if self.is_quality_content(post):
                return post

        except Exception:
            # Ignore individual story failures to maintain batch integrity
            pass

        return None

    async def search_stories(self, client, query, cutoff):
        """Queries the Algolia HN index for recent, well-received stories."""
        params = {
            "query": query,
            "tags": "story",
            "numericFilters": f"points>=10,created_at_i>{cutoff}",
            "hitsPerPage": 30,
        }
        try:
            resp = await client.get(self.search_url, params=params, timeout=10.0)
            if resp.status_code != 200:
                return []
            return resp.json().get('hits', [])
        except Exception:
            return []

    async def process_story(self, client, sid):
        """
        Fallback pipeline used when the Algolia index is unavailable: resolves a story
        ID through the Firebase API, then crawls the target.
        """
        try:
            item_res = await client.get(self.item_url.format(sid))
            if item_res.status_code != 200:
                return None

            item = item_res.json()
            if not item:
                return None

            url = item.get('url', '')
            external_text = await self.scrape_external_link(client, url)
            content = external_text if external_text else item.get('text', item.get('title'))

            post = {
                'source_platform': self.platform_name,
                'external_id': str(sid),
                'title': item.get('title', ''),
                'content': content,
                'author': item.get('by', 'unknown'),
                'url': url if url else f"https://news.ycombinator.com/item?id={sid}",
                'raw_score': item.get('score', 0),
                'sentiment': self.analyze_sentiment(content),
                'published_at': item.get('time', '')
            }

            if self.is_quality_content(post):
                return post
        except Exception:
            pass

        return None

    async def collect_fallback(self, client):
        """Original topstories path, retained for when Algolia is unreachable."""
        print(f"   ⚠️  {self.platform_name}: falling back to topstories feed.")
        response = await client.get(self.top_stories_url)
        if response.status_code != 200:
            return []
        story_ids = response.json()[:MAX_POSTS_PER_PLATFORM]
        results = await asyncio.gather(*[self.process_story(client, sid) for sid in story_ids])
        return [post for post in results if post is not None]

    async def collect(self, client: httpx.AsyncClient):
        """Main entry point. Searches for AI stories and dispatches concurrent crawlers."""
        print(f"--- {self.platform_name}: Crawling External Stories Concurrently... ---")
        try:
            cutoff = int(time.time()) - 7 * 86400

            search_results = await asyncio.gather(
                *[self.search_stories(client, q, cutoff) for q in self.SEARCH_QUERIES]
            )

            # Merge the queries, deduplicating stories matched by more than one of them.
            unique_hits = {}
            for hits in search_results:
                for hit in hits:
                    unique_hits.setdefault(hit.get('objectID'), hit)

            if not unique_hits:
                return await self.collect_fallback(client)

            hits = sorted(
                unique_hits.values(),
                key=lambda h: h.get('points', 0) or 0,
                reverse=True
            )[:MAX_POSTS_PER_PLATFORM]

            # Concurrency implementation: Dispatch a crawler task for each story
            tasks = [self.process_hit(client, hit) for hit in hits]

            # Wait for all concurrent crawlers to return their payloads
            results = await asyncio.gather(*tasks)

            # Clean out the None values returned by failed/rejected stories
            return [post for post in results if post is not None]

        except Exception as e:
            print(f"Critical Error in Hacker News Collector orchestration: {e}")
            return []
