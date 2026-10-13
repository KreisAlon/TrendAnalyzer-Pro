"""
Hacker News collector: industry discussion of linked articles.

Data flow
---------
1. Query the Algolia HN index once per planned phrase, limited to stories from the
   last seven days with at least `min_points` points (lower for reduced scans;
   a full scan falls back to the lower floor when a niche topic is too thin).
2. Merge, de-duplicate and rank by points into an oversampled pool.
3. Crawl each story's external link concurrently and extract the readable article
   text (text posts such as "Ask HN" use their own body).
4. Gate in concurrent waves until the per-platform target is met.

HN stories are mostly links, so this collector is a small, bounded web crawler;
external pages are volatile, so every step degrades to "no text" rather than raising.
"""

import asyncio
import time

import httpx
from bs4 import BeautifulSoup

from collectors.base import BaseCollector
from collectors.text_cleaning import JUNK_PATTERN
from config import (
    TEXT_PREVIEW_LENGTH, MAX_CONCURRENT_SCRAPES, DEFAULT_HN_QUERIES,
    REDUCED_SCAN_MAX_POSTS, HN_MIN_POINTS_REDUCED, HN_MIN_POINTS_FULL,
)

# Pages above this size are not parsed, bounding parse time and DOM memory.
MAX_PAGE_BYTES = 2_000_000

# A deep page costs the same single request; the gate consumes only what it needs.
ALGOLIA_HITS_PER_PAGE = 60

# A semantic container below this length is treated as a teaser, so paragraph-based
# extraction may replace it.
THIN_CONTAINER_CHARS = 600


