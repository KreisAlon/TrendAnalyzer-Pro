import sqlite3
import json
import os
from datetime import datetime

from config import TARGET_POSTS_PER_PLATFORM

# Resolved from this file's location, not the process CWD. collectors/base.py derives
# its DB_PATH the same way, and a CWD-relative default silently split the two apart:
# save_posts()/prune_database() wrote one file while recalculate_platform_stats()
# updated another whenever the engine was launched from outside the project root.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "trends_project.db")

class TrendManager:
    """
    Manages the SQLite database for trends.
    Uses dynamic entities (keywords) for semantic linking instead of heavy embeddings.
    """

    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        print("⚡ Initializing Lightweight Database Manager...")
        self._init_db()

    def _init_db(self):
        """Initializes the schema with support for dynamic keywords (entities)."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS unified_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_platform TEXT,
                    external_id TEXT,
                    title TEXT,
                    content TEXT,
                    author TEXT,
                    url TEXT,
                    raw_score REAL,
                    trend_score REAL,
                    published_at TEXT,
                    collected_at TEXT,
                    keywords TEXT,      -- Stores extracted dynamic entities as JSON
                    UNIQUE(source_platform, external_id)
                )
            ''')
            # How much matching content each platform had in the latest scan, before
            # the per-platform storage cap. One row per platform, replaced per scan:
            # unified_posts holds at most TARGET_POSTS_PER_PLATFORM rows each, so the
            # true volume ratio can only come from here.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS scan_volume (
                    source_platform TEXT PRIMARY KEY,
                    topic TEXT,
                    searched_total INTEGER,
                    overlap REAL,
                    acceptance_rate REAL,
                    estimated_matches INTEGER,
                    stored INTEGER,
                    capped INTEGER,
                    source TEXT,
                    measured_at TEXT
                )
            ''')
            conn.commit()

    def save_posts(self, posts):
        """Processes a list of posts and saves them to the DB using only keywords."""
        if not posts:
            return 0

        with sqlite3.connect(self.db_path) as conn:
            added_count = self._insert_posts(conn, posts)
            conn.commit()
        return added_count

    @staticmethod
    def _insert_posts(conn, posts):
        """Inserts posts on an open connection without committing. Returns the count."""
        added_count = 0
        cursor = conn.cursor()
        collected_at = datetime.now().isoformat()

        for post in posts:
            try:
                # Convert the keywords (entities) list to a JSON string for storage
                keywords_json = json.dumps(post.get('keywords', []))

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
                    post.get('trend_score', 0),
                    post['published_at'],
                    collected_at,
                    keywords_json
                ))
                if cursor.rowcount > 0:
                    added_count += 1
            except Exception as e:
                print(f"Error saving post {post.get('external_id')}: {e}")

        return added_count

    def replace_posts(self, posts):
        """
        Swaps the whole corpus for `posts` in one transaction. Returns the count saved.

        Used when the user scans a NEW topic: the dashboard shows one topic at a time,
        and the per-platform Top-K pruner would otherwise keep the old topic's
        high-scoring posts and mix them into the new results. One transaction, so a
        failure leaves the previous corpus intact rather than an empty database.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('DELETE FROM unified_posts')
            added = self._insert_posts(conn, posts)
            conn.commit()
        return added

    def save_volume(self, topic, reports):
        """Replaces the volume table with this scan's per-platform reports."""
        measured_at = datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('DELETE FROM scan_volume')
            conn.executemany('''
                INSERT INTO scan_volume (source_platform, topic, searched_total, overlap,
                    acceptance_rate, estimated_matches, stored, capped, source, measured_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', [(r['platform'], topic, r['searched_total'], r['overlap'],
                   r['acceptance_rate'], r['estimated_matches'], r['stored'],
                   int(r['capped']), r['source'], measured_at) for r in reports])
            conn.commit()

    def get_volume(self):
        """The latest scan's volume rows as dicts, keyed by platform."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('SELECT * FROM scan_volume').fetchall()
        return {r['source_platform']: dict(r) for r in rows}

    def get_all_posts(self):
        """Retrieves all posts sorted by their calculated trend intensity."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM unified_posts ORDER BY trend_score DESC')
            return [dict(row) for row in cursor.fetchall()]

    def get_db_stats(self):
        """Queries the database to provide a quick summary."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT source_platform, COUNT(*), AVG(trend_score) 
                FROM unified_posts 
                GROUP BY source_platform
            ''')
            stats = cursor.fetchall()
            print("\n📊 --- Database Health Summary ---")
            if not stats:
                print("No data found in the database yet.")
            for platform, count, avg_score in stats:
                print(f"📍 {platform}: {count} posts | Avg Trend Score: {avg_score:.2f}")
            print("----------------------------------\n")


    def prune_database(self, max_per_platform=None):
        """
        Maintains a balanced, high-quality database by keeping only the top N posts
        PER PLATFORM. This prevents high-engagement platforms (like Dev.to) from 
        drowning out technical signals from niche platforms (like Mastodon).
        Uses SQLite Window Functions (ROW_NUMBER) for stratified pruning.

        The retained count defaults to TARGET_POSTS_PER_PLATFORM so the collectors and
        the pruner share one number: the collectors aim for exactly what survives here.
        """
        max_per_platform = max_per_platform or TARGET_POSTS_PER_PLATFORM
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # The inner query assigns a row number (rank) to each post within its specific platform,
            # ordered by the trend score. The outer query deletes anything ranked > max_per_platform.
            cursor.execute(f'''
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
                    WHERE rank <= {max_per_platform}
                )
            ''')
            deleted_count = cursor.rowcount
            conn.commit()
            
            if deleted_count > 0:
                print(f"🧹 Stratified Pruning: Removed {deleted_count} low-scoring posts. Maintained top {max_per_platform} per platform.")
