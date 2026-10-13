"""
Emerging-bridge detection on the graph built by graph_analyzer: pairs of posts from
distant communities that discuss the same idea while sharing almost no neighbours.
"""

import re
from collections import Counter

import numpy as np

# A bridge needs three conditions, each ruling out a different false positive:
#   1. High similarity  -- the edge clears a stricter, percentile-based floor.
#   2. Low embeddedness -- (almost) no shared neighbours (Granovetter's weak tie);
#      otherwise it is just the dense core of one cluster spanning two platforms.
#   3. Register gap     -- the platforms lie far apart on the code-to-discourse axis.
# Axis position (not a ranking): GitHub -> Dev.to -> Hacker News -> Mastodon.
PLATFORM_REGISTER = {
    "GitHub": 0.0,
    "Dev.to": 0.4,
    "Hacker News": 0.65,
    "Mastodon": 1.0,
}

REGISTER_LABEL = {
    "GitHub": "implementation",
    "Dev.to": "practitioner write-up",
    "Hacker News": "industry debate",
    "Mastodon": "public discussion",
}

# Excludes only Dev.to <-> Hacker News (gap 0.25): same article-style readership.
MIN_REGISTER_GAP = 0.35

# Jaccard ceiling on endpoint neighbourhoods; tolerates one incidental shared neighbour.
MAX_NEIGHBOUR_OVERLAP = 0.25

MAX_EMERGING_BRIDGES = 6

# Per-pair diversity cap, else GitHub <-> Mastodon (gap 1.0) would take every slot.
MAX_PER_PLATFORM_PAIR = 2

# Bridge floor as a percentile of cross-platform edge weights; drops coincidental edges.
BRIDGE_SIMILARITY_PERCENTILE = 75

# Terms in over a quarter of nodes describe the corpus (usually the topic itself).
MAX_TERM_DOC_FREQ = 0.25

# Deliberately small, so domain vocabulary survives.
_MEANING_STOPWORDS = frozenset("""
a an the and or but if then than that this these those with without within from into
of to in on at by for as is are was were be been being it its it's we you your our
their his her they he she i me my not no nor so such only own same too very can will
just should now what which who whom when where why how all any both each few more
most other some new using use used uses make makes made get gets got how-to via
about after again against also because before below between during further here once
about—more show shows shown first second next last one two three
""".split())

# Two characters keeps meaningful acronyms such as 'ai'.
_MIN_TERM_CHARS = 2

# Keeps tokens like 'c++', 'c#', 'node.js' intact.
_TERM_SPLIT_RE = re.compile(r'[^a-z0-9+#.]+')


def _salient_terms(node_data):
    """
    Lowercase unigrams from a node's keywords, title and body opening. Phrases are
    split because whole phrases are rarely shared; generic words are removed later
    by the document-frequency filter.
    """
    chunks = [*(node_data.get('keywords') or []),
              node_data.get('title') or '', node_data.get('summary') or '']
    terms = set()
    for chunk in chunks:
        for token in _TERM_SPLIT_RE.split(str(chunk).lower()):
            token = token.strip('.+#')
            if (len(token) >= _MIN_TERM_CHARS and token not in _MEANING_STOPWORDS
                    and not token.isdigit()):
                terms.add(token)
    return terms


def _neighbour_overlap(graph, u, v):
    """
    Jaccard similarity of the endpoints' neighbourhoods (each excluding the other);
    0.0 for a leaf endpoint instead of an undefined 0/0.
    """
    nu = set(graph.neighbors(u)) - {v}
    nv = set(graph.neighbors(v)) - {u}
    if not nu or not nv:
        return 0.0
    return len(nu & nv) / len(nu | nv)


def _register_gap(platform_a, platform_b):
    """|register(a) - register(b)| in [0, 1]; 0.5 for an unmapped platform."""
    a = PLATFORM_REGISTER.get(platform_a)
    b = PLATFORM_REGISTER.get(platform_b)
    if a is None or b is None:
        # Mid-range rather than zero, so an unmapped platform's edges stay eligible.
        return 0.5
    return abs(a - b)


def _article(word):
    """Indefinite article ('a' or 'an') for `word`."""
    return 'an' if word[:1].lower() in 'aeiou' else 'a'


