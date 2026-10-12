"""
Mastodon collector: short-form public discussion from tag timelines.

Data flow
---------
1. Read each planned hashtag's seven-day usage history (the platform's own volume
   figure, since the timeline API has no match count).
2. Page backwards through the combined tag timeline (one request per page for all
   tags) until the target is met or the page budget runs out.
3. Apply cheap source-level metadata filters (reposts, content warnings, declared
   language, replies, hashtag-only posts), each recorded as a rejection reason,
   then pass survivors through the shared quality gate.
4. Rank accepted posts by engagement and report the funnel and volume figures.

Toots arrive complete in the timeline payload, so pages are gated without a deep fetch.
"""

import re
import asyncio
import textwrap

import httpx
from bs4 import BeautifulSoup

from collectors.base import BaseCollector
from config import DEFAULT_MASTODON_TAGS

MASTODON_HOST = "https://mastodon.social"

# Server-side clamp on the timeline 'limit' parameter.
MASTODON_MAX_LIMIT = 40

# Many short toots fail the gate, so the timeline is paged backwards (max_id) up to
# this many requests.
MASTODON_MAX_PAGES = 8

# A trailing block of hashtags ("... #ai #art #code") carries no prose value.
_TRAILING_TAGS_RE = re.compile(r'(?:\s*#\w+)+\s*$')

# The primary tag is interpolated into a URL path; this local sanitiser backs up the
# planner validation in engine/topic_profile.py (defence in depth).
_TAG_SAFE_RE = re.compile(r'[^a-z0-9]')


def flatten_toot_html(content):
    """
    Converts a toot's HTML into plain text that reads like the original toot.

    Mastodon splits links and hashtags across several <span>s, so generic tag
    stripping yields fragments like "# Rust". Hashtags and mentions are kept whole,
    other links dropped, and line breaks become spaces.
    """
    soup = BeautifulSoup(content or '', 'html.parser')
    for br in soup.find_all('br'):
        br.replace_with(' ')
    for anchor in soup.find_all('a'):
        text = anchor.get_text('', strip=True)
        anchor.replace_with(f' {text} ' if text.startswith(('#', '@')) else ' ')
    return soup.get_text(' ')


