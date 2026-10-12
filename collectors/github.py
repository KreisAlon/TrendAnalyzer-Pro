"""
GitHub collector: actively maintained repositories as the "code" signal.

Data flow
---------
1. Fan out one repository search per planned topic slug, restricted to repositories
   pushed to in the last 14 days and above a rank-dependent star floor.
2. Merge, de-duplicate and rank by stars into an oversampled pool.
3. Deep-fetch each README (unmetered CDN first, metered API second), strip markdown,
   and combine it with the description.
4. Gate in concurrent waves until the per-platform target is met.

GitHub's per-query `total_count` feeds the volume estimate used in the dashboard's
code-versus-discussion comparison.
"""

import base64
import os
import time
import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from collectors.base import BaseCollector
from collectors.text_cleaning import clean_markdown
from config import (
    TEXT_PREVIEW_LENGTH, MAX_CONCURRENT_GITHUB, GITHUB_RATE_LIMIT_FLOOR,
    DEFAULT_GITHUB_TOPICS,
)

# Search silently clamps per_page to 100.
GITHUB_MAX_PER_PAGE = 100

# Star floors by topic rank. Leading topics are the most specific, so they can demand
# popularity; later topics only deepen the pool to absorb gate rejections.
STAR_FLOOR_CORE = 200
STAR_FLOOR_MID = 100
STAR_FLOOR_WIDE = 50

# Longest rate-limit back-off honoured before a single retry; longer waits skip the
# query rather than consume the platform's time budget.
MAX_RATE_LIMIT_WAIT_S = 10.0


def build_headers():
    """GitHub API headers; GITHUB_TOKEN raises the core quota from 60 to 5,000/hour."""
    headers = {'Accept': 'application/vnd.github.v3+json'}
    token = os.environ.get('GITHUB_TOKEN')
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return headers


def rate_limit_wait(response):
    """
    Seconds until a rate-limited (403/429) response may be retried, from Retry-After
    or X-RateLimit-Reset; None when the response is not a rate limit.
    """
    if response.status_code not in (403, 429):
        return None
    retry_after = response.headers.get('Retry-After', '')
    if retry_after.isdigit():
        return float(retry_after)
    reset = response.headers.get('X-RateLimit-Reset', '')
    if response.headers.get('X-RateLimit-Remaining') == '0' and reset.isdigit():
        return max(0.0, int(reset) - time.time())
    return None


async def fetch_readme_raw(client, owner, repo, branch):
    """
    Primary README path via raw.githubusercontent.com, which is not metered against
    the core API quota. Other filenames fall through to `fetch_readme()`.

    Returns:
        str: Cleaned, boundary-truncated README text, or '' if not found.
    """
    # 'HEAD' resolves to the default branch when the search result omitted it.
    branch = branch or 'HEAD'
    for name in ("README.md", "readme.md"):
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{name}"
        try:
            response = await client.get(url, timeout=10.0)
            if response.status_code == 200 and response.text.strip():
                return BaseCollector.truncate_at_boundary(
                    clean_markdown(response.text), TEXT_PREVIEW_LENGTH
                )
        except Exception:
            continue
    return ""


