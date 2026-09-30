import re
import asyncio
import httpx
import textwrap
from collectors.base import BaseCollector
from config import (
    MAX_POSTS_PER_PLATFORM, TARGET_POSTS_PER_PLATFORM, TEXT_PREVIEW_LENGTH,
    DEFAULT_MASTODON_TAGS,
)

# Mastodon clamps the timeline 'limit' parameter to 40 server-side; asking for more
# silently returns 40, so request the real ceiling.
MASTODON_MAX_LIMIT = 40

# Toots are short, so most of a page dies on the length and hashtag gates -- a single
# page of 40 was yielding 8 stored posts. Page backwards through the tag timeline with
# max_id until the target is met, up to this many requests.
MASTODON_MAX_PAGES = 8

# Trailing hashtag blocks ("... #ai #art #kunst") carry no prose value.
_TRAILING_TAGS_RE = re.compile(r'(?:\s*#\w+)+\s*$')


# Mastodon's tag timeline lives at a path, so the PRIMARY tag is part of the URL and
# the rest widen the same request through 'any[]'. The base host is kept separate
# from the tag for exactly that reason.
MASTODON_HOST = "https://mastodon.social"

# Mastodon hashtags are alphanumeric. Anything else is stripped rather than sent:
# the primary tag is interpolated into a URL path, so a value containing a slash or
# a query character would silently address a different endpoint. topics.py already
# validates planner output to this shape; this is the second, local guarantee, so
# the invariant holds no matter who calls the collector.
_TAG_SAFE_RE = re.compile(r'[^a-z0-9]')


class MastodonCollector(BaseCollector):
    def __init__(self):
        super().__init__("Mastodon")
        # Retained for backwards compatibility and as the no-profile default; the
        # live URL is derived per request from the run's tag plan.
        self.api_url = self._timeline_url(DEFAULT_MASTODON_TAGS[0])

    @staticmethod
    def _timeline_url(tag):
        return f"{MASTODON_HOST}/api/v1/timelines/tag/{tag}"

    def hashtags(self):
        """
        The hashtags for this run, sanitised and de-duplicated, best-first.

        Empty when the plan has no usable tag; collect() then returns no posts
        rather than reading the curated AI timeline for an unrelated topic.
        """
        seen, tags = set(), []
        for raw in self.planned('mastodon_tags', DEFAULT_MASTODON_TAGS):
            tag = _TAG_SAFE_RE.sub('', str(raw).lower())
            if tag and tag not in seen:
                seen.add(tag)
                tags.append(tag)
        return tags

    async def fetch_page(self, client, max_id=None):
        """
        Fetches one page of the tag timeline, older than `max_id` when given.

        'any[]' widens the same request to additional tags instead of costing extra
        round trips: the leading tag anchors the timeline and the remainder are
        OR-ed onto it, so N tags still cost one request per page.
        """
        tags = self.hashtags()
        params = [("limit", MASTODON_MAX_LIMIT)]
        params += [("any[]", tag) for tag in tags[1:]]
        if max_id:
            params.append(("max_id", max_id))
        try:
            response = await client.get(self._timeline_url(tags[0]), params=params)
            if response.status_code != 200:
                return []
            return response.json()
        except Exception as e:
            print(f"Error fetching Mastodon page: {e}")
            return []

    async def fetch_tag_volume(self, client, tag):
        """
        Posts using `tag` over the last 7 days, from the tag's own daily history.

        The timeline API has no count, but GET /api/v1/tags/:name carries per-day
        `uses` for the past week as seen by this server -- the platform's own
        measure of how much is being posted under the tag.
        """
        try:
            response = await client.get(f"{MASTODON_HOST}/api/v1/tags/{tag}")
            if response.status_code != 200:
                return None
            history = response.json().get('history') or []
            return sum(int(day.get('uses') or 0) for day in history[:7])
        except Exception:
            return None

    def _measure_tag_overlap(self, items):
        """
        Overlap correction for the summed tag volumes, measured on fetched toots.

        A toot tagged #ai and #llm is counted in both tags' history, so the sum of
        the histories over-counts it. On the toots we actually fetched, the ratio
        of toots to (toot, planned-tag) memberships is how much to scale back.
        """
        planned = set(self.hashtags())
        memberships = 0
        for item in items:
            tags = {str(t.get('name', '')).lower() for t in item.get('tags') or []}
            memberships += max(1, len(tags & planned))
        if items and memberships:
            self.overlap_factor = len(items) / memberships

    async def collect(self, client: httpx.AsyncClient):
        if not self.hashtags():
            return []
        volumes = await asyncio.gather(
            *[self.fetch_tag_volume(client, tag) for tag in self.hashtags()]
        )
        self.search_totals = [v for v in volumes if v is not None]
        print(f"--- {self.platform_name}: Ingesting Toots for "
              f"#{', #'.join(self.hashtags())}... ---")
        posts = []
        seen = 0
        fetched = []
        try:
            max_id = None
            for _ in range(MASTODON_MAX_PAGES):
                if len(posts) >= TARGET_POSTS_PER_PLATFORM or seen >= MAX_POSTS_PER_PLATFORM * 2:
                    break

                items = await self.fetch_page(client, max_id)
                if not items:
                    break
                # Page backwards from the oldest id on this page.
                max_id = items[-1].get('id')
                seen += len(items)
                fetched.extend(items)

                posts.extend(self.process_page(items))

            return self._finalize(posts, seen, fetched)
        except Exception as e:
            print(f"Error in Mastodon: {e}")
            return self._finalize(posts, seen, fetched)

    def _finalize(self, posts, seen, fetched=()):
        """Keeps the most engaged toots and reports the yield for this cycle."""
        # Gate statistics are taken before the target cut, as in
        # BaseCollector.collect_until_target().
        self.gated += seen
        self.gate_accepted += len(posts)
        self.unique_candidates += len({i.get('id') for i in fetched})
        self.fetched_rows += len(fetched)
        self._measure_tag_overlap(list(fetched))
        # The timeline is chronological, so a final page can push the count past the
        # target. Rank by engagement before cutting rather than discarding whatever
        # happened to arrive last -- prune_database() keeps by trend_score, which is
        # derived from exactly this raw_score.
        posts.sort(key=lambda p: p.get('raw_score', 0), reverse=True)
        posts = posts[:TARGET_POSTS_PER_PLATFORM]

        if len(posts) < TARGET_POSTS_PER_PLATFORM:
            print(f"   ⚠️  {self.platform_name}: {len(posts)}/{TARGET_POSTS_PER_PLATFORM} "
                  f"toots cleared the gate from {seen} candidates. "
                  f"Rejections: {dict(self.reject_reasons)}")
        else:
            print(f"   ✅ {self.platform_name}: {len(posts)} posts from {seen} candidates.")
        return posts

    def process_page(self, items):
        """Applies the source-level filters and the quality gate to one page of toots."""
        accepted = []
        for item in items:
            try:
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
                # Long-form toots exist (13k characters observed). Hold them to the same
                # budget as every other platform, cut on a sentence boundary.
                prose = self.truncate_at_boundary(prose, TEXT_PREVIEW_LENGTH)

                # A toot has no title of its own, so the opening sentence stands in.
                # No "..." placeholder: the title is a label, and the full prose is
                # stored in 'content' anyway, so the ellipsis only ever read as if the
                # post itself had been truncated.
                title = textwrap.shorten(prose, width=80, placeholder="")
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
                    accepted.append(post)
            except Exception as e:
                # One malformed toot must not cost the rest of the page.
                print(f"Error processing Mastodon toot {item.get('id')}: {e}")
        return accepted
