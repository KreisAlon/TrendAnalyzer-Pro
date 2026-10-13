"""
One collection cycle: plan, collect from all platforms concurrently, apply the
anti-noise floors, then hand accepted posts to scoring. No Streamlit dependency:
progress is reported through an optional callback.
"""

import asyncio
import contextlib
import time
from datetime import datetime

import httpx

import config
from engine.models import (ScanCancelled, ScanResult, NO_RESULTS_MESSAGE,
                           LOW_VOLUME_MESSAGE, INSUFFICIENT_PLATFORMS_MESSAGE)
from engine.planning import DynamicTopicManager
from engine.scoring import _store_and_score, _score_background

# A trend is cross-community by definition, so a cycle must reach this many platforms.
MIN_PLATFORMS = 3


def noise_floors(max_posts):
    """
    Returns (per-platform floor, total floor) for a scan of `max_posts` per platform.
    A generic word matches a few unrelated posts per platform; a real technology
    clears these easily. Reduced-depth scans use lower floors.
    """
    return (config.NOISE_FLOORS_REDUCED if max_posts <= config.REDUCED_SCAN_MAX_POSTS
            else config.NOISE_FLOORS_FULL)


# Cancellation-check / progress "tick" interval while platforms collect.
TICK_S = 0.5


def _emit(progress, stage, **info):
    """
    Reports a stage to the optional `progress(stage, info)` callback. Ordinary
    callback errors are swallowed; BaseException is not, because Streamlit stops a
    script with one and the scan must stop with it.
    """
    if progress is None:
        return
    try:
        progress(stage, info)
    except Exception as exc:                      # noqa: BLE001
        print(f"   ⚠️  Progress callback failed at '{stage}': {exc}")


def _no_results(result, started):
    """Marks `result` as a legitimate empty answer and stamps its duration."""
    result.status = "no_results"
    result.error = NO_RESULTS_MESSAGE
    result.duration_s = time.monotonic() - started
    return result


