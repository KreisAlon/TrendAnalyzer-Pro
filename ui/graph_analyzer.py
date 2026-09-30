"""
Semantic graph construction over the collected trend corpus.

Framework-agnostic on purpose: nothing here imports Streamlit, so the graph can be
built and inspected from a plain Python shell. Caching is the caller's job.

Why embeddings are computed here rather than read from the database
-------------------------------------------------------------------
An earlier revision selected an `embedding` column that no longer exists -- it was
dropped when the pipeline moved to lightweight keyword JSON, which left this module
raising `OperationalError: no such column: embedding` on every call.

Restoring a stored-vector column would undo that decision. It is also unnecessary:
that change solved a *storage* problem, not a *compute* one. The MiniLM encoder is
already resident because KeyBERT loads it, and encoding the whole corpus costs about
0.65s for ~130 KB of transient memory. So vectors are derived on demand and never
persisted -- the database keeps its lean schema and the graph gets true semantics.

The stored keywords are deliberately NOT used as the linking substrate. Measured on
the current corpus, only 5 of 235 distinct keywords occur in more than one post,
because KeyBERT's MMR diversity yields document-specific phrases ("dsh web",
"nous research"). A shared-keyword graph is therefore almost edgeless. Keywords
remain valuable as node labels and tooltips.
"""

import itertools
import json
import os
import re
import sqlite3

import networkx as nx
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

# --- Path configuration -----------------------------------------------------
# Derived from this file's location, never the process CWD, so the module agrees
# with database/manager.py and collectors/base.py no matter where it is launched.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "trends_project.db")

# --- Model configuration ----------------------------------------------------
EMBED_MODEL_NAME = "all-MiniLM-L6-v2"
EMBED_CHAR_BUDGET = 600     # Characters of body text fed to the encoder per post

# Characters of body text kept ON each node. Two consumers need it and neither
# should re-query the database: the shared-vocabulary extractor below, and the
# dashboard's bridge panel, which previously had to look each endpoint back up in
# a DataFrame by id and silently rendered nothing when the row had been pruned.
# Bounded rather than the full body -- 100 nodes x 400 chars is ~40 KB, and the
# opening of a post is where its subject is stated anyway.
NODE_SUMMARY_CHARS = 400
LAYOUT_SEED = 42            # Fixed so the figure is reproducible between runs

PLATFORMS = ("GitHub", "Hacker News", "Dev.to", "Mastodon")

# --- Visual encoding --------------------------------------------------------
# Categorical hues validated with the data-viz palette validator at --pairs all
# (a network shows every pair adjacent, so the stricter pairlist applies):
#   worst CVD dE 9.2, worst normal-vision dE 16.3, all inside the lightness band.
# The previous brand colours failed that gate outright -- Dev.to's #333333 has zero
# chroma (reads as "no data"), and GitHub green against Hacker News orange scored
# dE 5.8 for deuteranopes, i.e. the two largest platforms were indistinguishable.
PLATFORM_COLORS = {
    "GitHub": "#2a78d6",        # blue
    "Hacker News": "#eb6834",   # orange
    "Dev.to": "#1baf7a",        # aqua
    "Mastodon": "#4a3aa7",      # violet
}

# Secondary encoding, so platform identity never rests on colour alone. This also
# carries the figure through greyscale printing and forced-colors mode.
PLATFORM_SYMBOLS = {
    "GitHub": "circle",
    "Hacker News": "square",
    "Dev.to": "diamond",
    "Mastodon": "triangle-up",
}

# Surface and ink tokens. Text never wears a series colour; the mark beside it
# carries identity.
SURFACE = "#fcfcfb"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
TEXT_MUTED = "#8a8985"

# Edges stay achromatic so they never compete with node identity. Cross-platform
# bridges -- the finding this project is about -- are solid and darker; within-platform
# edges are light and recessive. The distinction is weight plus value, not hue.
# Three levels of one VALUE ramp, not three hues: within-platform (recessive) ->
# cross-platform -> emerging bridge (the darkest ink in the figure, 19:1 against
# the surface). Spending a fifth hue on the finding would put it in competition
# with the four platform identities, and a warm accent would collide with Hacker
# News orange; a monotone value ramp reads as "how notable" for free and survives
# greyscale, CVD and forced-colors. Identity is not carried by value alone -- the
# emerging tier also gets a star glyph at the edge midpoint and a legend entry.
EDGE_CROSS_COLOR = "#52514e"
EDGE_SAME_COLOR = "#c9c8c3"
EDGE_EMERGING_COLOR = TEXT_PRIMARY



# ---------------------------------------------------------------------------
# Emerging bridges / weak ties
# ---------------------------------------------------------------------------
# The finding this dashboard exists to produce is not "these two posts are
# similar" -- the edge already says that. It is "this idea has jumped a register":
# something is being built in a repository AND argued about in a social timeline,
# which is the earliest externally visible sign that a technical development is
# becoming a market one.
#
# Three conditions have to hold at once for an edge to mean that, and each rules
# out a different way of being fooled:
#
#   1. HIGH SIMILARITY      -- guaranteed by construction; the edge exists.
#   2. LOW EMBEDDEDNESS     -- the endpoints share (almost) no neighbours. This is
#      Granovetter's weak tie: a tie whose ends have separate neighbourhoods is the
#      only path between two regions, so removing it disconnects them. Without this
#      test the "bridges" are just the densest part of one cluster that happens to
#      span two platforms, which is a well-covered topic, not an emerging one.
#   3. REGISTER GAP         -- the platforms sit far apart on the code-to-discourse
#      axis below. A Dev.to/Hacker News link is two article-shaped practitioner
#      posts about the same thing; a GitHub/Mastodon link is an implementation and
#      a public conversation, which is a genuinely different observation.
#
# Platform position on the code-to-discourse axis. Not a popularity or quality
# ranking -- it is how far a post's register sits from executable artefact and how
# close to open public discussion:
#   GitHub       source code, READMEs                      -> artefact
#   Dev.to       practitioner tutorials, long form         -> explanation
#   Hacker News  industry commentary and link discussion   -> debate
#   Mastodon     short public social posts                 -> discourse
PLATFORM_REGISTER = {
    "GitHub": 0.0,
    "Dev.to": 0.4,
    "Hacker News": 0.65,
    "Mastodon": 1.0,
}

