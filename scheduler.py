"""
Background orchestrator for TrendAnalyzer Pro (APScheduler).

One `BackgroundScheduler` per process runs `background_cycle()` every
`config.SCHEDULER_INTERVAL_HOURS`:

    1. Eviction  -- `TrendManager.cleanup_old_records()` enforces the TTL.
    2. Discovery -- `discovery_phase()` reads today's Hacker News and Dev.to front
                    pages and lets KeyBERT pick the topics (no fixed seed list).
    3. Tracking  -- each topic goes through `engine.scan_background()`, the normal
                    cross-platform pipeline in isolated, reduced-depth mode. Results
                    land in `live_trends` / `live_posts`.

Cold start: until `live_trends` holds SEED_TOPIC_COUNT distinct topics, each cycle
schedules a top-up that re-runs discovery for topics not yet tried, because the
dashboard blocks on a loading screen until the brief is full.

Every stage is guarded, so a failing cycle is logged and the next one still runs.
"""

import asyncio
import threading
from collections import Counter
from datetime import datetime, timedelta

import httpx
from apscheduler.schedulers.background import BackgroundScheduler

import config
import engine
from database.manager import TrendManager

JOB_ID = "autonomous_trend_scan"
RETRY_JOB_ID = "cold_start_retry"
COLD_START_MAX_DELAY_S = 300

# The running scheduler, so a cycle can schedule its own cold-start retry.
_scheduler = None

# Worker progress for the dashboard's cold-start screen; read via status().
_status_lock = threading.Lock()
_status = {"running": False, "seeds": [], "done": 0, "cycles_completed": 0,
           "retries": 0}
# Topics tried this process lifetime, so top-ups never re-scan a failed topic.
_attempted = set()
# The interval job and the cold-start retry are separate APScheduler jobs, so
# max_instances alone cannot stop them overlapping; this lock does.
_cycle_lock = threading.Lock()


def _set_status(**changes):
    with _status_lock:
        _status.update(changes)


def status():
    """
    Returns a snapshot of the worker's progress.

    Returns:
        dict: running (bool), seeds (list[str]), done (seeds finished this cycle),
        cycles_completed (int, cycles finished since start-up, failed or not),
        retries (int, cold-start retries scheduled so far).
    """
    with _status_lock:
        return {**_status, "seeds": list(_status["seeds"])}


async def _get_json(client, url, **params):
    """GET -> parsed JSON, or None on any failure (discovery must never raise)."""
    try:
        resp = await client.get(url, params=params or None)
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"   ⚠️  Discovery request failed ({url}): {exc}")
        return None


async def _fetch_front_pages():
    """
    Fetches HN front-page titles and Dev.to top/latest articles concurrently.
    Either source may fail independently.

    Returns:
        tuple[list[str], list[dict]]: (HN titles, Dev.to article objects).
    """
    async with httpx.AsyncClient(timeout=config.DISCOVERY_TIMEOUT_S,
                                 headers={'User-Agent': 'TrendAnalyzer/6.0'}) as client:
        ids, top, latest = await asyncio.gather(
            _get_json(client, config.HN_TOP_STORIES_URL),
            _get_json(client, config.DEVTO_ARTICLES_URL, top=1,
                      per_page=config.DISCOVERY_DEVTO_ARTICLES),
            _get_json(client, config.DEVTO_ARTICLES_URL,
                      per_page=config.DISCOVERY_DEVTO_ARTICLES),
        )
        ids = ids[:config.DISCOVERY_HN_STORIES] if isinstance(ids, list) else []
        items = await asyncio.gather(
            *(_get_json(client, config.HN_ITEM_URL.format(id=i)) for i in ids))
    titles = [i['title'] for i in items if isinstance(i, dict) and i.get('title')]
    articles = [a for batch in (top, latest) if isinstance(batch, list)
                for a in batch if isinstance(a, dict)]
    return titles, articles


def _usable(term):
    """A candidate topic: not a meta/format tag, and a valid search topic."""
    return (term and term not in config.DISCOVERY_TAG_BLOCKLIST
            and not term.endswith('challenge') and not engine.validate_topic(term)[1])


def discovery_phase(count=None, exclude=()):
    """
    Derives this cycle's topics from today's front pages.

    Candidates are Dev.to article tags; KeyBERT ranks them by centrality to the
    combined HN + Dev.to headlines (MMR for diversity). Without Dev.to, KeyBERT
    extracts single words from the HN headlines instead.

    Args:
        count (int | None): Topics wanted; defaults to config.SEED_TOPIC_COUNT.
        exclude (iterable[str]): Topics to skip (already saved or already tried).

    Returns:
        list[str]: Up to `count` topics, best first. Empty if both sources fail.
    """
    count = count or config.SEED_TOPIC_COUNT
    exclude = {e.lower() for e in exclude}
    titles, articles = asyncio.run(_fetch_front_pages())
    titles += [a['title'] for a in articles if a.get('title')]
    if not titles:
        print("   ⚠️  Discovery: no headlines fetched.")
        return []

    tag_counts = Counter(t.lower() for a in articles for t in a.get('tag_list') or []
                         if isinstance(t, str))
    candidates = [t for t, n in tag_counts.items()
                  if n >= config.DISCOVERY_MIN_TAG_COUNT and _usable(t) and t not in exclude]

    # Lazy import: loading KeyBERT costs seconds and is only needed here.
    from collectors.nlp import kw_model, _KW_LOCK
    text = ". ".join(titles)
    with _KW_LOCK:
        if candidates:
            ranked = kw_model.extract_keywords(text, candidates=candidates,
                                               top_n=len(candidates), use_mmr=True,
                                               diversity=0.3)
        else:
            ranked = kw_model.extract_keywords(text, keyphrase_ngram_range=(1, 1),
                                               stop_words='english', top_n=count * 4,
                                               use_mmr=True, diversity=0.5)
    topics = [k for k, _ in ranked if _usable(k) and k not in exclude][:count]
    print(f"🔭 Discovery: {len(titles)} headlines, {len(candidates)} candidate tags "
          f"-> {', '.join(topics) or 'nothing'}")
    return topics


