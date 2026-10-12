"""
Synchronous scan entry points and their concurrency control: `scan()` for the
dashboard, `scan_background()` for the autonomous worker (scratch DB, so the
user's corpus is never touched).
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import config
from engine.cycle import run_cycle
from engine.models import ScanCancelled, ScanResult, CANCELLED_MESSAGE
from engine.validation import validate_topic

# One interactive scan per process: sessions share one corpus, and interleaved
# scans would replace and re-score each other's rows.
_SCAN_LOCK = threading.Lock()

# Background scans never touch the corpus, so they get their own lock and may run
# alongside an interactive scan.
_BACKGROUND_LOCK = threading.Lock()

# Topic the background worker is scanning, or None (shown in the UI).
_background_topic = None


def is_scanning():
    """True while an interactive scan is running in any session."""
    return _SCAN_LOCK.locked()


def background_topic():
    """Returns the seed topic the background worker is scanning, or None."""
    return _background_topic


def _run_coroutine(coro):
    """
    Runs a coroutine to completion from synchronous code: `asyncio.run` on this
    thread normally (so progress callbacks fire on the caller's thread), or on a
    worker thread's private loop if this thread already runs one.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No loop on this thread (the normal Streamlit/APScheduler case).
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def scan(topic, progress=None, max_posts=None, cancel=None):
    """
    Dashboard entry point: plan, collect, score and store. Ordinary failures are
    returned as a ScanResult status/error, never raised.

    Args:
        topic (str): Raw topic text from the search box.
        progress (callable | None): Optional stage callback (see `run_cycle`).
        max_posts (int | None): Per-platform depth; defaults to config.MANUAL_MAX_POSTS.
        cancel (threading.Event | None): Set to abort the scan (status "cancelled").

    Returns:
        ScanResult: The outcome, including invalid/busy/cancelled/failed states.
    """
    topic, invalid = validate_topic(topic)
    if invalid:
        return ScanResult(topic="", status="invalid", error=invalid)

    # Non-blocking: a second scan is refused, not queued.
    if not _SCAN_LOCK.acquire(blocking=False):
        return ScanResult(topic=topic, status="busy",
                          error="Another scan is already running. "
                                "Give it a minute and try again.")
    try:
        return _run_coroutine(run_cycle(topic, progress,
                                        max_posts=max_posts or config.MANUAL_MAX_POSTS,
                                        cancel=cancel))
    except ScanCancelled:
        print(f">>> 🛑 Scan of {topic!r} cancelled; nothing was saved.")
        return ScanResult(topic=topic, status="cancelled", error=CANCELLED_MESSAGE)
    except Exception as exc:                      # noqa: BLE001
        print(f"❌ Critical error during scan: {exc}")
        return ScanResult(topic=topic, status="failed",
                          error=f"The scan stopped unexpectedly: {exc}")
    finally:
        _SCAN_LOCK.release()


def scan_background(topic, wait_s=0, max_posts=None):
    """
    Autonomous-worker entry point: one isolated cycle for a seed topic.
    Serialised only against other background scans.

    Args:
        topic (str): Seed topic (validated here).
        wait_s (float): Seconds to wait for another background scan to finish.
        max_posts (int | None): Per-platform depth; defaults to
            config.BACKGROUND_MAX_POSTS.

    Returns:
        ScanResult: The outcome, including invalid/busy/failed states.
    """
    global _background_topic
    topic, invalid = validate_topic(topic)
    if invalid:
        return ScanResult(topic="", status="invalid", error=invalid)
    acquired = (_BACKGROUND_LOCK.acquire(timeout=wait_s) if wait_s > 0
                else _BACKGROUND_LOCK.acquire(blocking=False))
    if not acquired:
        return ScanResult(topic=topic, status="busy",
                          error="Another background scan is still running.")
    _background_topic = topic
    try:
        return _run_coroutine(run_cycle(topic, background=True,
                                        max_posts=max_posts or config.BACKGROUND_MAX_POSTS))
    except Exception as exc:                      # noqa: BLE001
        print(f"❌ Background scan of {topic!r} failed: {exc}")
        return ScanResult(topic=topic, status="failed", error=str(exc))
    finally:
        _background_topic = None
        _BACKGROUND_LOCK.release()