# Short labels for the generated explanations.
REGISTER_LABEL = {
    "GitHub": "implementation",
    "Dev.to": "practitioner write-up",
    "Hacker News": "industry debate",
    "Mastodon": "public discussion",
}

# Minimum separation on that axis. 0.35 admits every pair except Dev.to <-> Hacker
# News (0.25), which is the one pair that is not a register crossing: both are
# article-shaped commentary aimed at the same readership, so a link there carries
# no "jumped context" information.
MIN_REGISTER_GAP = 0.35

# Neighbourhood Jaccard ceiling. Above this the two posts sit inside one shared
# cluster and the edge is redundant -- other paths already connect them -- so it
# is a strong tie that happens to cross platforms, not a bridge. 0.25 rather than
# 0 because at small corpus sizes one incidentally shared neighbour is noise.
MAX_NEIGHBOUR_OVERLAP = 0.25

# How many bridges to report. This is an executive finding, not a data dump: past
# roughly half a dozen nobody reads them, and the ranking below is what decides
# which half dozen.
MAX_EMERGING_BRIDGES = 6

# Cap per platform pair inside the reported set. Without it the report is
# monopolised by whichever pair has the widest register gap: measured on the
# current corpus, GitHub <-> Mastodon supplies just 8 of 113 cross-platform edges
# but its gap of 1.0 took all six slots, hiding the Dev.to <-> GitHub and
# GitHub <-> Hacker News crossings entirely. Diversifying the slate is the same
# reasoning that put MMR in the keyword extractor: the most informative six
# findings are not the six highest-scoring ones when they all say one thing.
MAX_PER_PLATFORM_PAIR = 2

# Reported bridges are held to a HIGHER similarity bar than the edge threshold,
# taken as this percentile of the cross-platform stratum's own weights. At p97 the
# cross cutoff sits at 0.393, where the weakest links are genuine false friends --
# an LLM-app repository matched a political post at 0.396 with no vocabulary in
# common. A percentile rather than a constant for the reason given in
# GraphBuilder's docstring: an absolute cosine drifts as the corpus changes.
BRIDGE_SIMILARITY_PERCENTILE = 75

# Document-frequency ceiling for a term to count as "shared subject matter".
# Measured on the current corpus, 'ai' occurs in 41 of 100 nodes: it is what the
# whole corpus is about, so reporting it as what two specific posts have in common
# is true and useless. Terms above this share of nodes are dropped from the
# explanation, which promotes the distinctive ones ('openai' at 0.05, 'llm' at
# 0.10) into the sentence instead.
MAX_TERM_DOC_FREQ = 0.25

# Words that carry no topical signal when comparing two posts' vocabulary. Kept
# deliberately small: this is a shared-term extractor, not a language model, and
# an aggressive list would strip the domain words the comparison depends on.
_MEANING_STOPWORDS = frozenset("""
a an the and or but if then than that this these those with without within from into
of to in on at by for as is are was were be been being it its it's we you your our
their his her they he she i me my not no nor so such only own same too very can will
just should now what which who whom when where why how all any both each few more
most other some new using use used uses make makes made get gets got how-to via
about after again against also because before below between during further here once
about—more show shows shown first second next last one two three
""".split())

# Tokens shorter than this are noise ('ai' survives because it is length 2).
_MIN_TERM_CHARS = 2

_TERM_SPLIT_RE = re.compile(r'[^a-z0-9+#.]+')


def _salient_terms(node_data):
    """
    The topical vocabulary of one node, as a set of lowercase tokens.

    Drawn from the stored KeyBERT phrases, the title and the opening of the body,
    then split to UNIGRAMS. Two reasons for that shape:

    Unigrams, because whole phrases are almost never shared -- the module docstring
    records that only 5 of 235 stored keywords occur in more than one post, since
    MMR diversity makes them document-specific -- but the words inside them are
    shared often ("vector search" and "vector database" share `vector`).

    Body text included, because keywords and titles alone are too thin a pool:
    measured on the current corpus, restricting the comparison to those two left
    five of six detected bridges with no shared term to report, which understates
    a real overlap rather than revealing the absence of one. The document-frequency
    filter downstream is what keeps the wider pool from admitting filler.
    """
    raw = (list(node_data.get('keywords') or [])
           + [node_data.get('title') or '', node_data.get('summary') or ''])
    terms = set()
    for chunk in raw:
        for token in _TERM_SPLIT_RE.split(str(chunk).lower()):
            token = token.strip('.+#')
            if (len(token) >= _MIN_TERM_CHARS and token not in _MEANING_STOPWORDS
                    and not token.isdigit()):
                terms.add(token)
    return terms