def background_cycle(exclude=None):
    """
    One autonomous cycle: TTL cleanup, discovery, then a scan per discovered topic.

    Args:
        exclude (iterable[str] | None): Topics discovery must skip; set by a
            cold-start top-up, None for a regular cycle.
    """
    if not _cycle_lock.acquire(blocking=False):
        # The running cycle schedules its own top-up when it finishes.
        print("🤖 Background cycle skipped: another cycle is still running.")
        return
    print(f"\n🤖 Background cycle started at {datetime.now():%H:%M:%S}"
          f"{' (cold-start top-up)' if exclude is not None else ''}")
    _set_status(running=True, seeds=[], done=0)
    try:
        _run_cycle_stages(exclude)
    finally:
        with _status_lock:
            _status["running"] = False
            _status["cycles_completed"] += 1
        _cycle_lock.release()
        _schedule_cold_start_retry()
    print("🤖 Background cycle finished.\n")


def _saved_topics():
    """Distinct topics already in live_trends; errors count as none saved."""
    try:
        return TrendManager().live_topics()
    except Exception as exc:                      # noqa: BLE001
        print(f"   ⚠️  Could not read live_trends: {exc}")
        return set()


def _schedule_cold_start_retry():
    """
    While live_trends holds fewer than SEED_TOPIC_COUNT topics, schedules a one-off
    discovery cycle that skips every topic already saved or tried. Never raises.
    """
    if _scheduler is None:
        return
    saved = _saved_topics()
    if len(saved) >= config.SEED_TOPIC_COUNT:
        return
    # Linear backoff, capped, so an unchanged front page is not re-read every few seconds.
    delay = min(COLD_START_MAX_DELAY_S, config.COLD_START_RETRY_S * (1 + status()["retries"]))
    try:
        _scheduler.add_job(
            background_cycle, 'date', kwargs={'exclude': sorted(saved | _attempted)},
            id=RETRY_JOB_ID, replace_existing=True, misfire_grace_time=300,
            run_date=datetime.now() + timedelta(seconds=delay),
        )
        with _status_lock:
            _status["retries"] += 1
        print(f"🔁 {len(saved)}/{config.SEED_TOPIC_COUNT} live topics saved; re-running "
              f"discovery in {delay}s.")
    except Exception as exc:                      # noqa: BLE001
        print(f"   ⚠️  Could not schedule cold-start top-up: {exc}")


def _run_cycle_stages(exclude=None):
    """The stages of `background_cycle()`, each guarded independently."""
    try:
        TrendManager().cleanup_old_records()
    except Exception as exc:                      # noqa: BLE001
        print(f"   ⚠️  TTL cleanup failed: {exc}")

    # A top-up only needs the missing topics; a regular cycle refreshes all of them.
    count = config.SEED_TOPIC_COUNT
    if exclude is not None:
        count = max(1, config.SEED_TOPIC_COUNT - len(_saved_topics()))
    try:
        topics = discovery_phase(count, exclude or ())
    except Exception as exc:                      # noqa: BLE001
        print(f"   ⚠️  Discovery failed: {exc}")
        topics = []
    _set_status(seeds=topics)

    for done, topic in enumerate(topics, 1):
        _attempted.add(topic)
        try:
            result = engine.scan_background(topic, wait_s=config.SCHEDULER_LOCK_WAIT_S,
                                            max_posts=config.BACKGROUND_MAX_POSTS)
            print(f"🤖 {topic}: {result.status}"
                  f"{' - ' + result.error if result.error else ''}")
        except Exception as exc:                  # noqa: BLE001
            print(f"   ⚠️  Background scan of {topic!r} failed: {exc}")
        _set_status(done=done)


def start_scheduler():
    """
    Starts and returns the process-wide scheduler. Call once per process
    (ui/app.py wraps it in `st.cache_resource`).
    """
    global _scheduler
    scheduler = BackgroundScheduler(daemon=True)
    # Published before the first job runs, so even an instant failure can retry.
    _scheduler = scheduler
    scheduler.add_job(
        background_cycle, 'interval', hours=config.SCHEDULER_INTERVAL_HOURS,
        id=JOB_ID, max_instances=1, coalesce=True, replace_existing=True,
        next_run_time=datetime.now() + timedelta(seconds=config.SCHEDULER_FIRST_RUN_DELAY_S),
        # APScheduler's default 1s grace would skip an immediate run that starts late.
        misfire_grace_time=300,
    )
    scheduler.start()
    delay = config.SCHEDULER_FIRST_RUN_DELAY_S
    print(f"⏱️  Background scheduler started: every {config.SCHEDULER_INTERVAL_HOURS:g}h, "
          f"first cycle {'now' if delay <= 0 else f'in {delay}s'}.")
    return scheduler


def next_run_time(scheduler):
    """Returns the next scheduled cycle as a datetime, or None."""
    try:
        job = scheduler.get_job(JOB_ID)
        return job.next_run_time if job else None
    except Exception:                             # noqa: BLE001
        return None


if __name__ == "__main__":
    # Manual one-off cycle: `python scheduler.py`.
    background_cycle()
