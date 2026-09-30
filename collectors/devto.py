import httpx
import asyncio
from collectors.base import BaseCollector
from collectors.github import clean_markdown
from config import (
    MAX_POSTS_PER_PLATFORM, TEXT_PREVIEW_LENGTH, MAX_CONCURRENT_DEVTO,
    DEFAULT_DEVTO_TAGS,
)

# Dev.to reports no match count, so each tag is fetched at the API's page maximum:
# the length of the week's list IS the count. One request per tag either way, and
# only the best MAX_POSTS_PER_PLATFORM are deep-fetched. A tag that fills the whole
# page is reported as capped (the true count is at least this).
DEVTO_COUNT_PAGE = 1000


class DevToCollector(BaseCollector):
    """
    Collector for Dev.to articles.

    Architecture Note:
    This collector uses 'Deep Fetching' to retrieve the full markdown body of articles
    rather than relying on truncated API descriptions. To prevent network I/O bottlenecks,
    it implements concurrent fetching using asyncio.gather().
    """

    # Dev.to's /articles endpoint filters on a single tag, so relevant content is
    # spread across several tags. Each is fetched separately and merged.
    # Curated fallback only -- the live tags come from the run's TopicProfile.
    TAGS = DEFAULT_DEVTO_TAGS

    def tags(self):
        """Dev.to tags for this run: the plan, or the curated fallback."""
        return self.planned('devto_tags', self.TAGS)

    def __init__(self):
        super().__init__("Dev.to")
        self.api_url = "https://dev.to/api/articles"
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_DEVTO)

    async def fetch_full_content(self, client, article_id):
        """
        Retrieves the complete markdown content for a specific article.

        Args:
            client (httpx.AsyncClient): The shared async HTTP client.
            article_id (int): The unique identifier of the Dev.to article.

        Returns:
            str: The full markdown body, or fallback description if the body is unavailable.
        """
        url = f"https://dev.to/api/articles/{article_id}"
        try:
            # Bounded concurrency keeps us inside Dev.to's per-client rate limit.
            async with self.semaphore:
                resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                # Prefer the full markdown body for deep semantic analysis, with the
                # markdown chrome (frontmatter, code fences, images, badges) removed so
                # the NLP stage sees prose rather than syntax.
                body = data.get('body_markdown', '') or ''
                if body:
                    # Boundary-aware cut: a plain slice left the stored article ending
                    # mid-word.
                    return self.truncate_at_boundary(clean_markdown(body), TEXT_PREVIEW_LENGTH)
                return data.get('description', '')
        except Exception as e:
            # Silent fail for individual requests to prevent breaking the entire batch
            return ""
        return ""

    async def process_article(self, client, art):
        """
        Pipeline for processing a single article: fetching content, normalizing schema,
        and validating quality. Designed to be run concurrently.
        """
        try:
            # 1. Execute the deep fetch
            full_content = await self.fetch_full_content(client, art['id'])

            # 2. Map external data to our internal Unified Schema
            post = {
                'source_platform': self.platform_name,
                'external_id': str(art['id']),
                'title': art.get('title', ''),
                'content': full_content if full_content else art.get('description', ''),
                'author': art.get('user', {}).get('username', 'unknown'),
                'url': art.get('url', ''),
                'raw_score': art.get('public_reactions_count', 0),
                'sentiment': self.analyze_sentiment(full_content),
                'published_at': art.get('published_at', '')
            }

            # 3. Pass through the semantic gatekeeper (base.py)
            if self.is_quality_content(post):
                return post
        except Exception as e:
            print(f"Error processing Dev.to article {art.get('id')}: {e}")

        # Return None if the article fails validation or encounters an error
        return None

    async def fetch_tag(self, client, tag, per_tag):
        """
        Fetches the best-received articles for one tag.

        'top=7' ranks by reactions over the last week, which is what "trending" means
        here. The default ordering is simply reverse-chronological and surfaces
        brand-new posts with no engagement signal at all.
        """
        params = {"tag": tag, "top": 7, "per_page": per_tag}
        try:
            response = await client.get(self.api_url, params=params)
            if response.status_code != 200:
                return []
            return response.json()
        except Exception:
            return []

    async def collect(self, client: httpx.AsyncClient):
        """Main entry point for the collector. Orchestrates the concurrent fetching."""
        print(f"--- {self.platform_name}: Performing Concurrent Deep Fetch... ---")
        try:
            tags = self.tags()
            if not tags:
                return []
            tag_results = await asyncio.gather(
                *[self.fetch_tag(client, tag, DEVTO_COUNT_PAGE) for tag in tags]
            )
            for batch in tag_results:
                self.search_totals.append(len(batch))
                if len(batch) >= DEVTO_COUNT_PAGE:
                    self.search_capped = True
            self.record_candidates(tag_results, 'id')

            # Merge tags, deduplicating articles that carry more than one of them.
            unique_articles = {}
            for batch in tag_results:
                for art in batch:
                    unique_articles.setdefault(art['id'], art)

            articles = sorted(
                unique_articles.values(),
                key=lambda a: a.get('public_reactions_count', 0),
                reverse=True
            )[:MAX_POSTS_PER_PLATFORM]

            # Deep-fetch in concurrent waves and stop once the target is met, so the
            # oversampled pool only costs requests when the gate is rejecting.
            return await self.collect_until_target(
                articles, lambda art: self.process_article(client, art)
            )

        except Exception as e:
            print(f"Critical Error in Dev.to Collector orchestration: {e}")
            return []