def _neighbour_overlap(graph, u, v):
    """
    Jaccard similarity of the two endpoints' neighbourhoods, each endpoint
    excluded from the other's.

    0.0 means the posts have no connection in common, so this edge is the only
    thing joining their two regions -- the structural definition of a bridge.
    Returns 0.0 when either node has no other neighbour, which is the strongest
    possible bridge rather than an undefined case.
    """
    nu = set(graph.neighbors(u)) - {v}
    nv = set(graph.neighbors(v)) - {u}
    if not nu or not nv:
        return 0.0
    return len(nu & nv) / len(nu | nv)


def _register_gap(platform_a, platform_b):
    """Distance between two platforms on the code-to-discourse axis."""
    a = PLATFORM_REGISTER.get(platform_a)
    b = PLATFORM_REGISTER.get(platform_b)
    if a is None or b is None:
        # An unmapped platform (a collector added later) is not silently scored
        # as a zero-gap pair, which would hide every bridge it takes part in.
        # It is treated as a mid-size gap so the edge stays eligible.
        return 0.5
    return abs(a - b)


def _article(word):
    """'a' or 'an' for a following word. The register labels start with both."""
    return 'an' if word[:1].lower() in 'aeiou' else 'a'


def _connection_meaning(node_a, node_b, weight, shared_terms):
    """
    Composes a one-or-two sentence reading of WHY two posts are connected.

    Extractive and deterministic, with no API call. That is a deliberate choice,
    not a shortcut: this text is attached to every reported bridge, it is computed
    inside a cached graph build, and the module is framework-agnostic by design.
    Spending a network round trip per edge here would make the network tab depend
    on an API key, and the honest content of the explanation -- which vocabulary
    the two posts actually share, and which two registers they span -- is present
    in the data already. The narrative layer that does need a model stays in the
    Intelligence Briefing, where the user asks for it explicitly.
    """
    reg_a = REGISTER_LABEL.get(node_a['platform'], node_a['platform'])
    reg_b = REGISTER_LABEL.get(node_b['platform'], node_b['platform'])

    # Order the sentence along the axis, so it always reads from the more
    # technical end toward the more public one -- the direction that makes
    # "crossing over" mean something.
    if PLATFORM_REGISTER.get(node_a['platform'], 0.5) > PLATFORM_REGISTER.get(node_b['platform'], 0.5):
        node_a, node_b, reg_a, reg_b = node_b, node_a, reg_b, reg_a

    # Written for a non-technical reader: the measurements behind the finding
    # (similarity, neighbourhood overlap) are shown in the network tab's advanced
    # section, not in the sentence someone reads to decide whether to care.
    if shared_terms:
        subject = ", ".join(shared_terms[:3])
        lead = f"Both are about {subject}."
    else:
        # A real and informative case: the encoder matched meaning with no
        # DISTINCTIVE vocabulary in common, which is either the same idea under two
        # communities' names or a weaker match. Saying so is more honest than
        # inventing a subject.
        lead = ("They use different words, but describe a similar idea — worth a "
                "quick read before acting on it.")

    return (f"{lead} {_article(reg_a).capitalize()} {reg_a} on {node_a['platform']} "
            f"and {_article(reg_b)} {reg_b} on {node_b['platform']} are covering the "
            f"same ground, even though the two communities aren't otherwise talking "
            f"to each other — a sign the idea is spreading beyond where it started.")


def _corpus_doc_frequency(graph):
    """
    Share of nodes each salient term appears in.

    Computed over the graph rather than the whole database because the graph is
    the sampled corpus the user is actually looking at, so "distinctive here"
    means distinctive in the view on screen.
    """
    total = max(graph.number_of_nodes(), 1)
    counts = {}
    for _, data in graph.nodes(data=True):
        for term in _salient_terms(data):
            counts[term] = counts.get(term, 0) + 1
    return {term: count / total for term, count in counts.items()}


def _shared_subject_terms(node_u, node_v, doc_freq):
    """
    The distinctive vocabulary two posts have in common, rarest first.

    Ranking by rarity rather than by position is what makes the explanation worth
    reading: both posts in an AI corpus contain 'ai', so the first shared term by
    position is almost always the least informative one available.
    """
    terms_u, terms_v = _salient_terms(node_u), _salient_terms(node_v)
    shared = {
        term for term in terms_u & terms_v
        if doc_freq.get(term, 0.0) <= MAX_TERM_DOC_FREQ
    }
    # Sorted by (frequency, term): the frequency orders by informativeness and the
    # term breaks ties deterministically, so the same bridge always describes
    # itself the same way between runs.
    return sorted(shared, key=lambda t: (doc_freq.get(t, 0.0), t))


