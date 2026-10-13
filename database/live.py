"""
Live-trend persistence for the background worker, mixed into `TrendManager`.
`live_trends` and `live_posts` together feed the homepage Intelligence Brief.
"""

import json
import sqlite3
from datetime import datetime

from database.schema import parse_keywords


class LiveTrendsMixin:
    """Background-worker reads and writes; relies on `TrendManager._connect()`."""

    def top_posts(self, per_platform=3):
        """
        Returns each platform's top `per_platform` posts as dicts
        {source_platform, title, url, trend_score}, best first.
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('''
                SELECT source_platform, title, url, trend_score FROM (
                    SELECT *, ROW_NUMBER() OVER (PARTITION BY source_platform
                                                 ORDER BY trend_score DESC) AS rn
                    FROM unified_posts
                ) WHERE rn <= ? ORDER BY trend_score DESC
            ''', (int(per_platform),)).fetchall()
        return [dict(r) for r in rows]

    def save_live_trend(self, topic, scores, keywords=None, top_posts=(), sentiment=None,
                        matches=None):
        """
        Records one background scan of `topic` in a single transaction.

        Args:
            topic (str): The seed topic scanned.
            scores (dict): Output of platform_scores() for that scan.
            keywords (dict | None): platform -> list of the scan's top KeyBERT phrases.
            top_posts (iterable[dict]): Output of top_posts() for that scan.
            sentiment (dict | None): platform -> (mean compound, positive, negative).
            matches (dict | None): platform -> platform-reported matching posts.
        """
        scanned_at = datetime.now().isoformat()
        keywords, sentiment, matches = keywords or {}, sentiment or {}, matches or {}
        with self._connect() as conn:
            conn.executemany('''
                INSERT INTO live_trends (topic, source_platform, posts, peak_score,
                    avg_score, top_title, top_url, keywords, matches, sentiment,
                    positive, negative, scanned_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', [(topic, platform, s['posts'], s['peak_score'], s['avg_score'],
                   s['top_title'], s['top_url'], json.dumps(keywords.get(platform, [])),
                   matches.get(platform), *sentiment.get(platform, (0.0, 0, 0)),
                   scanned_at)
                  for platform, s in scores.items()])
            conn.executemany('''
                INSERT INTO live_posts (topic, source_platform, title, url, trend_score,
                    scanned_at) VALUES (?, ?, ?, ?, ?, ?)
            ''', [(topic, p['source_platform'], p['title'], p['url'], p['trend_score'],
                   scanned_at) for p in top_posts])
            conn.commit()

    def get_top_live_posts(self, limit=None):
        """
        Returns the posts of each topic's latest background scan, best first.

        Args:
            limit (int | None): Maximum rows; None returns the full ranking.

        Returns:
            list[dict]: {topic, source_platform, title, url, trend_score}.
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('''
                SELECT lp.topic, lp.source_platform, lp.title, lp.url, lp.trend_score
                FROM live_posts lp
                JOIN (SELECT topic, MAX(scanned_at) AS latest
                      FROM live_posts GROUP BY topic) last
                  ON lp.topic = last.topic AND lp.scanned_at = last.latest
                ORDER BY lp.trend_score DESC LIMIT ?
            ''', (-1 if limit is None else int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def live_topics(self):
        """Returns the set of distinct topics recorded in live_trends."""
        with self._connect() as conn:
            return {r[0] for r in conn.execute('SELECT DISTINCT topic FROM live_trends')}

    def count_live_topics(self):
        """Returns how many distinct topics live_trends holds."""
        with self._connect() as conn:
            return conn.execute('SELECT COUNT(DISTINCT topic) FROM live_trends').fetchone()[0]

    def get_live_trends(self, limit=6):
        """
        Returns the latest background scan of each topic, best first.

        Ranked by platforms reached, then posts, then platform-reported matches
        (tie-break for capped samples), then recency. Engagement scores are not
        used: they are relative to each topic's own sample.

        Returns:
            list[dict]: {topic, scanned_at, platforms, posts, matches, sentiment,
            positive, negative, per_platform}, where per_platform maps platform ->
            {posts, matches, avg_score, peak_score, top_title, top_url, keywords,
            sentiment, positive, negative}.
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('''
                SELECT lt.* FROM live_trends lt
                JOIN (SELECT topic, MAX(scanned_at) AS latest
                      FROM live_trends GROUP BY topic) last
                  ON lt.topic = last.topic AND lt.scanned_at = last.latest
            ''').fetchall()
        topics = {}
        for row in rows:
            entry = topics.setdefault(row['topic'], {'topic': row['topic'],
                                                     'scanned_at': row['scanned_at'],
                                                     'per_platform': {}})
            entry['per_platform'][row['source_platform']] = {
                'posts': row['posts'] or 0, 'matches': row['matches'] or 0,
                'avg_score': row['avg_score'] or 0.0,
                'peak_score': row['peak_score'] or 0.0,
                'top_title': row['top_title'], 'top_url': row['top_url'],
                'keywords': parse_keywords(row['keywords']),
                'sentiment': row['sentiment'] or 0.0, 'positive': row['positive'] or 0,
                'negative': row['negative'] or 0}
        for entry in topics.values():
            platforms = entry['per_platform'].values()
            entry['platforms'] = len(entry['per_platform'])
            entry['posts'] = sum(p['posts'] for p in platforms)
            entry['matches'] = sum(p['matches'] for p in platforms)
            # Post-weighted mean polarity across the topic's communities.
            entry['sentiment'] = (sum(p['sentiment'] * p['posts'] for p in platforms)
                                  / entry['posts'] if entry['posts'] else 0.0)
            entry['positive'] = sum(p['positive'] for p in platforms)
            entry['negative'] = sum(p['negative'] for p in platforms)
        ranked = sorted(topics.values(), reverse=True,
                        key=lambda e: (e['platforms'], e['posts'], e['matches'],
                                       e['scanned_at']))
        return ranked[:limit]