async def run_cycle(topic, progress=None, background=False, max_posts=None, cancel=None):
    """
    Executes one collection cycle for `topic`.

    Stages reported to `progress(stage, info)`, in order:
        loading       -- collectors (and the KeyBERT model) are being imported
        planning      -- the search plan is being resolved
        planned       -- info['profile'] is the TopicProfile in use
        collecting    -- all platforms have been dispatched
        platform_done -- info['platform'], info['count'], info['volume'] (per platform)
        tick          -- info['elapsed'], every TICK_S seconds while collecting
        scoring       -- results are being stored, normalised and pruned

    Setting `cancel` (or a BaseException raised by the callback on "tick") aborts
    the cycle within TICK_S, before anything is persisted. Background mode scores
    in a scratch DB and leaves the user's corpus, plan and volume untouched. A new
    topic replaces the corpus only once results are in hand, so an empty scan keeps
    the previous one.

    Args:
        topic (str): A topic already accepted by `validate_topic`.
        progress (callable | None): Optional stage callback.
        background (bool): Run in isolated background mode.
        max_posts (int | None): Per-platform post target; defaults to
            config.BACKGROUND_MAX_POSTS or config.MANUAL_MAX_POSTS by mode.
        cancel (threading.Event | None): Cooperative cancellation flag.

    Returns:
        ScanResult: The outcome of the cycle.
    """
    started = time.monotonic()
    result = ScanResult(topic=topic)

    # Lazy import: collectors load KeyBERT, which would slow dashboard start-up.
    _emit(progress, "loading")
    from collectors.github import GitHubCollector
    from collectors.hacker_news import HackerNewsCollector
    from collectors.mastodon import MastodonCollector
    from collectors.devto import DevToCollector

    print("\n" + "=" * 80)
    print(f"🕒 {'BACKGROUND ' if background else ''}SCAN STARTING | TOPIC: {topic} | "
          f"TIME: {datetime.now().strftime('%H:%M:%S')}")
    print("=" * 80)

    # Fresh instances so per-run funnel state starts at zero; order matches config.PLATFORMS.
    collectors = [GitHubCollector(), HackerNewsCollector(),
                  MastodonCollector(), DevToCollector()]
    max_posts = max_posts or (config.BACKGROUND_MAX_POSTS if background
                              else config.MANUAL_MAX_POSTS)
    for collector in collectors:
        collector.set_max_posts(max_posts)
    min_per_platform, min_total = noise_floors(max_posts)

    # 0. Planning, broadcast to every collector.
    _emit(progress, "planning")
    profile = DynamicTopicManager(topic).resolve()
    for collector in collectors:
        collector.apply_topic_profile(profile)
    result.plan_source = profile.source
    _emit(progress, "planned", profile=profile)

    # Nothing searchable: stop before a single network request is made.
    if profile.is_empty():
        return _no_results(result, started)

    # 1. Ingest from all platforms concurrently.
    async def collect_one(collector, client):
        """Runs one collector in isolation and returns (platform, posts, report)."""
        try:
            # Hard ceiling: a stalled platform is cancelled, never awaited forever.
            posts = await asyncio.wait_for(collector.collect(client),
                                           timeout=config.PLATFORM_TIMEOUT_S)
        except asyncio.TimeoutError:
            print(f"   ⏱️  {collector.platform_name} timed out after "
                  f"{config.PLATFORM_TIMEOUT_S}s; continuing without it.")
            posts = []
        except Exception as exc:                  # noqa: BLE001
            # Fault isolation: one failing platform must not cost the others.
            print(f"   ❌ {collector.platform_name} collector failed: {exc}")
            posts = []
        # Funnel statistics are reported even for an empty result.
        report = collector.volume_report(len(posts))
        _emit(progress, "platform_done", platform=collector.platform_name,
              count=len(posts), volume=report)
        return collector.platform_name, posts, report

    _emit(progress, "collecting")

    # One shared client (connection pooling); gather() runs platforms concurrently.
    async def collect_all():
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT_S, follow_redirects=True,
                                     headers={'User-Agent': 'TrendAnalyzer/6.0'}) as client:
            return await asyncio.gather(*(collect_one(c, client) for c in collectors))

    platform_results = await _run_cancellable(collect_all(), progress, cancel, started)
    # Last checkpoint before anything is written.
    if cancel is not None and cancel.is_set():
        raise ScanCancelled()

    all_posts = []
    raw_total = 0
    for platform, posts, report in platform_results:
        raw_total += len(posts)
        result.volume[platform] = report
        # Anti-noise: a thin platform sample is more likely noise than signal.
        if len(posts) < min_per_platform:
            if posts:
                print(f"   🔇 {platform}: only {len(posts)} posts "
                      f"(< {min_per_platform}); platform dropped.")
            posts = []
            report['stored'] = 0
        result.per_platform[platform] = len(posts)
        all_posts.extend(posts)
    result.fetched = len(all_posts)

    # Abort before touching the database, so the previous corpus stays intact.
    if not raw_total:
        return _no_results(result, started)
    if len(all_posts) < min_total:
        print(f">>> ⛔ Only {len(all_posts)} posts survived the volume filter; "
              f"need {min_total}. Nothing saved.")
        result = _no_results(result, started)
        result.error = LOW_VOLUME_MESSAGE
        return result

    # Cross-platform minimum, checked before persistence.
    platforms_with_posts = sum(1 for n in result.per_platform.values() if n > 0)
    if platforms_with_posts < MIN_PLATFORMS:
        print(f">>> ⛔ Only {platforms_with_posts} platform(s) returned posts; "
              f"need {MIN_PLATFORMS}. Nothing saved.")
        result = _no_results(result, started)
        result.error = INSUFFICIENT_PLATFORMS_MESSAGE
        return result

    _emit(progress, "scoring")
    if background:
        _score_background(topic, collectors, all_posts, result, max_posts)
    else:
        _store_and_score(topic, profile, collectors, all_posts, result)

    result.duration_s = time.monotonic() - started
    print(f">>> ✅ Scan complete in {result.duration_s:.0f}s. "
          f"Fetched {result.fetched} items.")
    return result


async def _run_cancellable(coro, progress, cancel, started):
    """
    Awaits `coro`, checking every TICK_S seconds whether to abandon it: the work is
    cancelled when `cancel` is set (raises ScanCancelled) or when the callback
    raises a BaseException on "tick" (re-raised unchanged).
    """
    work = asyncio.ensure_future(coro)
    try:
        while True:
            done, _ = await asyncio.wait({work}, timeout=TICK_S)
            if done:
                return work.result()
            if cancel is not None and cancel.is_set():
                raise ScanCancelled()
            _emit(progress, "tick", elapsed=time.monotonic() - started)
    except BaseException:
        work.cancel()
        with contextlib.suppress(BaseException):
            await work
        raise
