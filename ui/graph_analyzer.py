"""
Semantic similarity graph of the stored corpus.

Each node is a post; an edge joins two posts whose sentence embeddings exceed an
adaptive cosine threshold. The graph is then searched for emerging bridges (see
graph_bridges.py).

Pipeline: sample top-N posts per platform -> embed (unit-norm MiniLM) -> link with
stratified percentile thresholds -> label communities (greedy modularity) -> rank
cross-platform weak ties.

No Streamlit import: caching is the caller's job. Embeddings are never persisted.
Stored keywords explain links but do not create them: KeyBERT's MMR makes phrases
document-specific, so a shared-keyword graph would be almost edgeless.
"""

import itertools
import os
import sqlite3
import threading
from contextlib import closing

import networkx as nx
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

from config import DB_PATH, EMBED_MODEL_NAME, EMBED_DEVICE, PLATFORMS
from database.manager import SQLITE_BUSY_TIMEOUT_S
from database.schema import parse_keywords
from graph_bridges import detect_emerging_bridges

# The encoder truncates long input anyway; the opening states the subject.
EMBED_CHAR_BUDGET = 600

# Body prefix kept on each node for the shared-vocabulary extractor.
NODE_SUMMARY_CHARS = 400

# Categorical platform hues (CVD-validated), shared by every view.
PLATFORM_COLORS = {
    "GitHub": "#2a78d6",
    "Hacker News": "#eb6834",
    "Dev.to": "#1baf7a",
    "Mastodon": "#4a3aa7",
}

# Ink tokens for chart text; text never wears a series colour.
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"


# Process-wide lazy singleton. The encoder is not safe for concurrent calls, so its
# load and every encode are serialised across sessions.
_ENCODER = None
_ENCODER_LOCK = threading.Lock()


def get_encoder():
    """Shared sentence encoder, loaded on first use so importing this module stays cheap."""
    global _ENCODER
    with _ENCODER_LOCK:
        if _ENCODER is None:
            from sentence_transformers import SentenceTransformer
            # Pinned to EMBED_DEVICE (CPU): MPS is not thread-safe.
            _ENCODER = SentenceTransformer(EMBED_MODEL_NAME, device=EMBED_DEVICE)
    return _ENCODER


