"""
Candidate-funnel bookkeeping and score normalisation, mixed into `BaseCollector`.
"""

import asyncio
import math
import sqlite3
from contextlib import closing

import numpy as np

from config import DB_PATH, TARGET_POSTS_PER_PLATFORM, COLLECTION_BATCH_SIZE

# Logistic shift in recalculate_platform_stats(): places a platform's mean post at
# ~62/100 rather than exactly 50.
SIGMOID_SHIFT = 0.5


class FunnelMixin:
    """
    Candidate pooling, wave-based collection, the volume report and per-platform
    trend-score normalisation. Uses funnel state set by `BaseCollector.__init__`.
    """

    def merge_candidates(self, batches, key, score_field):
        """
        Merges per-query results into one de-duplicated, engagement-ranked pool.

        The pool is capped at `max_candidates`, an oversample of the target, so gate
        rejections are absorbed by lower-ranked candidates.

        Args:
            batches (list[list[dict]]): One result list per query.
            key (str): Field identifying an item across queries ('id', 'objectID').
            score_field (str): Engagement field used for ranking.

        Returns:
            list[dict]: Unique candidates, best-first.
        """
        self.record_candidates(batches, key)
        unique = {}
        for batch in batches:
            for item in batch:
                unique.setdefault(item.get(key), item)
        return sorted(unique.values(), key=lambda i: i.get(score_field) or 0,
                      reverse=True)[:self.max_candidates]

    def log_yield(self, accepted, target, inspected, noun='posts'):
        """Console yield summary, with the rejection breakdown on a shortfall."""
        if accepted < target:
            print(f"   ⚠️  {self.platform_name}: {accepted}/{target} {noun} cleared the "
                  f"gate from {inspected} candidates. Rejections: {dict(self.reject_reasons)}")
        else:
            print(f"   ✅ {self.platform_name}: {accepted} posts from {inspected} candidates.")

    async def collect_until_target(self, candidates, process, target=None, batch_size=None):
        """
        Runs `process` over `candidates` in concurrent waves until `target` posts pass.

        Each wave is sized from the acceptance rate so far, and the walk stops at the
        target, so the oversampled pool only costs requests while the gate rejects.

        Args:
            candidates (list): Oversampled raw items, best-first.
            process (callable): async fn(item) -> post dict, or None if rejected.
            target (int): Defaults to this scan's `target_posts`.
            batch_size (int): First wave size; defaults to COLLECTION_BATCH_SIZE
                scaled to the target.

        Returns:
            list: Accepted posts, capped at `target`.
        """
        target = target or self.target_posts
        wave_size = batch_size or max(
            target, COLLECTION_BATCH_SIZE * target // TARGET_POSTS_PER_PLATFORM)
        accepted, cursor = [], 0

        while cursor < len(candidates) and len(accepted) < target:
            batch = candidates[cursor:cursor + max(1, wave_size)]
            cursor += len(batch)

            # Fault Isolation: one failing item must not cancel the rest of the wave.
            results = await asyncio.gather(
                *[process(item) for item in batch], return_exceptions=True
            )
            for result in results:
                if isinstance(result, BaseException):
                    self.reject_reasons['processing_error'] += 1
                elif result is not None:
                    accepted.append(result)

            if len(accepted) >= target:
                break

            # The 0.2 floor stops a zero-yield wave requesting the whole remaining pool.
            rate = max(len(accepted) / cursor, 0.2)
            wave_size = min(len(candidates) - cursor,
                            int((target - len(accepted)) / rate) + 2)

        # Recorded before the cap, so the acceptance rate describes the gate.
        self.gated += cursor
        self.gate_accepted += len(accepted)

        # The last wave may overshoot.
        accepted = accepted[:target]
        self.log_yield(len(accepted), target, cursor)
        return accepted

    def record_candidates(self, batches, key):
        """
        Records fetched vs. unique candidate rows.

        The ratio is volume_report()'s overlap correction: related queries return
        many of the same items, so their summed totals over-count.
        """
        rows = [item for batch in batches for item in batch if isinstance(item, dict)]
        self.fetched_rows += len(rows)
        self.unique_candidates += len({item.get(key) for item in rows})

    def volume_report(self, stored):
        """
        Summarises this collector's run: matching volume and the gate funnel.

        The estimate is the platform-wide count of posts that would pass the gate:

            estimate = platform-reported total
                       x overlap correction  (unique / fetched rows across queries)
                       x gate acceptance     (passed / inspected)

        Without a platform total the unique candidate count is used (a lower bound);
        the estimate is never below `stored`. The funnel balances
        (gated == accepted + rejections): candidates that failed before the gate
        could judge them are reported as the 'unreachable' residual.

        Args:
            stored (int): Posts this collector actually returned for storage.

        Returns:
            dict: One row of the `scan_volume` table.
        """
        if self.overlap_factor is not None:
            overlap = self.overlap_factor
        elif self.fetched_rows:
            overlap = self.unique_candidates / self.fetched_rows
        else:
            overlap = 1.0
        acceptance = self.gate_accepted / self.gated if self.gated else 0.0

        if self.search_totals:
            searched = int(sum(self.search_totals))
            estimate = searched * overlap * acceptance
            source = "platform"
        else:
            searched = self.unique_candidates
            estimate = searched * acceptance
            source = "candidates"

        reasons = dict(self.reject_reasons)
        unaccounted = self.gated - self.gate_accepted - sum(reasons.values())
        if unaccounted > 0:
            reasons['unreachable'] = reasons.get('unreachable', 0) + unaccounted

        # Built-in types only: the row is persisted to SQLite and JSON.
        return {
            'platform': self.platform_name,
            'searched_total': searched,
            'unique_candidates': int(self.unique_candidates),
            'gated': int(self.gated),
            'accepted': int(self.gate_accepted),
            'reject_reasons': reasons,
            'overlap': round(overlap, 4),
            'acceptance_rate': round(acceptance, 4),
            'estimated_matches': max(int(round(estimate)), int(stored)),
            'stored': int(stored),
            'capped': bool(self.search_capped),
            'source': source,
        }

    def recalculate_platform_stats(self, db_path=None):
        """
        Converts this platform's raw engagement into a comparable 0-100 trend score.

            trend_score = 100 * sigmoid( (log10(raw + 1) - mean) / std + SIGMOID_SHIFT )

        Per platform because raw units differ (stars vs. favourites); the log damps
        heavy tails and the logistic bounds the result.
        """
        # closing() releases the handle; the inner `with` commits or rolls back.
        with closing(sqlite3.connect(db_path or DB_PATH, timeout=30)) as conn, conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id, raw_score FROM unified_posts WHERE source_platform = ?',
                           (self.platform_name,))
            rows = cursor.fetchall()
            if not rows:
                return
            processed_scores = [math.log10((r[1] or 0) + 1) if (r[1] or 0) > 0 else 0.0
                                for r in rows]
            # Zero spread (all scores equal) would divide by zero.
            mean, std_dev = np.mean(processed_scores), np.std(processed_scores) or 1.0
            cursor.executemany(
                'UPDATE unified_posts SET trend_score = ? WHERE id = ?',
                [(100 / (1 + math.exp(-((score - mean) / std_dev + SIGMOID_SHIFT))), row[0])
                 for row, score in zip(rows, processed_scores)],
            )