class MastodonCollector(BaseCollector):
    """
    Collector for toots from the public tag timelines of mastodon.social.

    Source-level metadata filters run before the shared gate and are recorded in
    `reject_reasons`, so they appear in the Gatekeeper funnel.
    """

    def __init__(self):
        super().__init__("Mastodon")

    @staticmethod
    def _timeline_url(tag):
        """Tag timeline endpoint for a sanitised hashtag (without '#')."""
        return f"{MASTODON_HOST}/api/v1/timelines/tag/{tag}"

    def hashtags(self):
        """
        Sanitised, de-duplicated hashtags for this run, best-first. Empty when the
        plan has no usable tag, so collect() reads no unrelated default timeline.
        """
        seen, tags = set(), []
        for raw in self.planned('mastodon_tags', DEFAULT_MASTODON_TAGS):
            tag = _TAG_SAFE_RE.sub('', str(raw).lower())
            if tag and tag not in seen:
                seen.add(tag)
                tags.append(tag)
        return tags

    async def fetch_page(self, client, tags, max_id=None):
        """
        Fetches one page of the combined tag timeline: the first tag is the URL path
        and the rest are OR-ed on via 'any[]', so N tags cost one request per page.

        Args:
            tags (list[str]): Sanitised hashtags, primary first.
            max_id (str | None): Pagination cursor; only older toots are returned.

        Returns:
            list[dict]: Toot records carrying an id; [] on any failure.
        """
        params = [("limit", MASTODON_MAX_LIMIT)]
        params += [("any[]", tag) for tag in tags[1:]]
        if max_id:
            params.append(("max_id", max_id))
        try:
            response = await client.get(self._timeline_url(tags[0]), params=params)
            # Errors and rate limits end pagination rather than raising.
            if response.status_code != 200:
                print(f"   ⚠️  Mastodon timeline returned {response.status_code}")
                return []
            payload = response.json()
        except Exception as e:
            print(f"Error fetching Mastodon page: {e}")
            return []
        if not isinstance(payload, list):
            return []
        return [i for i in payload if isinstance(i, dict) and i.get('id')]

    async def fetch_tag_volume(self, client, tag):
        """
        Seven-day usage of `tag` from the per-day `uses` history of /api/v1/tags/:name.

        Returns:
            int | None: Seven-day usage, or None when unavailable.
        """
        try:
            response = await client.get(f"{MASTODON_HOST}/api/v1/tags/{tag}")
            if response.status_code != 200:
                return None
            payload = response.json()
            history = (payload.get('history') if isinstance(payload, dict) else None) or []
            # 'uses' arrives as a string.
            return sum(int(day.get('uses') or 0) for day in history[:7])
        except Exception:
            return None

    def _measure_tag_overlap(self, items, planned):
        """
        Overlap correction for the summed tag volumes: a toot with two planned tags
        is counted in both histories, so the factor is toots / (toot, tag) memberships.
        """
        memberships = 0
        for item in items:
            tags = {str(t.get('name', '')).lower() for t in item.get('tags') or []
                    if isinstance(t, dict)}
            # Each toot counts at least once, even if it matched through content only.
            memberships += max(1, len(tags & planned))
        if items and memberships:
            self.overlap_factor = len(items) / memberships

    async def collect(self, client: httpx.AsyncClient):
        """
        Runs the Mastodon stage of a scan: tag volumes, paging, filtering and gating.

        Returns:
            list[dict]: Up to `target_posts` accepted posts.
        """
        tags = self.hashtags()
        if not tags:
            return []
        volumes = await asyncio.gather(*[self.fetch_tag_volume(client, tag) for tag in tags])
        self.search_totals = [v for v in volumes if v is not None]
        posts, seen, fetched = [], 0, []
        try:
            max_id = None
            for _ in range(MASTODON_MAX_PAGES):
                # Stop once the target is met or the candidate budget is exhausted.
                if len(posts) >= self.target_posts or seen >= self.max_candidates * 2:
                    break
                items = await self.fetch_page(client, tags, max_id)
                if not items:
                    break
                max_id = items[-1].get('id')
                seen += len(items)
                fetched.extend(items)
                posts.extend(self.process_page(items))
        except Exception as e:
            # Graceful Degradation: whatever was gathered before the failure is kept.
            print(f"Error in Mastodon: {e}")
        return self._finalize(posts, seen, fetched, set(tags))

    def _finalize(self, posts, seen, fetched, planned):
        """
        Records the funnel figures and returns the most engaged accepted toots.

        Returns:
            list[dict]: At most `target_posts` posts, by engagement.
        """
        # Funnel accounting before the target cut, mirroring collect_until_target().
        self.gated += seen
        self.gate_accepted += len(posts)
        self.record_candidates([fetched], 'id')
        self._measure_tag_overlap(fetched, planned)
        # The last page can overshoot; ranking before the cut keeps the most engaged
        # toots, consistent with retention by trend score.
        posts.sort(key=lambda p: p.get('raw_score', 0), reverse=True)
        posts = posts[:self.target_posts]
        self.log_yield(len(posts), self.target_posts, seen, noun='toots')
        return posts

    def process_page(self, items):
        """
        Applies the free metadata filters, then the shared gate, to one page. Each
        filter records its reason, keeping the funnel balanced.

        Returns:
            list[dict]: Accepted posts from this page.
        """
        accepted = []
        for item in items:
            try:
                # Boosts duplicate content reachable through the original.
                if item.get('reblog'):
                    self.reject_reasons['repost'] += 1
                    continue
                if item.get('sensitive'):
                    self.reject_reasons['sensitive'] += 1
                    continue
                # Declared language is a free pre-check ahead of langdetect.
                language = item.get('language')
                if language and language != 'en':
                    self.reject_reasons['not_english'] += 1
                    continue

                clean_content = self.clean_text(flatten_toot_html(item.get('content', '')))

                # Replies (leading mention) are conversation, not topical discussion.
                if clean_content.startswith('@'):
                    self.reject_reasons['reply'] += 1
                    continue

                # The gate and title use the prose without its trailing hashtag block,
                # which would skew the hashtag-ratio and length checks.
                prose = _TRAILING_TAGS_RE.sub('', clean_content).strip()
                if not prose:
                    self.reject_reasons['hashtag_soup'] += 1
                    continue

                # The opening (<= 80 chars, cut on a word) serves as the title.
                title = textwrap.shorten(prose, width=80, placeholder="")
                raw_score = ((item.get('replies_count') or 0) +
                             (item.get('reblogs_count') or 0) +
                             (item.get('favourites_count') or 0))

                post = {
                    'source_platform': self.platform_name,
                    'external_id': str(item['id']),
                    'title': title,
                    'content': prose,
                    'author': (item.get('account') or {}).get('username') or 'unknown',
                    'url': item.get('url', ''),
                    'raw_score': raw_score,
                    'published_at': item.get('created_at', ''),
                }

                if self.is_quality_content(post):
                    # Toots are short, so the full text (hashtags included) is stored.
                    post['content'] = clean_content
                    accepted.append(post)
            except Exception as e:
                # Fault Isolation: counted as 'unreachable' by the funnel residual.
                print(f"Error processing Mastodon toot {item.get('id')}: {e}")
        return accepted