class GraphBuilder:
    """
    Builds the cross-platform semantic similarity graph. `build()` returns
    `(graph, metrics)`: edges carry `weight` (cosine) and `is_cross`; metrics hold
    the edge thresholds and the ranked emerging bridges.
    """

    def __init__(self, percentile=97.0, min_similarity=0.35,
                 top_n_per_platform=25, db_path=None):
        """
        Args:
            percentile (float): Edge cutoff as a percentile of observed similarities,
                so density adapts to the corpus (97: strongest cluster structure,
                modularity ~0.5, found empirically).
            min_similarity (float): Absolute floor, so a small or homogeneous corpus
                cannot link unrelated posts.
            top_n_per_platform (int): Balanced sampling cap per platform.
            db_path (str): SQLite file; defaults to the project database.
        """
        self.percentile = percentile
        self.min_similarity = min_similarity
        self.top_n_per_platform = top_n_per_platform
        self.db_path = db_path or DB_PATH
        self.graph = nx.Graph()
        self.metrics = {}
        self.thresholds = {}

    def load_posts(self):
        """Top N posts per platform by trend score; [] when the database or table is missing."""
        if not os.path.exists(self.db_path):
            return []
        rows = []
        try:
            # closing(): sqlite3's own context manager never closes the connection.
            with closing(sqlite3.connect(self.db_path, timeout=SQLITE_BUSY_TIMEOUT_S)) as conn:
                conn.row_factory = sqlite3.Row
                # One bounded query per platform guarantees balance.
                for platform in PLATFORMS:
                    cursor = conn.execute(
                        """
                        SELECT id, title, content, source_platform, url, keywords
                        FROM unified_posts
                        WHERE source_platform = ?
                        ORDER BY trend_score DESC
                        LIMIT ?
                        """,
                        (platform, self.top_n_per_platform),
                    )
                    rows.extend(dict(r) for r in cursor.fetchall())
        except sqlite3.Error:
            # The table does not exist until the first scan has stored posts.
            return []
        return rows

    @staticmethod
    def embed(posts):
        """Encodes title + body opening of each post as unit-norm 384-d embeddings."""
        texts = [f"{p['title']}. {(p.get('content') or '')[:EMBED_CHAR_BUDGET]}"
                 for p in posts]
        encoder = get_encoder()
        with _ENCODER_LOCK:
            return encoder.encode(
                texts, batch_size=32, show_progress_bar=False, normalize_embeddings=True
            )

    def _stratified_thresholds(self, sim, platforms):
        """
        Separate cutoffs for same- and cross-platform pairs: same-platform posts are
        systematically more similar, so one global cutoff would admit almost only
        within-platform edges.

            cutoff(S) = max(P_p({sim(i, j) : (i, j) in S}), min_similarity)

        Returns:
            dict: {'cross': float, 'same': float}.
        """
        cross_vals, same_vals = [], []
        for i, j in itertools.combinations(range(len(platforms)), 2):
            (cross_vals if platforms[i] != platforms[j] else same_vals).append(sim[i][j])

        def cutoff(values):
            # An empty stratum admits nothing.
            if not values:
                return 1.0
            return max(float(np.percentile(values, self.percentile)), self.min_similarity)

        return {'cross': cutoff(cross_vals), 'same': cutoff(same_vals)}

    def _compute_metrics(self, graph):
        """
        Labels communities by weighted greedy modularity (Clauset-Newman-Moore), which
        feeds the ranking's cross-community bonus, then ranks the emerging bridges.
        """
        metrics = {'thresholds': self.thresholds, 'emerging_bridges': []}
        if graph.number_of_edges() == 0:
            return metrics

        try:
            communities = nx.community.greedy_modularity_communities(graph, weight='weight')
            for idx, members in enumerate(communities):
                for nid in members:
                    graph.nodes[nid]['community'] = idx
        except (ZeroDivisionError, nx.NetworkXError, StopIteration):
            # Degenerate graphs are ranked without the community bonus.
            pass

        metrics['emerging_bridges'] = detect_emerging_bridges(graph)
        return metrics

    def build(self):
        """Builds the graph and its metrics from the current database contents."""
        self.graph = nx.Graph()
        posts = self.load_posts()
        if len(posts) < 2:
            self.metrics = self._compute_metrics(self.graph)
            return self.graph, self.metrics

        # The encoder may return a torch tensor.
        embeddings = self.embed(posts)
        embeddings = embeddings.cpu().numpy() if hasattr(embeddings, 'cpu') else np.array(embeddings)
        sim = cosine_similarity(embeddings)
        np.fill_diagonal(sim, 0.0)

        platforms = [p['source_platform'] for p in posts]
        self.thresholds = self._stratified_thresholds(sim, platforms)

        for post in posts:
            self.graph.add_node(
                post['id'],
                platform=post['source_platform'],
                title=post['title'],
                url=post.get('url') or '',
                # Corrupt keyword JSON degrades to [] rather than dropping the node.
                keywords=parse_keywords(post.get('keywords')),
                summary=(post.get('content') or '')[:NODE_SUMMARY_CHARS],
            )

        for i, j in itertools.combinations(range(len(posts)), 2):
            score = float(sim[i][j])
            is_cross = platforms[i] != platforms[j]
            if score >= self.thresholds['cross' if is_cross else 'same']:
                self.graph.add_edge(posts[i]['id'], posts[j]['id'],
                                    weight=score, is_cross=is_cross)

        self.metrics = self._compute_metrics(self.graph)
        return self.graph, self.metrics
