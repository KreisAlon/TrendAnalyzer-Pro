import base64
import os
import re
import httpx
import asyncio
from datetime import datetime, timedelta, timezone
from collectors.base import BaseCollector
from config import (
    MAX_POSTS_PER_PLATFORM, TARGET_POSTS_PER_PLATFORM, TEXT_PREVIEW_LENGTH,
    MAX_CONCURRENT_GITHUB, GITHUB_RATE_LIMIT_FLOOR, DEFAULT_GITHUB_TOPICS,
)

# GitHub's search endpoint caps per_page at 100; asking for more is silently clamped.
GITHUB_MAX_PER_PAGE = 100

# Star floors applied by topic rank. The leading topics are the ones most specific
# to the target, so they can afford to demand popularity; the tail exists purely to
# keep the candidate pool deep enough to absorb gatekeeper rejections, and a high
# floor there would starve it.
STAR_FLOOR_CORE = 200
STAR_FLOOR_MID = 100
STAR_FLOOR_WIDE = 50

# README boilerplate that carries no semantic signal. Stripped before truncation --
# otherwise the first 800 characters of a project like onnx are pure badge markup
# ("[![PyPI - Version]( [![CI]( [![CII Best Practices](") and the extracted keywords
# describe the badges rather than the project.
_FRONTMATTER_RE = re.compile(r'\A---\n.*?\n---\n', re.S)
_CODE_FENCE_RE = re.compile(r'```.*?```', re.S)
_HTML_COMMENT_RE = re.compile(r'<!--.*?-->', re.S)
_BADGE_RE = re.compile(r'\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)')   # [![alt](img)](link)
_IMAGE_RE = re.compile(r'!\[[^\]]*\]\([^)]*\)')                # ![alt](img)
_LINK_RE = re.compile(r'\[([^\]]+)\]\([^)]*\)')                # [text](url) -> text
_HEADING_RE = re.compile(r'^\s{0,3}#{1,6}\s*', re.M)


def clean_markdown(text):
    """Strips markdown chrome so only prose reaches the NLP stage."""
    if not text: return ""
    text = _FRONTMATTER_RE.sub('', text)
    text = _HTML_COMMENT_RE.sub(' ', text)
    text = _CODE_FENCE_RE.sub(' ', text)
    text = _BADGE_RE.sub(' ', text)
    text = _IMAGE_RE.sub(' ', text)
    text = _LINK_RE.sub(r'\1', text)
    text = _HEADING_RE.sub('', text)
    # Drop table rows and horizontal rules, which survive as pipe/dash noise
    text = re.sub(r'^\s*\|.*\|\s*$', ' ', text, flags=re.M)
    text = re.sub(r'^\s*[-=*_]{3,}\s*$', ' ', text, flags=re.M)
    return re.sub(r'\s+', ' ', text).strip()


def build_headers():
    """
    Builds request headers, adding auth when a GITHUB_TOKEN is present.

    Unauthenticated clients get 60 core requests/hour, and a single cycle wants roughly
    one README per repository. A token raises that ceiling to 5000/hour.
    """
    headers = {'Accept': 'application/vnd.github.v3+json'}
    token = os.environ.get('GITHUB_TOKEN')
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return headers


async def fetch_readme_raw(client, owner, repo, branch):
    """
    Fetches the README straight from raw.githubusercontent.com.

    This is the primary path because the CDN is NOT metered against the core API quota.
    That quota was the hidden limit behind short GitHub posts: unauthenticated clients
    get 60 core requests/hour, so by the second cycle readme_budget was 0, every repo
    was stored on its description alone (~110-260 characters) and the bodies the NLP
    stage needs never arrived at all.

    Only the two common casings are attempted -- a miss costs a 404 and falls through
    to the metered API, which resolves any filename.
    """
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
    Fetches and decodes the README via the metered core API.

    Fallback for repositories whose README is not named README.md (README.rst, .txt,
    docs/README.md), which the raw CDN path cannot guess.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/readme"
    try:
        # A 10s timeout: the previous 5s dropped the READMEs of large monorepos, which
        # are exactly the repositories worth keeping. The body is the only text these
        # posts have, so timing out here costs the whole post.
        response = await client.get(url, headers=build_headers(), timeout=10.0)

        if response.status_code == 200:
            payload = response.json()
            if payload.get('encoding') != 'base64':
                return ""
            raw = base64.b64decode(payload.get('content', '')).decode('utf-8', errors='ignore')
            # Strip boilerplate FIRST, then truncate, so the budget is spent on prose --
            # and cut on a sentence boundary rather than mid-word.
            return BaseCollector.truncate_at_boundary(clean_markdown(raw), TEXT_PREVIEW_LENGTH)
    except Exception:
        return ""
    return ""

