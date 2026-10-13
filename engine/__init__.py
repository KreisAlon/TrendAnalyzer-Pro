"""
Scan orchestration engine for TrendAnalyzer Pro.

`scan(topic)` runs one collection cycle:
    1. Validation  -- normalise and check the topic (validation.py).
    2. Planning    -- derive per-platform queries and relevance keywords
                      (planning.py, topic_profile.py).
    3. Collection  -- all collectors run concurrently via `asyncio.gather` (cycle.py).
    4. Persistence -- store, normalise, prune and record volume stats (scoring.py).

`scan_background(topic)` runs the same cycle for the autonomous worker against a
private scratch database (orchestrator.py). Collectors (and KeyBERT) are imported
lazily inside `run_cycle`.
"""

from engine.models import (ScanCancelled, ScanResult, NO_RESULTS_MESSAGE,
                           LOW_VOLUME_MESSAGE, INSUFFICIENT_PLATFORMS_MESSAGE,
                           CANCELLED_MESSAGE)
from engine.validation import (MAX_TOPIC_CHARS, ENGLISH_ONLY_MESSAGE, normalise_topic,
                               validate_topic)
from engine.planning import DynamicTopicManager, current_topic
from engine.signals import clean_buzzword
from engine.cycle import MIN_PLATFORMS, TICK_S, noise_floors, run_cycle
from engine.orchestrator import is_scanning, background_topic, scan, scan_background

__all__ = [
    'ScanCancelled', 'ScanResult', 'NO_RESULTS_MESSAGE', 'LOW_VOLUME_MESSAGE',
    'INSUFFICIENT_PLATFORMS_MESSAGE', 'CANCELLED_MESSAGE',
    'MAX_TOPIC_CHARS', 'ENGLISH_ONLY_MESSAGE', 'normalise_topic', 'validate_topic',
    'DynamicTopicManager', 'current_topic', 'clean_buzzword',
    'MIN_PLATFORMS', 'TICK_S', 'noise_floors', 'run_cycle',
    'is_scanning', 'background_topic', 'scan', 'scan_background',
]
