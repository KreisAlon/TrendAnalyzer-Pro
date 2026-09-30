import httpx
import asyncio
import time
from bs4 import BeautifulSoup
from collectors.base import BaseCollector
from config import (
    MAX_POSTS_PER_PLATFORM, TEXT_PREVIEW_LENGTH,
    MAX_CONCURRENT_SCRAPES, JUNK_PATTERNS, DEFAULT_HN_QUERIES,
)

# Maximum bytes of HTML we are willing to parse from an external page.
MAX_PAGE_BYTES = 2_000_000

# Hits requested per Algolia query. One request either way, so we ask for a deep page
# and let the gatekeeper consume as much of it as it needs.
ALGOLIA_HITS_PER_PAGE = 60

# A semantic container holding less than this is a teaser, not an article body, so the
# <p> extraction is allowed to replace it.
THIN_CONTAINER_CHARS = 600


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
    There is deliberately NO fallback to `topstories`: when the search finds nothing,
    the honest answer is zero posts, not the front page of an unrelated subject.
    """

    # Curated fallback. The live queries come from the run's TopicProfile via
    # search_queries(); this list is what a run without a plan searches for, and
    # keeping it here means losing the planner costs adaptivity, not discovery.
    SEARCH_QUERIES = DEFAULT_HN_QUERIES

    def search_queries(self):
        """Free-text Algolia queries for this run: the plan, or the fallback."""
        return self.planned('hn_queries', self.SEARCH_QUERIES)

    def __init__(self):
        super().__init__("Hacker News")
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

            def flat(node):
                return ' '.join(node.get_text(separator=' ').split()) if node is not None else ''

            # Prefer the semantic main-content container: falling back to whole-page text
            # mixes menus and cookie banners into the article body.
            preferred = flat(soup.find('article')) or flat(soup.find('main'))

            paragraphs = soup.find_all('p')
            fallback = ' '.join(flat(p) for p in paragraphs) if paragraphs \
                else flat(soup)

            # The container is only overridden when it holds a teaser rather than the
            # body -- some sites put a summary in <article> and the real text in sibling
            # <p> tags, which cost us most of the article. Preferring the *longer* text
            # unconditionally would be worse: on a GitHub link, <main> wraps the README
            # plus the entire sidebar, and the sidebar would win.
            if preferred and (len(preferred) >= THIN_CONTAINER_CHARS
                              or len(preferred) >= len(fallback)):
                text = preferred
            else:
                text = fallback

            # Bot walls and consent screens return HTTP 200 with no article behind them
            lowered = text[:400].lower()
            if any(pattern in lowered for pattern in JUNK_PATTERNS):
                return ""

            # Cut on the shared TEXT_PREVIEW_LENGTH budget instead of the old hardcoded
            # 2000, which was truncating every scraped article mid-word (the stored rows
            # all ended at exactly 2000 characters) and starving the NLP stage.
            return self.truncate_at_boundary(text, TEXT_PREVIEW_LENGTH)
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
            "hitsPerPage": ALGOLIA_HITS_PER_PAGE,
        }
        try:
            resp = await client.get(self.search_url, params=params, timeout=10.0)
            if resp.status_code != 200:
                return []
            payload = resp.json()
            # nbHits: every story matching this query and the same filters (last 7
            # days, 10+ points), not just the page returned.
            if isinstance(payload.get('nbHits'), int):
                self.search_totals.append(payload['nbHits'])
            return payload.get('hits', [])
        except Exception:
            return []

    async def collect(self, client: httpx.AsyncClient):
        """Main entry point. Searches for AI stories and dispatches concurrent crawlers."""
        topic_label = getattr(self.topic_profile, 'target_topic', 'AI')
        print(f"--- {self.platform_name}: Crawling '{topic_label}' stories "
              f"concurrently... ---")
        try:
            cutoff = int(time.time()) - 7 * 86400

            queries = self.search_queries()
            if not queries:
                return []
            search_results = await asyncio.gather(
                *[self.search_stories(client, q, cutoff) for q in queries]
            )

            # Merge the queries, deduplicating stories matched by more than one of them.
            self.record_candidates(search_results, 'objectID')
            unique_hits = {}
            for hits in search_results:
                for hit in hits:
                    unique_hits.setdefault(hit.get('objectID'), hit)

            if not unique_hits:
                # No direct matches means no posts -- see the class docstring.
                return []

            # Best-first and oversampled: external crawls fail often (bot walls, PDFs,
            # dead links) and the AI filter takes more, so the pool must be several times
            # the target for the target to be reachable.
            hits = sorted(
                unique_hits.values(),
                key=lambda h: h.get('points', 0) or 0,
                reverse=True
            )[:MAX_POSTS_PER_PLATFORM]

            # Crawl in concurrent waves, stopping once the target is satisfied.
            return await self.collect_until_target(
                hits, lambda hit: self.process_hit(client, hit)
            )

        except Exception as e:
            print(f"Critical Error in Hacker News Collector orchestration: {e}")
            return []