def detect_emerging_bridges(graph, limit=MAX_EMERGING_BRIDGES):
    """
    Finds and ranks the cross-platform weak ties, annotating the graph in place.

    Sets `is_emerging` on qualifying edges and `is_emerging_bridge` on their
    endpoints, so the figure can highlight them without re-deriving anything, and
    returns the ranked findings for the metrics dictionary.

    Ranking
    -------
        score = similarity x (1 - neighbour_overlap) x register_gap x community_bonus

    A product, not a weighted sum: each factor is a necessary condition, and a
    product collapses when any one of them is weak, which is the behaviour we
    want. A sum would let a very high similarity buy its way past a zero register
    gap and re-fill the list with ordinary same-context links.

    `community_bonus` is 1.25 when the endpoints fall in different detected
    communities and 1.0 otherwise -- a modest tilt rather than a filter, because
    greedy modularity is a heuristic and on a small corpus it can put a genuine
    bridge's endpoints in one community. Treating that as disqualifying would
    discard real findings on the strength of a clustering artefact.

    Selection is then diversified across platform pairs (see MAX_PER_PLATFORM_PAIR)
    so the slate describes the ecosystem rather than its single widest gap.
    """
    if graph.number_of_edges() == 0:
        return []

    for _, _, data in graph.edges(data=True):
        data['is_emerging'] = False

    cross_edges = [(u, v, d) for u, v, d in graph.edges(data=True) if d.get('is_cross')]
    if not cross_edges:
        return []

    # Adaptive floor: a reported bridge must be strong RELATIVE TO the other
    # cross-platform links present, not merely past the edge threshold.
    weights = [float(d.get('weight', 0.0)) for _, _, d in cross_edges]
    similarity_floor = float(np.percentile(weights, BRIDGE_SIMILARITY_PERCENTILE))

    # Edge betweenness is the textbook measure of how much traffic an edge
    # carries between regions. It is O(V*E), which on a corpus capped at ~100
    # nodes is milliseconds, and it is reported rather than scored because it is
    # already strongly correlated with (1 - overlap) -- including it in the
    # product would double-count the same structural fact.
    try:
        edge_betweenness = nx.edge_betweenness_centrality(graph, weight=None)
    except (ZeroDivisionError, nx.NetworkXError):
        edge_betweenness = {}

    doc_freq = _corpus_doc_frequency(graph)

    candidates = []
    for u, v, data in cross_edges:
        weight = float(data.get('weight', 0.0))
        if weight < similarity_floor:
            continue

        node_u, node_v = graph.nodes[u], graph.nodes[v]
        gap = _register_gap(node_u['platform'], node_v['platform'])
        if gap < MIN_REGISTER_GAP:
            continue

        overlap = _neighbour_overlap(graph, u, v)
        if overlap > MAX_NEIGHBOUR_OVERLAP:
            continue

        cross_community = (node_u.get('community') is not None
                           and node_u.get('community') != node_v.get('community'))
        score = weight * (1.0 - overlap) * gap * (1.25 if cross_community else 1.0)
        shared_terms = _shared_subject_terms(node_u, node_v, doc_freq)

        candidates.append({
            'source': u,
            'target': v,
            'source_title': node_u['title'],
            'target_title': node_v['title'],
            'source_platform': node_u['platform'],
            'target_platform': node_v['platform'],
            'source_url': node_u.get('url', ''),
            'target_url': node_v.get('url', ''),
            'source_keywords': list(node_u.get('keywords') or [])[:5],
            'target_keywords': list(node_v.get('keywords') or [])[:5],
            'similarity': weight,
            'neighbour_overlap': overlap,
            'register_gap': gap,
            'cross_community': cross_community,
            'edge_betweenness': float(edge_betweenness.get((u, v),
                                      edge_betweenness.get((v, u), 0.0))),
            'shared_terms': shared_terms[:6],
            'score': score,
            'meaning': _connection_meaning(node_u, node_v, weight, shared_terms),
            'platform_pair': " ↔ ".join(sorted((node_u['platform'], node_v['platform']))),
        })

    candidates.sort(key=lambda f: f['score'], reverse=True)

    # Two passes. The first respects the per-pair cap; the second tops the slate
    # back up from whatever was skipped, so a corpus that genuinely only contains
    # one kind of crossing still returns a full list instead of a short one.
    findings, pair_counts, deferred = [], {}, []
    for candidate in candidates:
        pair = candidate['platform_pair']
        if pair_counts.get(pair, 0) >= MAX_PER_PLATFORM_PAIR:
            deferred.append(candidate)
            continue
        pair_counts[pair] = pair_counts.get(pair, 0) + 1
        findings.append(candidate)
        if len(findings) >= limit:
            break
    findings.extend(deferred[:max(0, limit - len(findings))])

    # Annotate only the reported ones. Flagging every qualifying edge would put
    # the heaviest visual weight in the figure on a long tail the UI never names,
    # so the highlight and the list stay in exact correspondence.
    for finding in findings:
        graph.edges[finding['source'], finding['target']]['is_emerging'] = True
        for nid in (finding['source'], finding['target']):
            graph.nodes[nid]['is_emerging_bridge'] = True

    return findings


_ENCODER = None


def get_encoder():
    """
    Process-wide singleton for the sentence encoder.

    Imported lazily so that merely importing this module -- which the Streamlit app
    does at startup -- never pays the model-loading cost, and cached because loading
    costs ~3.7s against ~0.65s to encode the whole corpus: rebuilding the graph when
    the threshold slider moves must not reload the model each time.
    """
    global _ENCODER
    if _ENCODER is None:
        from sentence_transformers import SentenceTransformer
        _ENCODER = SentenceTransformer(EMBED_MODEL_NAME)
    return _ENCODER


def _normalize(pos, radius):
    """Recentres a layout on the origin and scales it to fit inside `radius`."""
    if not pos:
        return {}
    coords = np.array(list(pos.values()), dtype=float)
    centre = coords.mean(axis=0)
    coords -= centre
    extent = np.abs(coords).max()
    if extent > 0:
        coords *= radius / extent
    return dict(zip(pos.keys(), coords))