class HackerNewsCollector(BaseCollector):
    """
    Collector for Hacker News stories via the Algolia search index.

    Deliberately no front-page fallback: an empty search yields zero posts, not
    unrelated stories.
    """

    def __init__(self):
        super().__init__("Hacker News")
        self.search_url = "https://hn.algolia.com/api/v1/search"
        # Concurrency Control for external crawls.
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_SCRAPES)

    def search_queries(self):
        """Free-text Algolia queries: the plan's, or the curated fallback."""
        return self.planned('hn_queries', DEFAULT_HN_QUERIES)

    async def scrape_external_link(self, client, url):
        """
        Crawls one external page and extracts its readable article text.

        Prefers the `<article>` / `<main>` container over whole-page text (navigation,
        cookie banners). Concatenated `<p>` text replaces it only when the container
        is a short teaser shorter than the paragraphs; always taking the longer text
        would let sidebars wrapped in `<main>` win.

        Returns:
            str: Boundary-truncated plain text, or '' when the page is unusable.
        """
        if not url or "news.ycombinator.com" in url:
            return ""

        try:
            # Short timeout, so slow third-party servers cannot stall the wave.
            async with self.semaphore:
                resp = await client.get(url, timeout=5.0, follow_redirects=True)

            if resp.status_code != 200:
                return ""
            # Only HTML is parsed; PDFs and images would decode into binary noise.
            if 'html' not in resp.headers.get('content-type', '').lower():
                return ""
            if len(resp.content) > MAX_PAGE_BYTES:
                return ""

            soup = BeautifulSoup(resp.text, 'html.parser')
            for s in soup(["script", "style", "nav", "footer", "header",
                           "aside", "form", "noscript"]):
                s.decompose()

            def flat(node):
                """Visible text of a DOM node with whitespace collapsed ('' for None)."""
                return ' '.join(node.get_text(separator=' ').split()) if node is not None else ''

            preferred = flat(soup.find('article')) or flat(soup.find('main'))
            paragraphs = soup.find_all('p')
            fallback = ' '.join(flat(p) for p in paragraphs) if paragraphs else flat(soup)

            if preferred and (len(preferred) >= THIN_CONTAINER_CHARS
                              or len(preferred) >= len(fallback)):
                text = preferred
            else:
                text = fallback

            # Early exit on bot walls served with HTTP 200 (the gate would also catch them).
            if JUNK_PATTERN.search(text[:400]):
                return ""

            return self.truncate_at_boundary(text, TEXT_PREVIEW_LENGTH)
        except Exception:
            # Graceful Degradation: blocks, timeouts and malformed pages mean "no text".
            return ""

    async def process_hit(self, client, hit):
        """
        Crawls, normalises and gates one Algolia hit.

        Returns:
            dict | None: The accepted post, or None if rejected or malformed.
        """
        try:
            sid = hit.get('objectID')
            url = hit.get('url') or ''
            title = hit.get('title') or ''

            external_text = await self.scrape_external_link(client, url)

            # Text posts (Ask/Show HN) carry their own body; the title is a last resort.
            content = external_text or hit.get('story_text') or hit.get('comment_text') or title

            post = {
                'source_platform': self.platform_name,
                'external_id': str(sid),
                'title': title,
                'content': content,
                'author': hit.get('author') or 'unknown',
                # Link-less stories point at their discussion page.
                'url': url or f"https://news.ycombinator.com/item?id={sid}",
                'raw_score': hit.get('points') or 0,
                'published_at': hit.get('created_at', ''),
            }

            if self.is_quality_content(post):
                return post

        except Exception:
            # Malformed hit: counted as 'unreachable' by the funnel residual.
            pass

        return None

    @property
    def min_points(self):
        """Story-points floor for this scan: lenient for reduced (background) scans."""
        return (HN_MIN_POINTS_REDUCED if self.target_posts <= REDUCED_SCAN_MAX_POSTS
                else HN_MIN_POINTS_FULL)

    async def _search_all(self, client, queries, cutoff, min_points):
        """Runs every query at one points floor; returns the per-query hit lists."""
        return await asyncio.gather(
            *[self.search_stories(client, q, cutoff, min_points) for q in queries])

    async def search_stories(self, client, query, cutoff, min_points=None):
        """
        Queries Algolia for recent, well-received stories.

        Args:
            cutoff (int): Unix timestamp; only stories created after it are returned.
            min_points (int | None): Points floor; defaults to `min_points`.

        Returns:
            list[dict]: Story hits carrying an objectID; [] on any failure.
        """
        params = {
            "query": query,
            "tags": "story",
            "numericFilters": f"points>={min_points or self.min_points},"
                              f"created_at_i>{cutoff}",
            "hitsPerPage": ALGOLIA_HITS_PER_PAGE,
        }
        try:
            resp = await client.get(self.search_url, params=params, timeout=10.0)
            if resp.status_code != 200:
                return []
            payload = resp.json()
            if not isinstance(payload, dict):
                return []
            # nbHits counts every match, not just the returned page.
            if isinstance(payload.get('nbHits'), int):
                self.search_totals.append(payload['nbHits'])
            hits = payload.get('hits') or []
            return [h for h in hits if isinstance(h, dict) and h.get('objectID')]
        except Exception:
            return []

    async def collect(self, client: httpx.AsyncClient):
        """
        Runs the Hacker News stage of a scan: search, merge, crawl and gate.

        Returns:
            list[dict]: Up to `target_posts` accepted posts.
        """
        try:
            cutoff = int(time.time()) - 7 * 86400

            queries = self.search_queries()
            if not queries:
                return []
            totals_before = len(self.search_totals)
            search_results = await self._search_all(client, queries, cutoff,
                                                    self.min_points)
            # Niche fallback: too few stories at the full-scan floor re-queries once at
            # the lenient floor, replacing the first pass in the volume figures.
            unique = {h['objectID'] for batch in search_results for h in batch}
            if len(unique) < self.target_posts and self.min_points > HN_MIN_POINTS_REDUCED:
                print(f"   ↘️  {self.platform_name}: only {len(unique)} stories with "
                      f"≥{self.min_points} points; retrying at ≥{HN_MIN_POINTS_REDUCED}.")
                del self.search_totals[totals_before:]
                search_results = await self._search_all(client, queries, cutoff,
                                                        HN_MIN_POINTS_REDUCED)
            # External crawls fail often, hence the oversampled pool.
            hits = self.merge_candidates(search_results, 'objectID', 'points')
            if not hits:
                return []

            return await self.collect_until_target(
                hits, lambda hit: self.process_hit(client, hit)
            )

        except Exception as e:
            # Fault Isolation: an orchestration failure costs this platform, not the scan.
            print(f"Critical Error in Hacker News Collector orchestration: {e}")
            return []
