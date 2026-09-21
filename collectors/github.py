import base64
import os
import re
import httpx
import asyncio
from datetime import datetime, timedelta, timezone
from collectors.base import BaseCollector
from config import (
    MAX_POSTS_PER_PLATFORM, TEXT_PREVIEW_LENGTH,
    MAX_CONCURRENT_GITHUB, GITHUB_RATE_LIMIT_FLOOR,
)

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


async def fetch_readme(client, owner, repo):
    """
    Fetches and decodes the README.md for a given repository.
    Includes a timeout to prevent hanging on unresponsive servers or massive files.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/readme"
    try:
        # Added a 5.0 second timeout for external safety
        response = await client.get(url, headers=build_headers(), timeout=5.0)

        if response.status_code == 200:
            payload = response.json()
            if payload.get('encoding') != 'base64':
                return ""
            raw = base64.b64decode(payload.get('content', '')).decode('utf-8', errors='ignore')
            # Strip boilerplate FIRST, then truncate, so the budget is spent on prose
            return clean_markdown(raw)[:TEXT_PREVIEW_LENGTH]
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
        # How many READMEs this cycle may fetch. Unauthenticated clients get 60 core
        # requests/hour in total, so an uncapped cycle drains the entire quota in one
        # pass; repos beyond the cap are ingested on their description alone.
        self.readme_budget = 0
        # Repo descriptions are short but hand-written and topic-filtered, so they are
        # worth keeping even without a README body.
        self.min_content_chars = 100

    def build_queries(self):
        """
        GitHub's repository search does NOT support boolean OR, so the previous
        "AI OR LLM OR GPT OR 'Machine Learning'" was silently parsed as loose free text.
        Instead we fan out over curated topics and merge. 'pushed' scopes the window to
        genuinely active projects; sorting by stars surfaces the ones that matter.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).strftime('%Y-%m-%d')
        return [
            f"topic:llm pushed:>={cutoff} stars:>200",
            f"topic:machine-learning pushed:>={cutoff} stars:>200",
            f"topic:ai-agents pushed:>={cutoff} stars:>100",
            f"topic:generative-ai pushed:>={cutoff} stars:>100",
        ]

    async def run_query(self, client, query, per_query):
        """Executes one search query and returns its raw items."""
        params = {"q": query, "sort": "stars", "order": "desc", "per_page": per_query}
        try:
            response = await client.get(self.base_url, params=params, headers=build_headers())
            if response.status_code != 200:
                print(f"   ⚠️  GitHub search '{query}' returned {response.status_code}")
                return []
            return response.json().get('items', [])
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
            description = item.get('description') or ''

            # Execute the concurrent deep fetch for the README content, bounded by a
            # semaphore so we never fire one request per repository simultaneously.
            readme = ""
            if self.readme_budget > 0:
                self.readme_budget -= 1
                async with self.semaphore:
                    readme = await fetch_readme(client, item['owner']['login'], repo_name)

            # The repo name is deliberately NOT repeated here: the old template
            # ("Project: {name}. Description: ...") made KeyBERT rank name echoes
            # ("project xla", "xla project") above the actual subject matter.
            content = f"{description}. {readme}".strip()

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
                if self.readme_budget < MAX_POSTS_PER_PLATFORM:
                    print(f"   ⚠️  GitHub core budget {remaining}/hr — fetching at most "
                          f"{self.readme_budget} READMEs this cycle (rest use descriptions "
                          f"only). Set GITHUB_TOKEN to raise the limit to 5000/hr.")
        except Exception:
            self.readme_budget = MAX_POSTS_PER_PLATFORM  # Budget unknown; proceed optimistically

    async def collect(self, client: httpx.AsyncClient):
        """Main entry point. Fetches top repositories and dispatches concurrent tasks."""
        print(f"--- {self.platform_name}: Searching for trending AI repos concurrently... ---")
        try:
            await self.check_rate_budget(client)

            queries = self.build_queries()
            per_query = max(10, MAX_POSTS_PER_PLATFORM // len(queries))

            search_results = await asyncio.gather(
                *[self.run_query(client, q, per_query) for q in queries]
            )

            # Merge the topic queries, deduplicating repositories that carry several
            # of the topics we search for.
            unique_items = {}
            for items in search_results:
                for item in items:
                    unique_items.setdefault(item['id'], item)

            items = sorted(
                unique_items.values(),
                key=lambda i: i.get('stargazers_count', 0),
                reverse=True
            )[:MAX_POSTS_PER_PLATFORM]

            # Concurrency implementation: Dispatch a task to fetch the README for each repo
            tasks = [self.process_repo(client, item) for item in items]
            results = await asyncio.gather(*tasks)

            # Filter out invalid or failed repositories
            return [post for post in results if post is not None]

        except Exception as e:
            print(f"Critical Error in GitHub Collector orchestration: {e}")
            return []