def _layout_components(graph, core_radius=1.0, ring_radius=1.55):
    """
    Lays out each connected component separately, then packs them.

    `spring_layout` applied to a disconnected graph flings the small components far
    from the giant one, which squanders most of the canvas: the first render put the
    whole core in the bottom-right quadrant with a detached pair marooned top-left.
    Here the giant component gets the centre and everything else -- small components
    and isolates alike -- is arranged on a ring around it, so screen area tracks
    information content.
    """
    if graph.number_of_nodes() == 0:
        return {}

    components = sorted(nx.connected_components(graph), key=len, reverse=True)
    giant = graph.subgraph(components[0])
    pos = _normalize(
        nx.spring_layout(giant, weight='weight', seed=LAYOUT_SEED,
                         k=2.2 / np.sqrt(max(giant.number_of_nodes(), 2)), iterations=400),
        core_radius,
    ) if giant.number_of_nodes() > 1 else {next(iter(components[0])): np.array([0.0, 0.0])}

    satellites = components[1:]
    for idx, members in enumerate(satellites):
        angle = 2 * np.pi * idx / max(len(satellites), 1)
        anchor_pt = np.array([ring_radius * np.cos(angle), ring_radius * np.sin(angle)])
        sub = graph.subgraph(members)
        if sub.number_of_nodes() == 1:
            pos[next(iter(members))] = anchor_pt
            continue
        local = _normalize(
            nx.spring_layout(sub, weight='weight', seed=LAYOUT_SEED, iterations=120),
            0.18,
        )
        for node, coord in local.items():
            pos[node] = coord + anchor_pt
    return pos