class GitHubCollector(BaseCollector):
    """
    Collector for GitHub repositories.
    Implements concurrent fetching for README files to avoid sequential blocking.
    """
    def __init__(self):
        super().__init__("GitHub")
        self.base_url = "https://api.github.com/search/repositories"
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_GITHUB)
        # How many READMEs this cycle may fall back to the METERED core API for.
        # Unauthenticated clients get 60 core requests/hour in total, so an uncapped
        # cycle drains the whole quota in one pass. README.md itself now arrives over
        # the unmetered CDN, so this budget only bounds the odd-filename fallback.
        self.readme_budget = 0
        # Repo descriptions are short but hand-written and topic-filtered, so they are
        # worth keeping even without a README body.
        self.min_content_chars = 100

    def topics(self):
        """
        The topic slugs to fan out over: the run's plan, or the curated fallback.

        Read through the profile rather than a class constant so a run can be
        retargeted in memory. The planner's slugs are validated against GitHub's
        slug grammar in topics.py before they reach this point, because a phrase
        in this position is not rejected by GitHub -- it is silently reinterpreted
        as free text, which is the exact failure the curated list replaced.
        """
        return self.planned('github_topics', DEFAULT_GITHUB_TOPICS)

    def build_queries(self):
        """
        GitHub's repository search does NOT support boolean OR, so the previous
        "AI OR LLM OR GPT OR 'Machine Learning'" was silently parsed as loose free text.
        Instead we fan out over curated topics and merge. 'pushed' scopes the window to
        genuinely active projects; sorting by stars surfaces the ones that matter.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).strftime('%Y-%m-%d')
        topics = self.topics()

        # Two tiers, as before -- but the split is now derived from POSITION rather
        # than written out per topic. Both the curated list and the planner return
        # their slugs best-first, so the leading third are treated as the core and
        # held to a high star floor, and the tail widens the pool at a lower floor
        # so it survives the gatekeeper's rejection rate. Results are merged and
        # re-sorted by stars, so the wider tier only ever supplies the repositories
        # needed to top the cycle up to TARGET_POSTS_PER_PLATFORM.
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
        """Executes one search query and returns its raw items."""
        params = {"q": query, "sort": "stars", "order": "desc", "per_page": per_query}
        try:
            response = await client.get(self.base_url, params=params, headers=build_headers())
            if response.status_code != 200:
                print(f"   ⚠️  GitHub search '{query}' returned {response.status_code}")
                return []
            payload = response.json()
            # total_count is exact and free with the page we asked for anyway; it is
            # the number of active repositories matching this query, before any cap.
            if isinstance(payload.get('total_count'), int):
                self.search_totals.append(payload['total_count'])
            return payload.get('items', [])
        except Exception as e:
            print(f"   ⚠️  GitHub search '{query}' failed: {e}")
            return []

    async def process_repo(self, client, item):
        """
        Helper function to process a single repository concurrently.
        Fetches the README, normalizes the data, and runs quality checks.
        """
        try:
            repo_name = item['name']
            description = (item.get('description') or '').strip()

            # Execute the concurrent deep fetch for the README content, bounded by a
            # semaphore so we never fire one request per repository simultaneously.
            owner = item['owner']['login']
            async with self.semaphore:
                # Unmetered CDN first; the core-API quota is spent only on the
                # repositories the CDN could not resolve.
                readme = await fetch_readme_raw(
                    client, owner, repo_name, item.get('default_branch')
                )
                if not readme and self.readme_budget > 0:
                    self.readme_budget -= 1
                    readme = await fetch_readme(client, owner, repo_name)

            # The repo name is deliberately NOT repeated here: the old template
            # ("Project: {name}. Description: ...") made KeyBERT rank name echoes
            # ("project xla", "xla project") above the actual subject matter.
            #
            # The separator is added only when the description does not already end in
            # terminal punctuation. The old f"{description}. {readme}" appended one
            # unconditionally, so every README-less repo -- which, with the README budget
            # exhausted, meant all of them -- was stored ending in a bare "..".
            if description and description[-1] not in '.!?':
                description += '.'
            content = f"{description} {readme}".strip()

            post = {
                'source_platform': self.platform_name,
                'external_id': str(item['id']),
                'title': repo_name,
                'content': content,
                'author': item['owner']['login'],
                'url': item['html_url'],
                'raw_score': item['stargazers_count'],
                'sentiment': self.analyze_sentiment(content),
                'published_at': item['updated_at']
            }

            # Pass through the semantic gatekeeper to extract keywords
            if self.is_quality_content(post):
                return post

        except Exception as e:
            print(f"Error processing GitHub repo {item.get('name')}: {e}")

        return None

    async def check_rate_budget(self, client):
        """
        Reads the remaining core-API budget so README fetching can degrade gracefully
        instead of hammering a quota that is already spent.
        """
        try:
            resp = await client.get("https://api.github.com/rate_limit", headers=build_headers())
            if resp.status_code == 200:
                remaining = resp.json()['resources']['core']['remaining']
                # Always leave GITHUB_RATE_LIMIT_FLOOR in reserve so a cycle cannot
                # leave the quota at zero for whatever runs next.
                self.readme_budget = max(0, remaining - GITHUB_RATE_LIMIT_FLOOR)
                if self.readme_budget < TARGET_POSTS_PER_PLATFORM:
                    print(f"   ⚠️  GitHub core budget {remaining}/hr — at most "
                          f"{self.readme_budget} READMEs may use the API fallback this "
                          f"cycle. README.md still arrives over the unmetered CDN; only "
                          f"oddly-named ones are affected. Set GITHUB_TOKEN to raise the "
                          f"limit to 5000/hr.")
        except Exception:
            self.readme_budget = MAX_POSTS_PER_PLATFORM  # Budget unknown; proceed optimistically

    async def collect(self, client: httpx.AsyncClient):
        """Main entry point. Fetches top repositories and dispatches concurrent tasks."""
        topic_label = getattr(self.topic_profile, 'target_topic', 'AI')
        print(f"--- {self.platform_name}: Searching for trending '{topic_label}' repos "
              f"concurrently... ---")
        try:
            await self.check_rate_budget(client)

            queries = self.build_queries()
            if not queries:
                return []
            # Each query is asked for a full page rather than an even share of the target.
            # Search pages are one request each regardless of size, so the wide pool is
            # effectively free, and process_repo() is what actually costs quota -- and it
            # only runs on as many candidates as the target needs.
            per_query = min(
                GITHUB_MAX_PER_PAGE,
                max(25, MAX_POSTS_PER_PLATFORM // len(queries) * 2),
            )

            search_results = await asyncio.gather(
                *[self.run_query(client, q, per_query) for q in queries]
            )

            # Merge the topic queries, deduplicating repositories that carry several
            # of the topics we search for.
            self.record_candidates(search_results, 'id')
            unique_items = {}
            for items in search_results:
                for item in items:
                    unique_items.setdefault(item['id'], item)

            # Best-first, then oversampled: the pool is deliberately larger than the
            # target so gatekeeper rejections are absorbed by lower-ranked repositories
            # instead of shrinking the stored count.
            items = sorted(
                unique_items.values(),
                key=lambda i: i.get('stargazers_count', 0),
                reverse=True
            )[:MAX_POSTS_PER_PLATFORM]

            # Walk the pool in concurrent waves, stopping once TARGET_POSTS_PER_PLATFORM
            # repositories have cleared the quality gate.
            return await self.collect_until_target(
                items, lambda item: self.process_repo(client, item)
            )

        except Exception as e:
            print(f"Critical Error in GitHub Collector orchestration: {e}")
            return []
