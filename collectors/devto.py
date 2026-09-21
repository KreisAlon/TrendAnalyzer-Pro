import httpx
import asyncio
from collectors.base import BaseCollector
from collectors.github import clean_markdown
from config import MAX_POSTS_PER_PLATFORM, TEXT_PREVIEW_LENGTH, MAX_CONCURRENT_DEVTO

class DevToCollector(BaseCollector):
    """
    Collector for Dev.to articles.

    Architecture Note:
    This collector uses 'Deep Fetching' to retrieve the full markdown body of articles
    rather than relying on truncated API descriptions. To prevent network I/O bottlenecks,
    it implements concurrent fetching using asyncio.gather().
    """

    # Dev.to's /articles endpoint filters on a single tag, so relevant AI content is
    # spread across several tags. Each is fetched separately and merged.
    TAGS = ["ai", "machinelearning", "llm"]

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
                    return clean_markdown(body)[:TEXT_PREVIEW_LENGTH]
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
            per_tag = max(10, MAX_POSTS_PER_PLATFORM // len(self.TAGS))
            tag_results = await asyncio.gather(
                *[self.fetch_tag(client, tag, per_tag) for tag in self.TAGS]
            )

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

            # Concurrency implementation: Create a list of async tasks for all articles
            tasks = [self.process_article(client, art) for art in articles]

            # Execute all tasks simultaneously. This reduces execution time from
            # O(N) linear time to O(1) network latency time.
            results = await asyncio.gather(*tasks)

            # Filter out None values (articles that failed quality checks or network errors)
            return [post for post in results if post is not None]

        except Exception as e:
            print(f"Critical Error in Dev.to Collector orchestration: {e}")
            return []
