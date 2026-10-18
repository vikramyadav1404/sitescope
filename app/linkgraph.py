"""Internal link graph with NetworkX: PageRank, click depth and orphan pages."""
from __future__ import annotations

import networkx as nx

from .crawler import CrawlResult


def build_graph(result: CrawlResult) -> nx.DiGraph:
    g = nx.DiGraph()
    for url, page in result.pages.items():
        g.add_node(url)
        for link in set(page.internal_links):
            if link in result.pages and link != url:
                g.add_edge(url, link)
    return g


def pagerank(g: nx.DiGraph, alpha: float = 0.85, iters: int = 100, tol: float = 1e-8) -> dict[str, float]:
    """Power-iteration PageRank (no numpy/scipy needed). Dangling pages share
    their rank with every page, as in the original Brin and Page paper."""
    n = g.number_of_nodes()
    if n == 0:
        return {}
    nodes = list(g.nodes())
    rank = dict.fromkeys(nodes, 1.0 / n)
    out_deg = {u: g.out_degree(u) for u in nodes}
    for _ in range(iters):
        dangling = alpha * sum(rank[u] for u in nodes if out_deg[u] == 0) / n
        new = dict.fromkeys(nodes, (1.0 - alpha) / n + dangling)
        for u in nodes:
            if out_deg[u]:
                share = alpha * rank[u] / out_deg[u]
                for v in g.successors(u):
                    new[v] += share
        if sum(abs(new[u] - rank[u]) for u in nodes) < n * tol:
            return new
        rank = new
    return rank


def analyse(result: CrawlResult) -> dict:
    g = build_graph(result)
    pr = pagerank(g)
    depths = nx.single_source_shortest_path_length(g, result.start_url) if result.start_url in g else {}
    linked = {v for _, v in g.edges()}
    orphans = sorted(u for u in result.sitemap_urls if u not in linked and u != result.start_url)
    top = sorted(pr.items(), key=lambda kv: -kv[1])[:10]
    return {
        "nodes": g.number_of_nodes(),
        "edges": g.number_of_edges(),
        "pagerank_top": [{"url": u, "score": round(s, 4)} for u, s in top],
        "max_depth": max(depths.values()) if depths else 0,
        "orphans": orphans,
    }