class GraphBuilder:
    """
    Builds a semantic similarity graph across platforms.

    The public surface is unchanged from the previous revision -- `build_graph()`
    still returns an `nx.Graph` whose edges carry `weight` and `is_cross` -- so
    existing callers keep working. `build()` additionally returns the metrics.
    """

    def __init__(self, percentile=97.0, min_similarity=0.35,
                 top_n_per_platform=25, db_path=None):
        """
        percentile        Edge cutoff expressed as a percentile of the observed
                          similarity distribution rather than an absolute cosine
                          value. A fixed 0.55 produced only 6 cross-platform edges
                          on the current corpus and would drift as the corpus grows;
                          a percentile adapts to whatever distribution is present.
                          The 97.0 default was chosen by sweeping 94-99 on the current
                          corpus: it yields ~106 edges at modularity 0.51, where the
                          cluster structure is strongest while the figure stays legible
                          (94 gives 210 edges and an unreadable hairball; 99 gives 36
                          edges and 48 isolates).
        min_similarity    Absolute floor applied underneath the percentile, so a
                          sparse or homogeneous corpus cannot manufacture edges
                          between genuinely unrelated posts.
        top_n_per_platform  Balanced sampling cap, to stop a prolific platform from
                          dominating the node set.
        """
        self.percentile = percentile
        self.min_similarity = min_similarity
        self.top_n_per_platform = top_n_per_platform
        self.db_path = db_path or DB_PATH
        self.graph = nx.Graph()
        self.metrics = {}
        self.thresholds = {}

    # --- Data access --------------------------------------------------------

    def load_posts(self):
        """Balanced sample: the top N posts per platform by trend score."""
        if not os.path.exists(self.db_path):
            return []

        rows = []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            for platform in PLATFORMS:
                cursor = conn.execute(
                    """
                    SELECT id, title, content, source_platform, trend_score,
                           url, author, keywords, published_at
                    FROM unified_posts
                    WHERE source_platform = ?
                    ORDER BY trend_score DESC
                    LIMIT ?
                    """,
                    (platform, self.top_n_per_platform),
                )
                rows.extend(dict(r) for r in cursor.fetchall())
        return rows

    # --- Embedding ----------------------------------------------------------

    @staticmethod
    def embed(posts):
        """
        Encodes posts to unit-norm vectors, reusing the process-wide encoder.
        """
        texts = [
            f"{p['title']}. {(p.get('content') or '')[:EMBED_CHAR_BUDGET]}"
            for p in posts
        ]
        return get_encoder().encode(
            texts, batch_size=32, show_progress_bar=False, normalize_embeddings=True
        )

    # --- Thresholding -------------------------------------------------------

    def _stratified_thresholds(self, sim, platforms):
        """
        Derives a cutoff per pair type from that stratum's own distribution.

        Posts from the same platform are systematically more similar to each other
        than to posts elsewhere -- shared house style, shared topic mix, shared
        length. A single global cutoff therefore saturates the within-platform
        stratum before the cross-platform stratum contributes anything. Taking the
        same percentile within each stratum equalises that bias, which is what makes
        the cross-platform edge count meaningful rather than an artefact.
        """
        cross_vals, same_vals = [], []
        for i, j in itertools.combinations(range(len(platforms)), 2):
            (cross_vals if platforms[i] != platforms[j] else same_vals).append(sim[i][j])

        def cutoff(values):
            if not values:
                return 1.0    # No pairs in this stratum: admit nothing
            return max(float(np.percentile(values, self.percentile)), self.min_similarity)

        return {'cross': cutoff(cross_vals), 'same': cutoff(same_vals)}

    # --- Metrics ------------------------------------------------------------

    def _compute_metrics(self, graph, posts):
        """
        Structural measures that turn the picture into reportable findings.

        Returned rather than printed so the caller decides how to surface them.
        """
        n_nodes, n_edges = graph.number_of_nodes(), graph.number_of_edges()
        metrics = {
            'nodes': n_nodes,
            'edges': n_edges,
            'density': nx.density(graph) if n_nodes > 1 else 0.0,
            'cross_edges': 0,
            'same_edges': 0,
            'cross_ratio': 0.0,
            'assortativity': None,
            'modularity': None,
            'communities': [],
            'n_communities': 0,
            'top_bridges': [],
            'emerging_bridges': [],
            'n_emerging_bridges': 0,
            'bridge_criteria': {
                'min_register_gap': MIN_REGISTER_GAP,
                'max_neighbour_overlap': MAX_NEIGHBOUR_OVERLAP,
                'similarity_percentile': BRIDGE_SIMILARITY_PERCENTILE,
                'max_per_platform_pair': MAX_PER_PLATFORM_PAIR,
            },
            'isolates': len(list(nx.isolates(graph))),
            'thresholds': self.thresholds,
            'percentile': self.percentile,
        }
        if n_edges == 0:
            return metrics

        metrics['cross_edges'] = sum(1 for *_, d in graph.edges(data=True) if d['is_cross'])
        metrics['same_edges'] = n_edges - metrics['cross_edges']
        metrics['cross_ratio'] = metrics['cross_edges'] / n_edges

        # Assortativity by platform. Negative means posts preferentially connect
        # ACROSS platforms -- direct quantitative evidence of cross-platform
        # convergence, rather than an assertion that the picture looks connected.
        try:
            metrics['assortativity'] = float(
                nx.attribute_assortativity_coefficient(graph, 'platform')
            )
        except (ValueError, ZeroDivisionError, nx.NetworkXError):
            metrics['assortativity'] = None

        # Betweenness identifies posts that sit ON the shortest paths between
        # clusters: the actual connective tissue, which is not the same thing as
        # the most-connected or highest-scoring post.
        betweenness = nx.betweenness_centrality(graph, weight=None)
        nx.set_node_attributes(graph, betweenness, 'betweenness')
        titles = {p['id']: p['title'] for p in posts}
        plats = {p['id']: p['source_platform'] for p in posts}
        metrics['top_bridges'] = [
            {'id': nid, 'title': titles.get(nid, ''), 'platform': plats.get(nid, ''),
             'betweenness': score, 'degree': graph.degree(nid)}
            for nid, score in sorted(betweenness.items(), key=lambda kv: kv[1], reverse=True)[:8]
            if score > 0
        ]

        # Greedy modularity finds topic clusters that emerge from the similarity
        # structure alone -- no platform label and no keyword list is consulted.
        try:
            communities = [
                sorted(c) for c in nx.community.greedy_modularity_communities(graph, weight='weight')
            ]
            metrics['communities'] = communities
            metrics['n_communities'] = len(communities)
            metrics['modularity'] = float(nx.community.modularity(graph, communities, weight='weight'))
            for idx, members in enumerate(communities):
                for nid in members:
                    graph.nodes[nid]['community'] = idx
        except (ZeroDivisionError, nx.NetworkXError, StopIteration):
            pass

        # Last, because the weak-tie ranking reads the community labels assigned
        # immediately above. It is also what annotates the graph for the figure,
        # so it must run on every build and not only when communities succeed.
        metrics['emerging_bridges'] = detect_emerging_bridges(graph)
        metrics['n_emerging_bridges'] = len(metrics['emerging_bridges'])

        return metrics

    # --- Construction -------------------------------------------------------

    def build(self):
        """Builds the graph and its metrics. Returns (graph, metrics)."""
        self.graph = nx.Graph()
        posts = self.load_posts()
        if len(posts) < 2:
            self.metrics = self._compute_metrics(self.graph, posts)
            return self.graph, self.metrics

        embeddings = self.embed(posts)
        embeddings_array = embeddings.cpu().numpy() if hasattr(embeddings, 'cpu') else np.array(embeddings)
        sim = cosine_similarity(embeddings_array)
        np.fill_diagonal(sim, 0.0)

        platforms = [p['source_platform'] for p in posts]
        self.thresholds = self._stratified_thresholds(sim, platforms)

        # --- Nodes ---
        for post in posts:
            try:
                keywords = json.loads(post.get('keywords') or '[]')
            except (json.JSONDecodeError, TypeError):
                keywords = []
            platform = post['source_platform']
            self.graph.add_node(
                post['id'],
                label=post['title'],
                platform=platform,
                title=post['title'],
                url=post.get('url') or '',
                author=post.get('author') or '',
                trend_score=float(post.get('trend_score') or 0.0),
                keywords=keywords,
                summary=(post.get('content') or '')[:NODE_SUMMARY_CHARS],
                published_at=post.get('published_at') or '',
                color=PLATFORM_COLORS.get(platform, TEXT_SECONDARY),
                symbol=PLATFORM_SYMBOLS.get(platform, 'circle'),
            )

        # --- Edges ---
        for i, j in itertools.combinations(range(len(posts)), 2):
            score = float(sim[i][j])
            is_cross = platforms[i] != platforms[j]
            if score >= self.thresholds['cross' if is_cross else 'same']:
                self.graph.add_edge(
                    posts[i]['id'], posts[j]['id'],
                    weight=score,
                    is_cross=is_cross,
                )

        # --- Layout ---
        # Force-directed, so distance on screen reflects similarity. The previous
        # circular layout with physics disabled placed nodes by insertion order,
        # which actively concealed the cluster structure the graph exists to show.
        for node, (x, y) in _layout_components(self.graph).items():
            self.graph.nodes[node]['x'] = float(x)
            self.graph.nodes[node]['y'] = float(y)

        self.metrics = self._compute_metrics(self.graph, posts)
        return self.graph, self.metrics

    def build_graph(self):
        """Backwards-compatible entry point returning only the graph."""
        return self.build()[0]


# ---------------------------------------------------------------------------
# Figure construction
# ---------------------------------------------------------------------------