def _connection_meaning(node_a, node_b, shared_terms):
    """
    Plain-English reason two posts are linked, assembled from the shared vocabulary
    and platform registers, so it never claims anything the data does not contain.
    """
    # Always read from the more technical platform towards the more public one.
    if PLATFORM_REGISTER.get(node_a['platform'], 0.5) > PLATFORM_REGISTER.get(node_b['platform'], 0.5):
        node_a, node_b = node_b, node_a
    reg_a = REGISTER_LABEL.get(node_a['platform'], node_a['platform'])
    reg_b = REGISTER_LABEL.get(node_b['platform'], node_b['platform'])

    if shared_terms:
        lead = f"Both are about {', '.join(shared_terms[:3])}."
    else:
        # No distinctive shared vocabulary: say so rather than invent a subject.
        lead = ("They use different words, but describe a similar idea — worth a "
                "quick read before acting on it.")

    return (f"{lead} {_article(reg_a).capitalize()} {reg_a} on {node_a['platform']} "
            f"and {_article(reg_b)} {reg_b} on {node_b['platform']} are covering the "
            f"same ground, even though the two communities aren't otherwise talking "
            f"to each other — a sign the idea is spreading beyond where it started.")


def _corpus_doc_frequency(graph):
    """Share of graph nodes each salient term appears in (distinctive within the sample shown)."""
    total = max(graph.number_of_nodes(), 1)
    counts = Counter(term for _, data in graph.nodes(data=True) for term in _salient_terms(data))
    return {term: count / total for term, count in counts.items()}


def _shared_subject_terms(node_u, node_v, doc_freq):
    """Distinctive vocabulary two posts share, rarest first."""
    shared = {
        term for term in _salient_terms(node_u) & _salient_terms(node_v)
        if doc_freq.get(term, 0.0) <= MAX_TERM_DOC_FREQ
    }
    # Alphabetical tie-break keeps descriptions identical across runs.
    return sorted(shared, key=lambda t: (doc_freq.get(t, 0.0), t))


def detect_emerging_bridges(graph, limit=MAX_EMERGING_BRIDGES):
    """
    Finds and ranks the cross-platform weak ties of `graph`.

        score = similarity x (1 - neighbour_overlap) x register_gap x community_bonus

    A product because each factor is a necessary condition. `community_bonus` (1.25
    across detected communities) is a soft weight, since greedy modularity is only a
    heuristic on small graphs. Selection is capped per platform pair.

    Returns:
        list[dict]: Ranked, self-contained findings.
    """
    cross_edges = [(u, v, d) for u, v, d in graph.edges(data=True) if d.get('is_cross')]
    if not cross_edges:
        return []

    weights = [float(d.get('weight', 0.0)) for _, _, d in cross_edges]
    similarity_floor = float(np.percentile(weights, BRIDGE_SIMILARITY_PERCENTILE))
    doc_freq = _corpus_doc_frequency(graph)

    candidates = []
    for u, v, data in cross_edges:
        weight = float(data.get('weight', 0.0))
        if weight < similarity_floor:
            continue

        # Register gap is checked before the costlier neighbourhood overlap.
        node_u, node_v = graph.nodes[u], graph.nodes[v]
        gap = _register_gap(node_u['platform'], node_v['platform'])
        if gap < MIN_REGISTER_GAP:
            continue

        overlap = _neighbour_overlap(graph, u, v)
        if overlap > MAX_NEIGHBOUR_OVERLAP:
            continue

        # A missing label (community detection failed) never counts as different.
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
            'shared_terms': shared_terms[:6],
            'score': score,
            'meaning': _connection_meaning(node_u, node_v, shared_terms),
            # Order-independent key for the per-pair diversity cap.
            'platform_pair': " ↔ ".join(sorted((node_u['platform'], node_v['platform']))),
        })

    candidates.sort(key=lambda f: f['score'], reverse=True)

    # Score order under the per-pair cap, then top up from deferred candidates.
    findings, deferred, pair_counts = [], [], Counter()
    for candidate in candidates:
        pair = candidate['platform_pair']
        if pair_counts[pair] >= MAX_PER_PLATFORM_PAIR:
            deferred.append(candidate)
            continue
        pair_counts[pair] += 1
        findings.append(candidate)
        if len(findings) >= limit:
            break
    findings.extend(deferred[:max(0, limit - len(findings))])
    return findings
