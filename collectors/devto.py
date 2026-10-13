"""
Dev.to collector: long-form practitioner articles.

Data flow
---------
1. Request the week's top articles per planned tag (the API filters on one tag per
   request, so tags are fetched concurrently and merged).
2. De-duplicate, rank by reactions, and keep an oversampled candidate pool.
3. Deep-fetch each full markdown body, strip markup, and gate in concurrent waves
   until the target is met.

Dev.to reports no match count, so each tag's weekly list length serves as its volume.
"""

import asyncio

import httpx

from collectors.base import BaseCollector
from collectors.text_cleaning import clean_markdown
from config import TEXT_PREVIEW_LENGTH, MAX_CONCURRENT_DEVTO, DEFAULT_DEVTO_TAGS

# With no total count, a large page makes the list length itself the week's count
# for the tag; a full page is flagged as capped (true count is at least this).
DEVTO_COUNT_PAGE = 1000

# Longest Retry-After honoured before the single retry of a 429 response.
MAX_RETRY_AFTER_S = 5.0


class DevToCollector(BaseCollector):
    """
    Collector for Dev.to articles.

    The list endpoint returns only a short description, so each candidate is
    deep-fetched individually under a semaphore sized to Dev.to's rate limit.
    """

    def __init__(self):
        super().__init__("Dev.to")
        self.api_url = "https://dev.to/api/articles"
        # Rate Limiting: Dev.to enforces a strict per-client limit.
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_DEVTO)

    def tags(self):
        """Dev.to tags: the plan's, or the curated fallback."""
        return self.planned('devto_tags', DEFAULT_DEVTO_TAGS)

    async def _get(self, client, url, params=None):
        """GET under the semaphore, retrying once after a short HTTP 429 back-off."""
        async with self.semaphore:
            resp = await client.get(url, params=params)
            if resp.status_code == 429:
                retry_after = resp.headers.get('Retry-After', '')
                wait = float(retry_after) if retry_after.isdigit() else 1.0
                if wait <= MAX_RETRY_AFTER_S:
                    await asyncio.sleep(wait)
                    resp = await client.get(url, params=params)
        return resp

    async def fetch_full_content(self, client, article_id):
        """
        Deep-fetches the complete body of one article.

        Returns:
            str: The cleaned, boundary-truncated markdown body; the API description
            when the body is empty; or '' on failure.
        """
        try:
            resp = await self._get(client, f"{self.api_url}/{article_id}")
            if resp.status_code == 200:
                data = resp.json()
                body = data.get('body_markdown') or ''
                if body:
                    return self.truncate_at_boundary(clean_markdown(body), TEXT_PREVIEW_LENGTH)
                return data.get('description') or ''
        except Exception:
            # Fault Isolation: one failed fetch must not break the wave.
            return ""
        return ""

    async def process_article(self, client, art):
        """
        Deep-fetches, normalises and gates one article.

        Returns:
            dict | None: The accepted post, or None if rejected or malformed.
        """
        try:
            full_content = await self.fetch_full_content(client, art['id'])

            post = {
                'source_platform': self.platform_name,
                'external_id': str(art['id']),
                'title': art.get('title', ''),
                # Graceful Degradation to the list-level description.
                'content': full_content or art.get('description', ''),
                'author': (art.get('user') or {}).get('username') or 'unknown',
                'url': art.get('url', ''),
                'raw_score': art.get('public_reactions_count') or 0,
                'published_at': art.get('published_at', ''),
            }

            if self.is_quality_content(post):
                return post
        except Exception as e:
            # Malformed record: counted as 'unreachable' by the funnel residual.
            print(f"Error processing Dev.to article {art.get('id')}: {e}")

        return None

    async def fetch_tag(self, client, tag, per_tag):
        """
        Fetches the week's best-received articles for one tag.

        `top=7` ranks by reactions over seven days; the default chronological order
        would surface posts with no engagement signal yet.

        Returns:
            list[dict]: Article records carrying an id; [] on any failure.
        """
        params = {"tag": tag, "top": 7, "per_page": per_tag}
        try:
            response = await self._get(client, self.api_url, params=params)
            if response.status_code != 200:
                return []
            payload = response.json()
        except Exception:
            return []
        # Error bodies arrive as a JSON object; only a list of records is usable.
        if not isinstance(payload, list):
            return []
        return [a for a in payload if isinstance(a, dict) and a.get('id') is not None]

    async def collect(self, client: httpx.AsyncClient):
        """
        Runs the Dev.to stage of a scan: tag lists, merge, deep fetch and gate.

        Returns:
            list[dict]: Up to `target_posts` accepted posts.
        """
        try:
            tags = self.tags()
            if not tags:
                return []
            tag_results = await asyncio.gather(
                *[self.fetch_tag(client, tag, DEVTO_COUNT_PAGE) for tag in tags]
            )
            # Each list's length is that tag's weekly count; a full page means the
            # reporting ceiling was reached.
            for batch in tag_results:
                self.search_totals.append(len(batch))
                if len(batch) >= DEVTO_COUNT_PAGE:
                    self.search_capped = True

            articles = self.merge_candidates(tag_results, 'id', 'public_reactions_count')

            return await self.collect_until_target(
                articles, lambda art: self.process_article(client, art)
            )

        except Exception as e:
            # Fault Isolation: an orchestration failure costs this platform, not the scan.
            print(f"Critical Error in Dev.to Collector orchestration: {e}")
            return []