def _wrap(text, width=48):
    """Soft-wraps hover text so long titles do not run off the tooltip."""
    words, lines, current = str(text).split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return "<br>".join(lines)


def build_network_figure(graph, metrics, show_isolates=True, label_top_bridges=5):
    """
    Renders the semantic graph as a Plotly figure.

    Plotly rather than pyvis: it matches the dashboard's existing charts, needs no
    embedded HTML document, and the repository's vendored vis-network assets are
    incomplete (vis-network.css is a zero-byte file).
    """
    import plotly.graph_objects as go

    fig = go.Figure()

    nodes = list(graph.nodes())
    if not show_isolates:
        nodes = [n for n in nodes if graph.degree(n) > 0]
    node_set = set(nodes)

    if not nodes:
        fig.add_annotation(text="No nodes to display", showarrow=False,
                           font=dict(color=TEXT_SECONDARY, size=14))
        fig.update_layout(height=680, paper_bgcolor=SURFACE, plot_bgcolor=SURFACE)
        return fig

    # Meaning text, keyed by unordered endpoint pair, so the tooltip can explain a
    # highlighted edge without the figure re-deriving the analysis.
    bridge_meanings = {
        frozenset((b['source'], b['target'])): b
        for b in metrics.get('emerging_bridges', [])
    }

    # --- Edges ---
    # Three traces rather than one per edge: identity is carried by style, and a
    # separate midpoint trace supplies per-edge hover without hundreds of traces.
    #
    # Back to front, so the finding is never overdrawn by the background: the
    # recessive within-platform links, then cross-platform links, then the
    # emerging bridges. The three tiers are one VALUE ramp on an achromatic ink
    # (see EDGE_EMERGING_COLOR) and the emerging tier additionally carries a star
    # glyph, so the highlight does not rest on darkness alone.
    def tier(edge_data):
        if edge_data.get('is_emerging'):
            return 'emerging'
        return 'cross' if edge_data['is_cross'] else 'same'

    for key, color, width, dash, name in (
        ('same', EDGE_SAME_COLOR, 1.0, 'dot', 'Same-community link'),
        ('cross', EDGE_CROSS_COLOR, 2.0, 'solid', 'Cross-community link'),
        ('emerging', EDGE_EMERGING_COLOR, 3.5, 'solid', 'Surprising connection'),
    ):
        xs, ys = [], []
        for u, v, d in graph.edges(data=True):
            if tier(d) != key or u not in node_set or v not in node_set:
                continue
            xs += [graph.nodes[u]['x'], graph.nodes[v]['x'], None]
            ys += [graph.nodes[u]['y'], graph.nodes[v]['y'], None]
        if not xs:
            continue
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode='lines', name=name,
            line=dict(color=color, width=width, dash=dash),
            hoverinfo='skip', legendgroup=name, showlegend=True,
        ))

    # Invisible midpoints carry the edge tooltip.
    mx, my, mtext = [], [], []
    for u, v, d in graph.edges(data=True):
        if u not in node_set or v not in node_set:
            continue
        mx.append((graph.nodes[u]['x'] + graph.nodes[v]['x']) / 2)
        my.append((graph.nodes[u]['y'] + graph.nodes[v]['y']) / 2)
        finding = bridge_meanings.get(frozenset((u, v)))
        if finding:
            kind = "★ Surprising connection across communities"
        elif d['is_cross']:
            kind = "Cross-community link"
        else:
            kind = "Same-community link"

        parts = [
            f"<b>{d['weight'] * 100:.1f}% similarity</b><br>{kind}<br><br>"
            f"{graph.nodes[u]['platform']}: {_wrap(graph.nodes[u]['title'], 40)}<br>"
            f"{graph.nodes[v]['platform']}: {_wrap(graph.nodes[v]['title'], 40)}"
        ]
        if finding:
            # The whole point of the highlight is that the reader immediately
            # learns WHY, so the explanation travels with the mark rather than
            # living only in a panel further down the page.
            parts.append(f"<br><br><i>{_wrap(finding['meaning'], 52)}</i>")
        mtext.append("".join(parts))
    if mx:
        fig.add_trace(go.Scatter(
            x=mx, y=my, mode='markers', showlegend=False,
            marker=dict(size=10, color='rgba(0,0,0,0)'),
            hoverinfo='text', hovertext=mtext,
        ))

    # --- Nodes, one trace per platform so the legend reads as identity ---
    scores = [graph.nodes[n]['trend_score'] for n in nodes] or [0]
    lo, hi = min(scores), max(scores)
    span = (hi - lo) or 1.0

    for platform in PLATFORMS:
        members = [n for n in nodes if graph.nodes[n]['platform'] == platform]
        if not members:
            continue
        hover = []
        for n in members:
            nd = graph.nodes[n]
            kw = ", ".join(nd['keywords'][:4]) if nd['keywords'] else "—"
            hover.append(
                f"<b>{_wrap(nd['title'])}</b><br><br>"
                f"Platform: {nd['platform']}<br>"
                f"Trend score: {nd['trend_score']:.1f}<br>"
                f"Connections: {graph.degree(n)}<br>"
                f"Bridge score: {nd.get('betweenness', 0):.3f}<br>"
                f"Keywords: {kw}"
            )
        fig.add_trace(go.Scatter(
            x=[graph.nodes[n]['x'] for n in members],
            y=[graph.nodes[n]['y'] for n in members],
            mode='markers', name=platform, legendgroup=platform,
            marker=dict(
                size=[10 + 20 * (graph.nodes[n]['trend_score'] - lo) / span for n in members],
                color=PLATFORM_COLORS[platform],
                symbol=PLATFORM_SYMBOLS[platform],
                # 2px ring keeps overlapping nodes separable in dense regions. It
                # switches from the surface to ink on a bridge endpoint, so the
                # posts a highlighted edge joins are findable without tracing the
                # line -- the node keeps its platform hue and symbol either way.
                line=dict(
                    color=[EDGE_EMERGING_COLOR if graph.nodes[n].get('is_emerging_bridge')
                           else SURFACE for n in members],
                    width=2,
                ),
            ),
            hoverinfo='text', hovertext=hover,
        ))

    # --- Star glyphs on the emerging bridges ---
    # The second encoding channel for the highlight. A reader in greyscale, with a
    # colour-vision deficiency, or in forced-colors mode still sees which edges are
    # flagged, and the legend entry names them.
    star_x, star_y, star_text = [], [], []
    for u, v, d in graph.edges(data=True):
        if not d.get('is_emerging') or u not in node_set or v not in node_set:
            continue
        finding = bridge_meanings.get(frozenset((u, v)), {})
        star_x.append((graph.nodes[u]['x'] + graph.nodes[v]['x']) / 2)
        star_y.append((graph.nodes[u]['y'] + graph.nodes[v]['y']) / 2)
        star_text.append(
            f"<b>★ Emerging bridge</b><br>"
            f"{finding.get('platform_pair', '')}<br>"
            f"Similarity {d['weight'] * 100:.0f}% · "
            f"neighbourhood overlap {finding.get('neighbour_overlap', 0) * 100:.0f}%"
            f"<br><br><i>{_wrap(finding.get('meaning', ''), 52)}</i>"
        )
    if star_x:
        fig.add_trace(go.Scatter(
            x=star_x, y=star_y, mode='markers',
            name='Surprising connection', legendgroup='Surprising connection',
            showlegend=False,
            marker=dict(size=13, symbol='star', color=EDGE_EMERGING_COLOR,
                        line=dict(color=SURFACE, width=1.5)),
            hoverinfo='text', hovertext=star_text,
        ))

    # --- Selective direct labels: only the strongest bridges, never every node ---
    # Candidates are additionally spaced apart: the top bridges by betweenness all
    # live in the dense core, so labelling them naively overprinted three titles on
    # top of each other. A label is kept only if it clears every accepted one.
    span_xy = max(
        max(graph.nodes[n]['x'] for n in nodes) - min(graph.nodes[n]['x'] for n in nodes),
        max(graph.nodes[n]['y'] for n in nodes) - min(graph.nodes[n]['y'] for n in nodes),
        1e-6,
    )
    min_gap = 0.16 * span_xy

    # Emerging-bridge endpoints are offered a label FIRST, then the betweenness
    # ranking tops the list up. The flagged edges are the headline finding, so the
    # few labels the figure can afford should name them rather than leave the
    # reader hovering to find out which posts the starred edges join.
    label_candidates = []
    for b in metrics.get('emerging_bridges', []):
        for nid, title, platform in ((b['source'], b['source_title'], b['source_platform']),
                                     (b['target'], b['target_title'], b['target_platform'])):
            label_candidates.append({'id': nid, 'title': title, 'platform': platform})
    label_candidates += metrics.get('top_bridges', [])

    placed, seen_ids = [], set()
    for b in label_candidates:
        if (b['id'] not in node_set or b['id'] in seen_ids
                or len(placed) >= label_top_bridges):
            continue
        bx, by = graph.nodes[b['id']]['x'], graph.nodes[b['id']]['y']
        if any(abs(bx - px) < min_gap and abs(by - py) < min_gap * 0.5 for px, py, _ in placed):
            continue
        seen_ids.add(b['id'])
        placed.append((bx, by, b))

    # Annotations rather than a text trace, because they are the only Plotly text
    # that can carry a background. Rendered on a force-directed layout, a bare
    # label lands on whatever mark or edge happens to be behind it -- the first
    # render had four titles printed across nodes and lines, all four unreadable.
    # A surface-coloured plate at 82% opacity keeps the text legible without
    # hiding the structure underneath.
    for i, (bx, by, b) in enumerate(placed):
        title = (b['title'][:24] + '…') if len(b['title']) > 24 else b['title']
        fig.add_annotation(
            x=bx, y=by, text=title, showarrow=False,
            # Alternating placement gives adjacent labels a second axis of separation
            yshift=16 if i % 2 == 0 else -16,
            # Ink token, not the series colour: the mark carries identity, not the text
            font=dict(size=11, color=TEXT_PRIMARY),
            bgcolor="rgba(252,252,251,0.82)", borderpad=2,
        )

    axis = dict(showgrid=False, zeroline=False, showticklabels=False, visible=False)
    fig.update_layout(
        height=680,
        margin=dict(t=10, l=10, r=10, b=10),
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
        xaxis=axis,
        # scaleanchor forces one unit of x to equal one unit of y. Without it the
        # layout is stretched to the container's aspect ratio and on-screen distance
        # no longer corresponds to semantic distance.
        yaxis=dict(**axis, scaleanchor='x', scaleratio=1),
        hovermode='closest',
        hoverlabel=dict(bgcolor="#ffffff", bordercolor="#e6e5e1",
                        font=dict(color=TEXT_PRIMARY, size=12)),
        legend=dict(orientation='h', yanchor='bottom', y=1.01, xanchor='left', x=0,
                    font=dict(color=TEXT_SECONDARY, size=12),
                    bgcolor='rgba(0,0,0,0)'),
    )
    return fig
