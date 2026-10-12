"""
Persistence layer: the SQLite store behind the scan engine and the dashboard.

Tables:
* `unified_posts` -- one row per accepted post (keywords as a JSON list; embeddings
  are recomputed on demand, not stored).
* `scan_volume`   -- per-platform volume and quality-gate funnel of the latest scan.
* `live_trends`   -- per-platform summary of each background scan.
* `live_posts`    -- retained posts of each background scan.

Every public method opens its own short-lived connection, so one TrendManager is
safe to share across threads; WAL mode means readers never wait for a writer.
Schema lives in schema.py; live_trends/live_posts methods come from live.py.
"""

import contextlib
import json
import sqlite3
from datetime import datetime, timedelta

from config import DB_PATH, TARGET_POSTS_PER_PLATFORM, DATA_RETENTION_DAYS
from database.live import LiveTrendsMixin
from database.schema import init_schema

# Seconds a connection waits for another writer's lock before giving up.
SQLITE_BUSY_TIMEOUT_S = 30


class TrendManager(LiveTrendsMixin):
    """
    Data-access object for the trend corpus and per-scan volume statistics.

    Args:
        db_path (str): SQLite file; defaults to config.DB_PATH (injectable for tests).
    """

    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        self._init_db()  # Idempotent

    @contextlib.contextmanager
    def _connect(self):
        """
        Yields a short-lived connection: committed on success, rolled back on error,
        and always closed (`with sqlite3.connect()` alone never closes, leaking file
        handles and WAL readers). The busy timeout makes a writer wait for a
        concurrent transaction instead of failing with "database is locked".
        """
        conn = sqlite3.connect(self.db_path, timeout=SQLITE_BUSY_TIMEOUT_S)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        """Creates and migrates the schema (see database.schema.init_schema)."""
        with self._connect() as conn:
            init_schema(conn)

    def save_posts(self, posts):
        """
        Adds posts to the existing corpus (re-scan of the same topic).

        Returns:
            int: Rows actually inserted; posts already stored are ignored.
        """
        if not posts:
            return 0
        with self._connect() as conn:
            added_count = self._insert_posts(conn, posts)
            conn.commit()
        return added_count

    @staticmethod
    def _insert_posts(conn, posts):
        """
        Inserts posts on an open connection without committing, so replace_posts()
        can make delete + insert one transaction. Returns the rows inserted.
        """
        added_count = 0
        cursor = conn.cursor()
        collected_at = datetime.now().isoformat()

        for post in posts:
            try:
                # Parameterised statement: scraped content is bound, never interpolated.
                cursor.execute('''
                    INSERT OR IGNORE INTO unified_posts (
                        source_platform, external_id, title, content,
                        author, url, raw_score, trend_score,
                        published_at, collected_at, keywords
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    post['source_platform'],
                    post['external_id'],
                    post['title'],
                    post['content'],
                    post['author'],
                    post['url'],
                    post['raw_score'],
                    post.get('trend_score', 0),  # Normalised after storage
                    post['published_at'],
                    collected_at,
                    json.dumps(post.get('keywords', [])),
                ))
                # rowcount is 0 when OR IGNORE skipped a duplicate.
                added_count += cursor.rowcount > 0
            except sqlite3.OperationalError:
                # Lock/IO errors abort the batch (rolled back) instead of being
                # swallowed once per post.
                raise
            except Exception as e:
                # Fault Isolation: one malformed record cannot abort the batch.
                print(f"Error saving post {post.get('external_id')}: {e}")

        return added_count

    def replace_posts(self, posts):
        """
        Replaces the whole corpus with `posts` in one transaction (new topic), so a
        failure leaves the previous corpus intact. Returns the rows inserted.
        """
        with self._connect() as conn:
            conn.execute('DELETE FROM unified_posts')
            added = self._insert_posts(conn, posts)
            conn.commit()
        return added

    def save_volume(self, topic, reports):
        """
        Replaces the volume table with the current scan's per-platform reports.

        Args:
            topic (str): Topic the scan was run for.
            reports (list[dict]): One BaseCollector.volume_report() dict per platform.
        """
        measured_at = datetime.now().isoformat()
        with self._connect() as conn:
            # The table always describes exactly one scan.
            conn.execute('DELETE FROM scan_volume')
            conn.executemany('''
                INSERT INTO scan_volume (source_platform, topic, searched_total, overlap,
                    acceptance_rate, estimated_matches, stored, capped, source, measured_at,
                    unique_candidates, gated, accepted, reject_reasons)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', [(r['platform'], topic, r['searched_total'], r['overlap'],
                   r['acceptance_rate'], r['estimated_matches'], r['stored'],
                   int(r['capped']), r['source'], measured_at,
                   # Defaults let a partial report still store.
                   r.get('unique_candidates', 0), r.get('gated', 0), r.get('accepted', 0),
                   json.dumps(r.get('reject_reasons') or {})) for r in reports])
            conn.commit()

    def get_volume(self):
        """
        Reads the latest scan's volume rows.

        Returns:
            dict[str, dict]: One row per platform, keyed by platform name, with
            `reject_reasons` decoded to a dict ({} when absent or corrupt).
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('SELECT * FROM scan_volume').fetchall()
        volume = {}
        for row in rows:
            entry = dict(row)
            try:
                # NULL (pre-migration rows) is treated as an empty tally.
                reasons = json.loads(entry.get('reject_reasons') or '{}')
            except (json.JSONDecodeError, TypeError):
                reasons = {}
            entry['reject_reasons'] = reasons if isinstance(reasons, dict) else {}
            volume[entry['source_platform']] = entry
        return volume

    def prune_database(self, max_per_platform=None):
        """
        Keeps only the top-N posts per platform by trend score (stratified pruning),
        so a high-engagement platform cannot displace the others.

        Args:
            max_per_platform (int): Defaults to TARGET_POSTS_PER_PLATFORM.
        """
        max_per_platform = max_per_platform or TARGET_POSTS_PER_PLATFORM
        with self._connect() as conn:
            cursor = conn.execute('''
                DELETE FROM unified_posts
                WHERE id NOT IN (
                    SELECT id FROM (
                        SELECT id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY source_platform
                                   ORDER BY trend_score DESC
                               ) as rank
                        FROM unified_posts
                    )
                    WHERE rank <= ?
                )
            ''', (int(max_per_platform),))
            deleted_count = cursor.rowcount
            conn.commit()

        if deleted_count > 0:
            print(f"🧹 Stratified Pruning: Removed {deleted_count} low-scoring posts. "
                  f"Maintained top {max_per_platform} per platform.")

    # --- Per-platform scores ------------------------------------------------

    def platform_scores(self):
        """
        Summarises the stored corpus per platform, after normalisation.

        Returns:
            dict[str, dict]: platform -> {posts, peak_score, avg_score, top_title,
            top_url}, where top_* describe the highest-scoring post.
        """
        with self._connect() as conn:
            rows = conn.execute('''
                SELECT source_platform, COUNT(*), MAX(trend_score), AVG(trend_score)
                FROM unified_posts GROUP BY source_platform
            ''').fetchall()
            scores = {}
            for platform, count, peak, avg in rows:
                top = conn.execute('''
                    SELECT title, url FROM unified_posts
                    WHERE source_platform = ? ORDER BY trend_score DESC LIMIT 1
                ''', (platform,)).fetchone() or ('', '')
                scores[platform] = {'posts': count, 'peak_score': peak or 0.0,
                                    'avg_score': avg or 0.0,
                                    'top_title': top[0], 'top_url': top[1]}
        return scores

    # --- Data eviction policy (TTL) -----------------------------------------

    def cleanup_old_records(self, days=None):
        """
        Deletes posts and live-trend rows collected more than `days` days ago.

        Args:
            days (int | None): Retention window; defaults to DATA_RETENTION_DAYS.

        Returns:
            int: Total rows deleted.
        """
        days = days or DATA_RETENTION_DAYS
        # Same naive-local ISO format as stored stamps, so string order is time order.
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            posts = conn.execute('DELETE FROM unified_posts WHERE collected_at < ?',
                                 (cutoff,)).rowcount
            trends = conn.execute('DELETE FROM live_trends WHERE scanned_at < ?',
                                  (cutoff,)).rowcount
            trends += conn.execute('DELETE FROM live_posts WHERE scanned_at < ?',
                                   (cutoff,)).rowcount
            conn.commit()
        if posts or trends:
            print(f"🧹 TTL cleanup: removed {posts} posts and {trends} live-trend rows "
                  f"older than {days} days.")
        return posts + trends
