import re
import httpx
import textwrap
from collectors.base import BaseCollector
from config import MAX_POSTS_PER_PLATFORM

# Mastodon clamps the timeline 'limit' parameter to 40 server-side; asking for more
# silently returns 40, so request the real ceiling.
MASTODON_MAX_LIMIT = 40

# Trailing hashtag blocks ("... #ai #art #kunst") carry no prose value.
_TRAILING_TAGS_RE = re.compile(r'(?:\s*#\w+)+\s*$')


class MastodonCollector(BaseCollector):
    def __init__(self):
        super().__init__("Mastodon")
        self.api_url = "https://mastodon.social/api/v1/timelines/tag/ai"

    async def collect(self, client: httpx.AsyncClient):
        print(f"--- {self.platform_name}: Ingesting Toots... ---")
        posts = []
        try:
            # 'any[]' widens the same request to additional tags instead of costing
            # extra round trips.
            params = [
                ("limit", min(MAX_POSTS_PER_PLATFORM, MASTODON_MAX_LIMIT)),
                ("any[]", "llm"),
                ("any[]", "machinelearning"),
                ("any[]", "genai"),
            ]
            response = await client.get(self.api_url, params=params)
            if response.status_code != 200: return []

            items = response.json()[:MAX_POSTS_PER_PLATFORM]

            for item in items:
                # --- SOURCE-LEVEL FILTERS ---
                # Reblogs duplicate content already reachable via the original toot.
                if item.get('reblog'):
                    continue
                # Sensitive/CW posts are usually off-topic or NSFW.
                if item.get('sensitive'):
                    continue
                # Mastodon reports the author's declared language. Using it is free,
                # unlike langdetect further down the pipeline.
                language = item.get('language')
                if language and language != 'en':
                    continue

                clean_content = self.clean_text(item.get('content', ''))

                # --- FILTER REPLIES ---
                # Skip if the post is a personal conversation starting with @
                if clean_content.startswith('@'): continue

                # Hashtag-only toots ("#AI #BigTech #Musk #Meta") produce keywords that
                # describe the tags, not a topic. Measure the prose that remains once
                # the trailing tag block is removed.
                prose = _TRAILING_TAGS_RE.sub('', clean_content).strip()
                if not prose:
                    continue

                title = textwrap.shorten(prose, width=80, placeholder="...")
                raw_score = (item.get('replies_count', 0) +
                             item.get('reblogs_count', 0) +
                             item.get('favourites_count', 0))

                post = {
                    'source_platform': self.platform_name,
                    'external_id': str(item['id']),
                    'title': title,
                    'content': prose,
                    'author': item.get('account', {}).get('username', 'unknown'),
                    'url': item.get('url', ''),
                    'raw_score': raw_score,
                    'sentiment': self.analyze_sentiment(prose),
                    'published_at': item.get('created_at', '')
                }

                if self.is_quality_content(post):
                    posts.append(post)
            return posts
        except Exception as e:
            print(f"Error in Mastodon: {e}")
            return []
