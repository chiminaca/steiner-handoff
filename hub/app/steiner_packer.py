"""Hub-Penalized Kou Terminal-Steiner-Tree solver, NumPy PPR packer, and L0-L3 ELK builder."""

from __future__ import annotations

import heapq
import html
import json
import math
import re
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]


RELATION_BASE_WEIGHT: dict[str, float] = {
    "governs": 0.25,
    "routes_to": 0.28,
    "calls_cross_repo": 0.35,
    "validates": 0.55,
    "calls": 0.60,
    "inherits": 0.70,
    "implements": 0.70,
    "defines": 0.85,
    "references": 0.90,
    "uses": 0.95,
    "contains": 1.35,
    "imports_from": 1.80,
    "imports": 2.10,
}
DEFAULT_EDGE_WEIGHT: float = 1.0


class SeedResolutionError(ValueError):
    """Raised when none of the requested seeds match any node in the graph."""


def _is_invariant(node_attr: dict[str, Any]) -> bool:
    return (
        str(node_attr.get("kind", "")).lower() == "domain_invariant"
        or str(node_attr.get("type", "")).lower() == "domain_invariant"
    )


def _estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 3.6))


class SteinerPacker:
    """Computes Hub-Penalized Kou Steiner Trees, vectorized PPR, XML packs, and ELK L0-L3 graphs."""

    def __init__(
        self,
        graph_data: str | Path | dict[str, Any],
        repo_paths: dict[str, str | Path] | None = None,
        alpha: float = 1.4,
        pagerank_damping: float = 0.85,
    ) -> None:
        self.alpha = float(alpha)
        self.pagerank_damping = float(pagerank_damping)
        self.repo_paths: dict[str, Path] = {
            k: Path(v).expanduser().resolve() for k, v in (repo_paths or {}).items()
        }
        self._lock = threading.RLock()
        self._source_path: Path | None = None

        self.nodes: dict[str, dict[str, Any]] = {}
        self.out_edges: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.u_adj: dict[str, list[tuple[str, float, str]]] = defaultdict(list)
        self.raw_degree: dict[str, int] = defaultdict(int)
        self.node_list: list[str] = []
        self.node_to_idx: dict[str, int] = {}
        self.comp_id: dict[str, int] = {}
        self.components: list[set[str]] = []

        self._ppr_src: Any = None
        self._ppr_dst: Any = None
        self._ppr_prob: Any = None
        self._ppr_dang: Any = None
        self._ppr_in_links: list[list[tuple[int, float]]] = []
        self._ppr_dangling: list[int] = []

        if isinstance(graph_data, (str, Path)):
            self._source_path = Path(graph_data).expanduser()
            raw = self._load_json(self._source_path)
        else:
            raw = graph_data
        self._build_index(raw)

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {"nodes": [], "links": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"nodes": [], "links": []}
        except Exception:
            return {"nodes": [], "links": []}

    def _penalized_weight(self, u: str, v: str, base_w: float, alpha: float) -> float:
        """w'(u, v) = w(u, v) * (1 + alpha * ln(1 + deg(v))), zero hub penalty on domain_invariant."""
        if _is_invariant(self.nodes[v]):
            return base_w
        deg_v = self.raw_degree.get(v, 0)
        return base_w * (1.0 + alpha * math.log1p(deg_v))

    def _symmetric_penalized_weight(self, u: str, v: str, base_w: float, alpha: float) -> float:
        u_inv = _is_invariant(self.nodes[u])
        v_inv = _is_invariant(self.nodes[v])
        if u_inv and v_inv:
            return base_w
        deg_u = 0 if u_inv else self.raw_degree.get(u, 0)
        deg_v = 0 if v_inv else self.raw_degree.get(v, 0)
        return base_w * (1.0 + alpha * math.log1p(max(deg_u, deg_v)))

    def _build_index(self, raw: dict[str, Any]) -> None:
        raw_nodes = raw.get("nodes", [])
        raw_links = raw.get("links", raw.get("edges", []))

        nodes: dict[str, dict[str, Any]] = {}
        for item in raw_nodes:
            if not isinstance(item, dict):
                continue
            nid = str(item.get("id", "")).strip()
            if not nid:
                continue
            attr = dict(item)
            attr.setdefault("label", nid)
            attr.setdefault("repo", str(attr.get("project_id") or (nid.split("::")[0] if "::" in nid else "default")))
            attr.setdefault("project_id", attr["repo"])
            attr.setdefault("kind", attr.get("type", "code"))
            attr["_invariant"] = _is_invariant(attr)
            attr["_callable"] = bool(attr.get("_callable", attr.get("callable", False)))
            nodes[nid] = attr

        raw_deg: dict[str, int] = defaultdict(int)
        valid_links: list[tuple[str, str, str, float]] = []
        for link in raw_links:
            if not isinstance(link, dict):
                continue
            u = str(link.get("source", "")).strip()
            v = str(link.get("target", "")).strip()
            if not u or not v or u == v or u not in nodes or v not in nodes:
                continue
            rel = str(link.get("relation", link.get("label", "references"))).lower()
            raw_w = float(link.get("weight", 1.0) or 1.0)
            c_rel = RELATION_BASE_WEIGHT.get(rel, DEFAULT_EDGE_WEIGHT)
            base_w = c_rel / max(raw_w, 0.1)
            valid_links.append((u, v, rel, base_w))
            raw_deg[u] += 1
            raw_deg[v] += 1

        with self._lock:
            self.nodes = nodes
            self.raw_degree = raw_deg
            self.out_edges = defaultdict(list)
            self.u_adj = defaultdict(list)

            undir_best: dict[tuple[str, str], tuple[float, str, float]] = {}
            for u, v, rel, base_w in valid_links:
                dir_w = self._penalized_weight(u, v, base_w, self.alpha)
                sym_w = self._symmetric_penalized_weight(u, v, base_w, self.alpha)
                aff = 1.0 / max(dir_w, 1e-4)
                self.out_edges[u].append({
                    "source": u,
                    "target": v,
                    "relation": rel,
                    "weight": dir_w,
                    "base_weight": base_w,
                    "affinity": aff,
                })
                pair = (u, v) if u < v else (v, u)
                if pair not in undir_best or sym_w < undir_best[pair][0]:
                    undir_best[pair] = (sym_w, rel, base_w)

            self._undir_base: dict[tuple[str, str], float] = {}
            for (u, v), (sym_w, rel, base_w) in undir_best.items():
                self.u_adj[u].append((v, sym_w, rel))
                self.u_adj[v].append((u, sym_w, rel))
                self._undir_base[(u, v)] = base_w

            self.node_list = sorted(nodes.keys())
            self.node_to_idx = {nid: idx for idx, nid in enumerate(self.node_list)}

            visited: set[str] = set()
            self.components = []
            self.comp_id = {}
            for nid in self.node_list:
                if nid in visited:
                    continue
                comp: set[str] = set()
                dq = deque([nid])
                visited.add(nid)
                while dq:
                    cur = dq.popleft()
                    comp.add(cur)
                    self.comp_id[cur] = len(self.components)
                    for nb, _, _ in self.u_adj.get(cur, ()):
                        if nb not in visited:
                            visited.add(nb)
                            dq.append(nb)
                self.components.append(comp)

            self._build_ppr_index()

    def _build_ppr_index(self) -> None:
        n = len(self.node_list)
        in_links: list[list[tuple[int, float]]] = [[] for _ in range(n)]
        dangling: list[int] = []
        srcs: list[int] = []
        dsts: list[int] = []
        probs: list[float] = []

        for u_idx, u in enumerate(self.node_list):
            neighbors: list[tuple[int, float]] = []
            for edge in self.out_edges.get(u, ()):
                v_idx = self.node_to_idx.get(edge["target"])
                if v_idx is not None:
                    neighbors.append((v_idx, float(edge["affinity"])))
            for nb, sym_w, _ in self.u_adj.get(u, ()):
                v_idx = self.node_to_idx.get(nb)
                if v_idx is not None:
                    neighbors.append((v_idx, 0.5 / max(sym_w, 1e-4)))

            if not neighbors:
                dangling.append(u_idx)
                continue

            merged: dict[int, float] = defaultdict(float)
            for v_idx, w in neighbors:
                merged[v_idx] += w
            total_w = sum(merged.values())
            if total_w <= 0.0:
                dangling.append(u_idx)
                continue
            inv_w = 1.0 / total_w
            for v_idx, w in merged.items():
                p = w * inv_w
                in_links[v_idx].append((u_idx, p))
                srcs.append(u_idx)
                dsts.append(v_idx)
                probs.append(p)

        self._ppr_in_links = in_links
        self._ppr_dangling = dangling
        if np is not None and n > 0:
            self._ppr_src = np.array(srcs, dtype=np.int32)
            self._ppr_dst = np.array(dsts, dtype=np.int32)
            self._ppr_prob = np.array(probs, dtype=np.float64)
            self._ppr_dang = np.array(dangling, dtype=np.int32)
        else:
            self._ppr_src = None
            self._ppr_dst = None
            self._ppr_prob = None
            self._ppr_dang = None

    def compute_ppr(
        self,
        seed_set: set[str],
        backbone_set: set[str] | None = None,
        damping: float | None = None,
        max_iter: int = 60,
        tol: float = 1e-6,
    ) -> dict[str, float]:
        """Vectorized NumPy sparse power-iteration Personalized PageRank."""
        n = len(self.node_list)
        if n == 0:
            return {}
        alpha_d = float(damping if damping is not None else self.pagerank_damping)
        one_minus_d = 1.0 - alpha_d
        bb = backbone_set or set()

        if np is not None and self._ppr_src is not None:
            p_vec = np.zeros(n, dtype=np.float64)
            for nid in seed_set | bb:
                idx = self.node_to_idx.get(nid)
                if idx is not None:
                    p_vec[idx] = 3.0 if nid in seed_set else 1.5
            p_sum = float(p_vec.sum())
            p_vec = (p_vec / p_sum) if p_sum > 0.0 else np.full(n, 1.0 / n, dtype=np.float64)

            x = p_vec.copy()
            src_arr = self._ppr_src
            dst_arr = self._ppr_dst
            prob_arr = self._ppr_prob
            dang_arr = self._ppr_dang

            for _ in range(max_iter):
                dangle_mass = alpha_d * float(x[dang_arr].sum()) if dang_arr.size else 0.0
                trans = (
                    np.bincount(dst_arr, weights=x[src_arr] * prob_arr, minlength=n)
                    if src_arr.size
                    else np.zeros(n, dtype=np.float64)
                )
                x_next = (one_minus_d + dangle_mass) * p_vec + alpha_d * trans
                if float(np.abs(x_next - x).sum()) < tol:
                    x = x_next
                    break
                x = x_next
            return {self.node_list[i]: float(x[i]) for i in np.flatnonzero(x > 0.0)}

        p_raw = [
            3.0 if nid in seed_set else (1.5 if nid in bb else 0.0)
            for nid in self.node_list
        ]
        p_sum = sum(p_raw)
        p_norm = [v / p_sum for v in p_raw] if p_sum > 0.0 else [1.0 / n] * n
        x_py = list(p_norm)
        for _ in range(max_iter):
            dangle_mass = alpha_d * sum(x_py[i] for i in self._ppr_dangling) if self._ppr_dangling else 0.0
            t_scale = one_minus_d + dangle_mass
            x_next_py = [0.0] * n
            err = 0.0
            for j in range(n):
                acc = sum(x_py[src_i] * prob for src_i, prob in self._ppr_in_links[j])
                val = t_scale * p_norm[j] + alpha_d * acc
                x_next_py[j] = val
                err += abs(val - x_py[j])
            x_py = x_next_py
            if err < tol:
                break
        return {self.node_list[i]: x_py[i] for i in range(n) if x_py[i] > 0.0}

    def _edge_weight_for_alpha(self, u: str, v: str, alpha: float) -> float:
        pair = (u, v) if u < v else (v, u)
        base_w = self._undir_base.get(pair, DEFAULT_EDGE_WEIGHT)
        return self._symmetric_penalized_weight(u, v, base_w, alpha)

    def compute_terminal_steiner_tree(
        self,
        terminals: list[str],
        alpha: float | None = None,
    ) -> tuple[set[str], set[tuple[str, str]]]:
        """
        Hub-Penalized Kou Terminal-Steiner-Tree:
        1. Full pairwise shortest-path distance matrix over resolved terminal seeds via Dijkstra
        2. Kruskal MST over the metric closure
        3. Induced subgraph reconstruction
        4. Second MST over the induced subgraph
        5. Iterative non-terminal leaf pruning
        """
        eff_alpha = self.alpha if alpha is None else float(alpha)
        unique_terms = [t for t in dict.fromkeys(terminals) if t in self.nodes]
        if not unique_terms:
            return set(), set()
        if len(unique_terms) == 1:
            return {unique_terms[0]}, set()

        # 1. Full pairwise shortest-path distance matrix via Dijkstra
        dist_matrix: dict[tuple[str, str], float] = {}
        pair_paths: dict[tuple[str, str], list[str]] = {}
        closure_edges: list[tuple[float, str, str]] = []

        for idx, src in enumerate(unique_terms):
            dist: dict[str, float] = {src: 0.0}
            parent: dict[str, str] = {}
            pq: list[tuple[float, str]] = [(0.0, src)]
            settled: set[str] = set()
            targets = set(unique_terms) - {src}

            while pq and targets:
                d_u, u = heapq.heappop(pq)
                if u in settled:
                    continue
                settled.add(u)
                if u in targets:
                    targets.remove(u)
                    if not targets:
                        break
                for v, stored_w, _rel in self.u_adj.get(u, ()):
                    if v in settled:
                        continue
                    w_uv = stored_w if abs(eff_alpha - self.alpha) < 1e-9 else self._edge_weight_for_alpha(u, v, eff_alpha)
                    nd = d_u + w_uv
                    if nd < dist.get(v, math.inf):
                        dist[v] = nd
                        parent[v] = u
                        heapq.heappush(pq, (nd, v))

            for dst in unique_terms[idx + 1 :]:
                if dst in dist:
                    d_sd = dist[dst]
                    dist_matrix[(src, dst)] = d_sd
                    dist_matrix[(dst, src)] = d_sd
                    path = [dst]
                    cur = dst
                    while cur != src:
                        cur = parent[cur]
                        path.append(cur)
                    path.reverse()
                    pair_paths[(src, dst)] = path
                    closure_edges.append((d_sd, src, dst))

        if not closure_edges:
            return set(unique_terms), set()

        # 2. Kruskal MST over metric closure
        closure_edges.sort(key=lambda item: (item[0], item[1], item[2]))
        uf = {t: t for t in unique_terms}

        def _find(x: str) -> str:
            while uf[x] != x:
                uf[x] = uf[uf[x]]
                x = uf[x]
            return x

        induced_nodes: set[str] = set(unique_terms)
        for _cost, u_t, v_t in closure_edges:
            ru, rv = _find(u_t), _find(v_t)
            if ru != rv:
                uf[ru] = rv
                induced_nodes.update(pair_paths[(u_t, v_t)])

        # 3. Induced subgraph reconstruction
        induced_edges: list[tuple[float, str, str]] = []
        seen_pairs: set[tuple[str, str]] = set()
        for u in induced_nodes:
            for v, stored_w, _rel in self.u_adj.get(u, ()):
                if v in induced_nodes and u < v:
                    pair = (u, v)
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        w_uv = (
                            stored_w
                            if abs(eff_alpha - self.alpha) < 1e-9
                            else self._edge_weight_for_alpha(u, v, eff_alpha)
                        )
                        induced_edges.append((w_uv, u, v))

        # 4. Second MST over induced subgraph
        induced_edges.sort(key=lambda item: (item[0], item[1], item[2]))
        uf_sub = {n: n for n in induced_nodes}

        def _find_sub(x: str) -> str:
            while uf_sub[x] != x:
                uf_sub[x] = uf_sub[uf_sub[x]]
                x = uf_sub[x]
            return x

        tree_adj: dict[str, set[str]] = defaultdict(set)
        for _w, u, v in induced_edges:
            ru, rv = _find_sub(u), _find_sub(v)
            if ru != rv:
                uf_sub[ru] = rv
                tree_adj[u].add(v)
                tree_adj[v].add(u)

        # 5. Iterative non-terminal leaf pruning
        term_set = set(unique_terms)
        leaf_queue = deque(n for n, nbs in tree_adj.items() if len(nbs) == 1 and n not in term_set)
        while leaf_queue:
            leaf = leaf_queue.popleft()
            if leaf in term_set or len(tree_adj.get(leaf, ())) != 1:
                continue
            nb = next(iter(tree_adj[leaf]))
            tree_adj[nb].discard(leaf)
            del tree_adj[leaf]
            if nb not in term_set and len(tree_adj.get(nb, ())) == 1:
                leaf_queue.append(nb)

        final_nodes = set(term_set) | set(tree_adj.keys())
        final_edges: set[tuple[str, str]] = set()
        for u, nbs in tree_adj.items():
            for v in nbs:
                if u < v:
                    final_edges.add((u, v))
        return final_nodes, final_edges

    def resolve_seeds(self, seeds: list[str], max_per_seed: int = 2) -> list[str]:
        resolved: list[str] = []
        seen: set[str] = set()

        def _add(nid: str) -> None:
            if nid not in seen and nid in self.nodes:
                seen.add(nid)
                resolved.append(nid)

        for raw_seed in seeds:
            q = str(raw_seed).strip()
            if not q:
                continue
            if q in self.nodes:
                _add(q)
                continue

            ql = q.lower()
            tokens = [t for t in re.split(r"[\s:/._-]+", ql) if t]
            scored: list[tuple[float, str]] = []
            for nid, attr in self.nodes.items():
                label = str(attr.get("label", "")).lower()
                sf = str(attr.get("source_file", "")).lower()
                repo = str(attr.get("repo", "")).lower()
                nl = nid.lower()

                score = 0.0
                if nl == ql or label == ql or label.rstrip("()") == ql:
                    score = 100.0
                elif ql in label:
                    score = 72.0
                elif ql in sf:
                    score = 62.0
                elif ql == repo:
                    score = 52.0 if attr.get("kind") == "file" else 42.0
                elif ql in nl:
                    score = 45.0
                elif tokens:
                    hay = f"{nl} {label} {sf} {repo}"
                    hits = sum(1 for tok in tokens if tok in hay)
                    if hits == len(tokens):
                        score = 38.0
                    elif hits > 0:
                        score = 12.0 * (hits / len(tokens))

                if score > 0.0:
                    if attr.get("_invariant"):
                        score += 6.0
                    if attr.get("_callable"):
                        score += 2.0
                    score += min(self.raw_degree.get(nid, 0) * 0.1, 3.0)
                    scored.append((score, nid))

            scored.sort(key=lambda item: (-item[0], item[1]))
            for _sc, nid in scored[:max_per_seed]:
                _add(nid)

        if not resolved:
            raise SeedResolutionError(
                f"No graph nodes matched seeds {seeds} (total nodes: {len(self.nodes)})."
            )
        return resolved

    def _read_snippet(self, nid: str, max_lines: int = 24) -> str:
        attr = self.nodes[nid]
        if _is_invariant(attr):
            return str(attr.get("summary", "")).strip()

        repo = str(attr.get("repo", ""))
        rel_file = str(attr.get("source_file", "")).lstrip("/\\")
        repo_root = self.repo_paths.get(repo)
        if not repo_root or not rel_file:
            return ""
        try:
            target = (repo_root / rel_file).resolve()
            if not target.is_relative_to(repo_root) or not target.is_file():
                return ""
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        if not lines:
            return ""

        start = int(attr.get("start_line") or 0)
        end = int(attr.get("end_line") or 0)
        if start <= 0:
            loc = str(attr.get("source_location", ""))
            m = re.findall(r"L?(\d+)", loc)
            start = int(m[0]) if m else 1
            end = int(m[1]) if len(m) > 1 else start + max_lines - 1

        idx_start = max(0, min(start - 1, len(lines) - 1))
        idx_end = min(len(lines), max(idx_start + 1, min(end, idx_start + max_lines)))
        return "\n".join(lines[idx_start:idx_end]).rstrip()

    def _format_node_block(self, nid: str, ppr: float, is_seed: bool, snippet: str) -> str:
        attr = self.nodes[nid]
        sf = str(attr.get("source_file", ""))
        loc = str(attr.get("source_location", ""))
        file_ref = f"{attr.get('repo', 'default')}/{sf}:{loc}" if loc else f"{attr.get('repo', 'default')}/{sf}"
        role = "seed" if is_seed else ("invariant" if attr.get("_invariant") else "backbone")
        header = (
            f'<node id="{html.escape(nid)}" label="{html.escape(str(attr.get("label", nid)))}" '
            f'role="{role}" file="{html.escape(file_ref)}" kind="{html.escape(str(attr.get("kind", "code")))}" '
            f'ppr="{ppr:.4f}">'
        )
        if snippet:
            return f"{header}\n{html.escape(snippet)}\n</node>"
        return f"{header}</node>"

    def extract_subgraph(
        self,
        seeds: list[str],
        max_tokens: int = 3500,
        alpha: float | None = None,
    ) -> dict[str, Any]:
        """Extract a token-budgeted subgraph via Kou Steiner Tree + PPR greedy packing."""
        with self._lock:
            eff_alpha = self.alpha if alpha is None else float(alpha)
            max_tokens = max(200, min(int(max_tokens), 32000))
            seed_ids = self.resolve_seeds(seeds)
            seed_set = set(seed_ids)

            comp_groups: dict[int, list[str]] = defaultdict(list)
            for sid in seed_ids:
                cid = self.comp_id.get(sid, -1)
                comp_groups[cid].append(sid)

            disjoint = len(comp_groups) > 1
            backbone_nodes: set[str] = set(seed_ids)
            backbone_edges: set[tuple[str, str]] = set()

            for group in comp_groups.values():
                if len(group) >= 2:
                    st_nodes, st_edges = self.compute_terminal_steiner_tree(group, alpha=eff_alpha)
                    backbone_nodes.update(st_nodes)
                    backbone_edges.update(st_edges)
                elif len(group) == 1:
                    solo = group[0]
                    nbs = sorted(self.u_adj.get(solo, ()), key=lambda item: item[1])[:2]
                    for nb, _w, _rel in nbs:
                        backbone_nodes.add(nb)
                        backbone_edges.add((solo, nb) if solo < nb else (nb, solo))

            ppr_scores = self.compute_ppr(seed_set=seed_set, backbone_set=backbone_nodes)

            ordered_backbone = sorted(
                backbone_nodes,
                key=lambda nid: (
                    nid not in seed_set,
                    not self.nodes[nid].get("_invariant", False),
                    -ppr_scores.get(nid, 0.0),
                    nid,
                ),
            )

            candidate_neighbors: set[str] = set()
            for bnode in backbone_nodes:
                for nb, _w, _rel in self.u_adj.get(bnode, ()):
                    if nb not in backbone_nodes:
                        candidate_neighbors.add(nb)

            ranked_neighbors = sorted(
                candidate_neighbors,
                key=lambda nid: (
                    not self.nodes[nid].get("_invariant", False),
                    -ppr_scores.get(nid, 0.0),
                    nid,
                ),
            )

            used_tokens = 96
            tier1_entries: list[dict[str, Any]] = []
            tier2_entries: list[dict[str, Any]] = []
            tier3_edges: list[dict[str, Any]] = []
            included_nodes: set[str] = set()

            for nid in ordered_backbone:
                is_seed = nid in seed_set
                ppr = ppr_scores.get(nid, 0.0)
                snippet = self._read_snippet(nid)
                block = self._format_node_block(nid, ppr, is_seed, snippet)
                est = _estimate_tokens(block)

                if used_tokens + est > max_tokens and snippet:
                    remaining_chars = max(0, int((max_tokens - used_tokens - 30) * 3.5))
                    if remaining_chars > 60:
                        trimmed = snippet[:remaining_chars].rstrip() + "\n..."
                        block = self._format_node_block(nid, ppr, is_seed, trimmed)
                        est = _estimate_tokens(block)
                    else:
                        block = self._format_node_block(nid, ppr, is_seed, "")
                        est = _estimate_tokens(block)

                if used_tokens + est <= max_tokens:
                    attr = self.nodes[nid]
                    tier1_entries.append({
                        "id": nid,
                        "label": attr.get("label", nid),
                        "repo": attr.get("repo", ""),
                        "file": f"{attr.get('repo', '')}/{attr.get('source_file', '')}",
                        "kind": attr.get("kind", "code"),
                        "is_seed": is_seed,
                        "is_invariant": bool(attr.get("_invariant")),
                        "ppr": round(ppr, 5),
                        "tokens": est,
                        "rendered_block": block,
                    })
                    included_nodes.add(nid)
                    used_tokens += est

            tier2_cap = int(max_tokens * 0.90)
            for nid in ranked_neighbors:
                if nid in included_nodes:
                    continue
                attr = self.nodes[nid]
                ppr = ppr_scores.get(nid, 0.0)
                sf = str(attr.get("source_file", ""))
                loc = str(attr.get("source_location", ""))
                line_txt = html.escape(
                    f"- {nid} | label={attr.get('label', nid)} | loc={sf}:{loc} | kind={attr.get('kind', 'code')} | ppr={ppr:.4f}"
                )
                est = _estimate_tokens(line_txt)
                if used_tokens + est <= min(tier2_cap, max_tokens):
                    tier2_entries.append({
                        "id": nid,
                        "label": attr.get("label", nid),
                        "repo": attr.get("repo", ""),
                        "file": f"{attr.get('repo', '')}/{sf}",
                        "kind": attr.get("kind", "code"),
                        "ppr": round(ppr, 5),
                        "tokens": est,
                        "rendered_line": line_txt,
                    })
                    included_nodes.add(nid)
                    used_tokens += est
                else:
                    break

            seen_topo: set[tuple[str, str, str]] = set()
            for u in sorted(included_nodes):
                for edge in self.out_edges.get(u, ()):
                    v = edge["target"]
                    if v not in included_nodes:
                        continue
                    rel = edge["relation"]
                    sig = (u, v, rel)
                    if sig in seen_topo:
                        continue
                    seen_topo.add(sig)
                    undir = (u, v) if u < v else (v, u)
                    is_bb = undir in backbone_edges
                    edge_str = f"{u} --[{rel}]--> {v}"
                    est = _estimate_tokens(edge_str)
                    if used_tokens + est <= max_tokens:
                        tier3_edges.append({
                            "source": u,
                            "target": v,
                            "relation": rel,
                            "is_backbone": is_bb,
                            "rendered_edge": edge_str,
                        })
                        used_tokens += est

            tier3_edges.sort(key=lambda e: (not e["is_backbone"], e["source"], e["target"]))
            repos = sorted({self.nodes[nid].get("repo", "default") for nid in included_nodes})

            return {
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "project": repos[0] if len(repos) == 1 else "global",
                "alpha": eff_alpha,
                "requested_seeds": seeds,
                "resolved_seeds": seed_ids,
                "disjoint_components": disjoint,
                "budget_tokens": max_tokens,
                "actual_tokens": min(used_tokens, max_tokens),
                "tier1_backbone": tier1_entries,
                "tier2_neighbors": tier2_entries,
                "tier3_topology": tier3_edges,
                "stats": {
                    "total_graph_nodes": len(self.nodes),
                    "backbone_nodes": len(backbone_nodes),
                    "packed_tier1": len(tier1_entries),
                    "packed_tier2": len(tier2_entries),
                    "packed_edges": len(tier3_edges),
                    "compression_ratio": round(len(included_nodes) / max(1, len(self.nodes)), 4),
                },
            }

    def format_xml_pack(
        self,
        subgraph_result: dict[str, Any],
        project_id: str | None = None,
    ) -> str:
        """Render the extracted subgraph into `<xml_pack project="..." actual_tokens="..." budget_tokens="...">`."""
        res = subgraph_result
        proj = project_id or res.get("project") or "global"
        lines: list[str] = [
            f'<xml_pack project="{html.escape(str(proj))}" '
            f'actual_tokens="{int(res["actual_tokens"])}" '
            f'budget_tokens="{int(res["budget_tokens"])}" '
            f'alpha="{res.get("alpha", self.alpha)}" '
            f'disjoint="{str(res.get("disjoint_components", False)).lower()}">'
        ]

        lines.append("  <seeds>")
        for sid in res.get("resolved_seeds", []):
            lines.append(f'    <seed id="{html.escape(str(sid))}" />')
        lines.append("  </seeds>")

        lines.append("  <tier1_backbone>")
        for item in res.get("tier1_backbone", []):
            for line in str(item.get("rendered_block", "")).splitlines():
                lines.append(f"    {line}")
        lines.append("  </tier1_backbone>")

        if res.get("tier2_neighbors"):
            lines.append("  <tier2_ppr_neighbors>")
            for item in res["tier2_neighbors"]:
                lines.append(f"    {item['rendered_line']}")
            lines.append("  </tier2_ppr_neighbors>")

        if res.get("tier3_topology"):
            lines.append("  <topology_edges>")
            for edge in res["tier3_topology"]:
                tag = "BACKBONE" if edge.get("is_backbone") else "CONTEXT"
                lines.append(f"    [{tag}] {html.escape(str(edge['rendered_edge']))}")
            lines.append("  </topology_edges>")

        lines.append("</xml_pack>")
        return "\n".join(lines)

    def search(self, query: str, limit: int = 15) -> list[dict[str, Any]]:
        """Compact symbol & domain invariant lookup (kept strictly under 1 KB payload)."""
        q = query.strip().lower()
        limit = max(1, min(int(limit), 15))
        scored: list[tuple[float, dict[str, Any]]] = []

        for nid, attr in self.nodes.items():
            label = str(attr.get("label", nid))
            sf = str(attr.get("source_file", ""))
            kind = str(attr.get("kind", "code"))
            repo = str(attr.get("repo", ""))

            if not q:
                sc = 5.0 if attr.get("_invariant") else (2.0 if attr.get("_callable") else 1.0)
            else:
                ll = label.lower()
                nl = nid.lower()
                sfl = sf.lower()
                if ll == q or ll.rstrip("()") == q:
                    sc = 100.0
                elif q in ll:
                    sc = 70.0
                elif q in sfl:
                    sc = 45.0
                elif q in nl:
                    sc = 30.0
                else:
                    continue
                if attr.get("_invariant"):
                    sc += 10.0

            scored.append((
                sc + min(self.raw_degree.get(nid, 0) * 0.05, 2.0),
                {
                    "id": nid,
                    "label": label[:42],
                    "kind": kind[:16],
                    "repo": repo[:24],
                    "loc": str(attr.get("source_location", ""))[:16],
                },
            ))

        scored.sort(key=lambda item: (-item[0], item[1]["id"]))
        results: list[dict[str, Any]] = []
        byte_budget = 880
        used_bytes = 2
        for _sc, entry in scored[:limit]:
            chunk = json.dumps(entry, separators=(",", ":"))
            if used_bytes + len(chunk) + 1 > byte_budget:
                break
            results.append(entry)
            used_bytes += len(chunk) + 1
        return results

    def build_cockpit_elk_graph(
        self,
        project_id: str | None = None,
        level: int = 1,
        highlight_nodes: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Build L0-L3 hierarchical ELK JSON topology for the Spatial Cockpit."""
        with self._lock:
            lvl = max(0, min(int(level), 3))
            hl_set = set(highlight_nodes or ())

            repo_nodes: dict[str, list[str]] = defaultdict(list)
            repo_meta: dict[str, dict[str, Any]] = {}
            for nid, attr in self.nodes.items():
                repo = str(attr.get("repo", "default"))
                repo_nodes[repo].append(nid)
                if repo not in repo_meta:
                    repo_meta[repo] = {
                        "host": str(attr.get("host", "workspace")),
                        "group": str(attr.get("group", "services")),
                    }

            if project_id and project_id in repo_nodes:
                active_repos = [project_id] if lvl >= 3 else sorted(repo_nodes.keys())
            else:
                active_repos = sorted(repo_nodes.keys())

            drill_repo = (
                project_id
                if (project_id and project_id in repo_nodes and lvl >= 2)
                else (active_repos[0] if (lvl >= 3 and active_repos) else None)
            )

            hierarchy: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
            for repo in active_repos:
                meta = repo_meta[repo]
                hierarchy[meta["host"]][meta["group"]].append(repo)

            l0_children: list[dict[str, Any]] = []
            l3_included: set[str] = set()
            elk_edges: list[dict[str, Any]] = []

            for host_name in sorted(hierarchy.keys()):
                groups = hierarchy[host_name]
                l1_children: list[dict[str, Any]] = []

                if lvl > 0:
                    for group_name in sorted(groups.keys()):
                        repos = sorted(groups[group_name])
                        l2_children: list[dict[str, Any]] = []

                        for repo in repos:
                            r_nids = repo_nodes[repo]
                            file_cnt = sum(1 for n in r_nids if self.nodes[n].get("kind") == "file")
                            fn_cnt = sum(1 for n in r_nids if self.nodes[n].get("_callable"))
                            inv_cnt = sum(1 for n in r_nids if self.nodes[n].get("_invariant"))
                            is_drilled = (repo == drill_repo and lvl >= 3) or (
                                lvl >= 3 and project_id is None and repo == active_repos[0]
                            )

                            l3_children: list[dict[str, Any]] = []
                            if is_drilled:
                                ranked = sorted(
                                    r_nids,
                                    key=lambda n: (
                                        n not in hl_set,
                                        not self.nodes[n].get("_invariant", False),
                                        not self.nodes[n].get("_callable", False),
                                        -self.raw_degree.get(n, 0),
                                        n,
                                    ),
                                )[:24]
                                for nid in ranked:
                                    attr = self.nodes[nid]
                                    l3_included.add(nid)
                                    sf = str(attr.get("source_file", ""))
                                    loc = str(attr.get("source_location", ""))
                                    sub = f"{sf}:{loc}" if loc else sf
                                    l3_children.append({
                                        "id": nid,
                                        "width": 216,
                                        "height": 54,
                                        "properties": {
                                            "tier": "L3",
                                            "repo": repo,
                                            "label": str(attr.get("label", nid)),
                                            "sub": sub,
                                            "kind": str(attr.get("kind", "code")),
                                            "callable": bool(attr.get("_callable")),
                                            "invariant": bool(attr.get("_invariant")),
                                            "highlighted": nid in hl_set,
                                        },
                                    })

                            repo_hl = any(n in hl_set for n in r_nids) or f"repo::{repo}" in hl_set
                            l2_node: dict[str, Any] = {
                                "id": f"repo::{repo}",
                                "properties": {
                                    "tier": "L2",
                                    "repo": repo,
                                    "label": repo,
                                    "sub": f"{file_cnt} files | {fn_cnt} fn | {inv_cnt} inv | {len(r_nids)} nodes",
                                    "drilled": is_drilled,
                                    "highlighted": repo_hl,
                                },
                                "children": l3_children,
                            }
                            if is_drilled:
                                l2_node["layoutOptions"] = {
                                    "elk.padding": "[top=44,left=18,bottom=18,right=18]",
                                    "elk.algorithm": "layered",
                                    "elk.direction": "RIGHT",
                                    "elk.spacing.nodeNode": "18",
                                    "elk.layered.spacing.nodeNodeBetweenLayers": "36",
                                }
                            else:
                                l2_node["width"] = 236
                                l2_node["height"] = 78
                            l2_children.append(l2_node)

                        l1_node: dict[str, Any] = {
                            "id": f"group::{host_name}::{group_name}",
                            "properties": {
                                "tier": "L1",
                                "label": group_name,
                                "sub": f"{len(repos)} projects",
                                "highlighted": any(c["properties"]["highlighted"] for c in l2_children),
                            },
                            "children": l2_children,
                            "layoutOptions": {
                                "elk.padding": "[top=40,left=18,bottom=18,right=18]",
                                "elk.algorithm": "layered",
                                "elk.direction": "RIGHT",
                                "elk.spacing.nodeNode": "22",
                                "elk.layered.spacing.nodeNodeBetweenLayers": "42",
                            },
                        }
                        l1_children.append(l1_node)

                host_repos = sum(len(v) for v in groups.values())
                host_nodes = sum(len(repo_nodes[r]) for g_repos in groups.values() for r in g_repos)
                l0_node: dict[str, Any] = {
                    "id": f"host::{host_name}",
                    "properties": {
                        "tier": "L0",
                        "label": host_name,
                        "sub": f"{host_repos} projects | {host_nodes} AST nodes",
                        "highlighted": any(c["properties"]["highlighted"] for c in l1_children),
                    },
                    "children": l1_children,
                }
                if lvl == 0:
                    l0_node["width"] = 260
                    l0_node["height"] = 88
                else:
                    l0_node["layoutOptions"] = {
                        "elk.padding": "[top=46,left=20,bottom=20,right=20]",
                        "elk.algorithm": "layered",
                        "elk.direction": "DOWN",
                        "elk.spacing.nodeNode": "26",
                        "elk.layered.spacing.nodeNodeBetweenLayers": "48",
                    }
                l0_children.append(l0_node)

            seen_elk_edges: set[tuple[str, str, str]] = set()
            for u in sorted(l3_included):
                for edge in self.out_edges.get(u, ()):
                    v = edge["target"]
                    if v in l3_included and u != v:
                        rel = edge["relation"]
                        sig = (u, v, rel)
                        if sig in seen_elk_edges:
                            continue
                        seen_elk_edges.add(sig)
                        elk_edges.append({
                            "id": f"e_l3::{u}->{v}::{rel}",
                            "sources": [u],
                            "targets": [v],
                            "properties": {
                                "kind": "L3_AST",
                                "relation": rel,
                                "highlighted": (u in hl_set and v in hl_set),
                            },
                        })

            if lvl >= 1:
                cross_bundles: dict[tuple[str, str], dict[str, Any]] = {}
                for u, edges in self.out_edges.items():
                    ru = str(self.nodes[u].get("repo", ""))
                    for edge in edges:
                        v = edge["target"]
                        rv = str(self.nodes[v].get("repo", ""))
                        if ru and rv and ru != rv and (ru in active_repos and rv in active_repos):
                            pair = (f"repo::{ru}", f"repo::{rv}")
                            if pair not in cross_bundles:
                                cross_bundles[pair] = {"count": 0, "relations": set()}
                            cross_bundles[pair]["count"] += 1
                            cross_bundles[pair]["relations"].add(edge["relation"])
                for (src_r, dst_r), binfo in sorted(cross_bundles.items()):
                    elk_edges.append({
                        "id": f"e_l2::{src_r}->{dst_r}",
                        "sources": [src_r],
                        "targets": [dst_r],
                        "properties": {
                            "kind": "L2_CROSS_REPO",
                            "relation": ", ".join(sorted(binfo["relations"])[:2]),
                            "weight": binfo["count"],
                            "highlighted": False,
                        },
                    })

            return {
                "id": "root",
                "layoutOptions": {
                    "elk.algorithm": "layered",
                    "elk.direction": "RIGHT",
                    "elk.hierarchyHandling": "INCLUDE_CHILDREN",
                    "elk.edgeRouting": "ORTHOGONAL",
                    "elk.spacing.nodeNode": "48",
                    "elk.layered.spacing.nodeNodeBetweenLayers": "72",
                },
                "children": l0_children,
                "edges": elk_edges,
            }
