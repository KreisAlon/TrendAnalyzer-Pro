"""SQLite schema: table creation, forward migrations and the `keywords` codec."""

import json

# scan_volume columns added after the original schema (migrated by init_schema).
_FUNNEL_COLUMNS = {
    'unique_candidates': 'INTEGER',
    'gated': 'INTEGER',
    'accepted': 'INTEGER',
    'reject_reasons': 'TEXT',        # JSON object: rejection code -> count
}


def parse_keywords(raw):
    """
    Decodes a stored `keywords` JSON value into a list of strings; corrupt, NULL
    or non-list values yield [].
    """
    try:
        keywords = json.loads(raw or '[]')
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(k) for k in keywords] if isinstance(keywords, list) else []


def init_schema(conn):
    """
    Creates missing tables and adds later columns via `ALTER TABLE ... ADD COLUMN`
    (old rows read back NULL). Commits on `conn` before returning.
    """
    # WAL: readers never block on a writer; the mode persists in the file.
    conn.execute('PRAGMA journal_mode=WAL')
    cursor = conn.cursor()
    # UNIQUE(source_platform, external_id) makes INSERT OR IGNORE idempotent.
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
    # unified_posts is capped per platform; true volume ratios come from here.
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
            measured_at TEXT,
            unique_candidates INTEGER,
            gated INTEGER,
            accepted INTEGER,
            reject_reasons TEXT
        )
    ''')
    # Background results, kept apart so they never replace the user's corpus.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS live_trends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT,
            source_platform TEXT,
            posts INTEGER,
            peak_score REAL,
            avg_score REAL,
            matches INTEGER,
            sentiment REAL,
            positive INTEGER,
            negative INTEGER,
            top_title TEXT,
            top_url TEXT,
            scanned_at TEXT
        )
    ''')
    # Column names are constants, never input, so formatting them into DDL is safe.
    existing = {row[1] for row in cursor.execute('PRAGMA table_info(scan_volume)')}
    for column, sql_type in _FUNNEL_COLUMNS.items():
        if column not in existing:
            cursor.execute(f'ALTER TABLE scan_volume ADD COLUMN {column} {sql_type}')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS live_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT,
            source_platform TEXT,
            title TEXT,
            url TEXT,
            trend_score REAL,
            scanned_at TEXT
        )
    ''')
    # live_trends gained these columns after its first release.
    live_columns = {row[1] for row in cursor.execute('PRAGMA table_info(live_trends)')}
    for column, sql_type in (('keywords', 'TEXT'), ('matches', 'INTEGER'),
                             ('sentiment', 'REAL'), ('positive', 'INTEGER'),
                             ('negative', 'INTEGER')):
        if column not in live_columns:
            cursor.execute(f'ALTER TABLE live_trends ADD COLUMN {column} {sql_type}')
    conn.commit()