async def fetch_readme(client, owner, repo):
    """
    Metered fallback via the core API `/readme` endpoint: resolves any README
    filename at the cost of one quota unit, so it is limited by `readme_budget`.

    Returns:
        str: Cleaned, boundary-truncated README text, or '' on any failure.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/readme"
    try:
        response = await client.get(url, headers=build_headers(), timeout=10.0)
        if response.status_code == 200:
            payload = response.json()
            if payload.get('encoding') != 'base64':
                return ""
            raw = base64.b64decode(payload.get('content') or '').decode('utf-8', errors='ignore')
            # Markup is stripped before truncating, so the budget is spent on prose.
            return BaseCollector.truncate_at_boundary(clean_markdown(raw), TEXT_PREVIEW_LENGTH)
    except Exception:
        # Graceful Degradation: any network or decoding failure means "no README".
        return ""
    return ""


class GitHubCollector(BaseCollector):
    """
    Collector for GitHub repositories: the implementation side of a topic.

    Searches and README fetches run concurrently, bounded by one semaphore.
    """

    def __init__(self):
        super().__init__("GitHub")
        self.base_url = "https://api.github.com/search/repositories"
        # Concurrency Control for all requests to GitHub hosts.
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_GITHUB)
        # Metered-fallback allowance for this scan; set by check_rate_budget().
        self.readme_budget = 0
        # Descriptions are short but curated, so the length floor is lowered.
        self.min_content_chars = 100

    def topics(self):
        """
        Topic slugs to query, best-first. Planner slugs are validated in
        engine/topic_profile.py, as GitHub treats a malformed topic as free text.
        """
        return self.planned('github_topics', DEFAULT_GITHUB_TOPICS)

    def build_queries(self):
        """
        One search query per topic slug (repository search has no boolean OR).

        Returns:
            list[str]: GitHub search-syntax query strings.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).strftime('%Y-%m-%d')
        topics = self.topics()

        # Positional tiering: first third core, next third mid, rest wide. Results
        # are re-ranked by stars after merging.
        core = max(1, len(topics) // 3)
        queries = []
        for idx, topic in enumerate(topics):
            if idx < core:
                floor = STAR_FLOOR_CORE
            elif idx < core * 2:
                floor = STAR_FLOOR_MID
            else:
                floor = STAR_FLOOR_WIDE
            queries.append(f"topic:{topic} pushed:>={cutoff} stars:>{floor}")
        return queries

    async def run_query(self, client, query, per_query):
        """
        Executes one repository search, retrying once after a short rate-limit wait.
        Returns repository records carrying an id; [] on any failure.
        """
        params = {"q": query, "sort": "stars", "order": "desc", "per_page": per_query}
        try:
            async with self.semaphore:
                response = await client.get(self.base_url, params=params,
                                            headers=build_headers())
                wait = rate_limit_wait(response)
                if wait is not None and wait <= MAX_RATE_LIMIT_WAIT_S:
                    await asyncio.sleep(wait + 1)
                    response = await client.get(self.base_url, params=params,
                                                headers=build_headers())
            # Rate limits and server errors degrade to no candidates.
            if response.status_code != 200:
                print(f"   ⚠️  GitHub search '{query}' returned {response.status_code}")
                return []
            payload = response.json()
            if not isinstance(payload, dict):
                return []
            # Match count before any page limit; feeds volume_report().
            if isinstance(payload.get('total_count'), int):
                self.search_totals.append(payload['total_count'])
            items = payload.get('items') or []
            # Id-less records cannot be de-duplicated or stored.
            return [i for i in items if isinstance(i, dict) and i.get('id') is not None]
        except Exception as e:
            print(f"   ⚠️  GitHub search '{query}' failed: {e}")
            return []

    async def process_repo(self, client, item):
        """Deep-fetches, normalises and gates one repository; None if rejected."""
        try:
            repo_name = item['name']
            description = (item.get('description') or '').strip()
            owner = item['owner']['login']

            async with self.semaphore:
                # Unmetered CDN first; core quota only for what it missed.
                readme = await fetch_readme_raw(
                    client, owner, repo_name, item.get('default_branch')
                )
                if not readme and self.readme_budget > 0:
                    self.readme_budget -= 1
                    readme = await fetch_readme(client, owner, repo_name)

            # The repo name stays out of the body, or KeyBERT ranks echoes of it above
            # the subject matter.
            if description and description[-1] not in '.!?':
                description += '.'
            content = f"{description} {readme}".strip()

            post = {
                'source_platform': self.platform_name,
                'external_id': str(item['id']),
                'title': repo_name,
                'content': content,
                'author': owner,
                'url': item['html_url'],
                'raw_score': item.get('stargazers_count') or 0,
                'published_at': item.get('updated_at', ''),
            }

            if self.is_quality_content(post):
                return post

        except Exception as e:
            # Malformed record: counted as 'unreachable' by the funnel residual.
            print(f"Error processing GitHub repo {item.get('name')}: {e}")

        return None

    async def check_rate_budget(self, client):
        """
        Sets the README fallback budget from the live core API quota, keeping
        GITHUB_RATE_LIMIT_FLOOR requests in reserve for whatever runs next.
        """
        try:
            resp = await client.get("https://api.github.com/rate_limit", headers=build_headers())
            if resp.status_code == 200:
                remaining = resp.json()['resources']['core']['remaining']
                self.readme_budget = max(0, remaining - GITHUB_RATE_LIMIT_FLOOR)
                if self.readme_budget < self.target_posts:
                    print(f"   ⚠️  GitHub core budget {remaining}/hr — at most "
                          f"{self.readme_budget} READMEs may use the API fallback this "
                          f"cycle. README.md still arrives over the unmetered CDN; only "
                          f"oddly-named ones are affected. Set GITHUB_TOKEN to raise the "
                          f"limit to 5000/hr.")
        except Exception:
            # Budget unknown: proceed optimistically rather than starving the fallback.
            self.readme_budget = self.max_candidates

    async def collect(self, client: httpx.AsyncClient):
        """
        Runs the GitHub stage of a scan: search, merge, deep fetch and gate.

        Returns:
            list[dict]: Up to `target_posts` accepted posts.
        """
        try:
            # Budget must be known before any metered fetch is issued.
            await self.check_rate_budget(client)

            queries = self.build_queries()
            if not queries:
                return []
            # A search page costs one request whatever its size, and deep fetches only
            # run on the candidates needed, so each query asks for a generous page.
            per_query = min(
                GITHUB_MAX_PER_PAGE,
                max(25, self.max_candidates // len(queries) * 2),
            )

            search_results = await asyncio.gather(
                *[self.run_query(client, q, per_query) for q in queries]
            )
            items = self.merge_candidates(search_results, 'id', 'stargazers_count')

            return await self.collect_until_target(
                items, lambda item: self.process_repo(client, item)
            )

        except Exception as e:
            # Fault Isolation: an orchestration failure costs this platform, not the scan.
            print(f"Critical Error in GitHub Collector orchestration: {e}")
            return []
