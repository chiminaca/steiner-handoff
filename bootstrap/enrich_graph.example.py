#!/usr/bin/env python3
"""Example Phase-0 domain invariant enrichment hook for Steiner AST graphs.

Anchors high-priority ``domain_invariant`` nodes into ``.steiner/graph.json`` or
``graphify-out/graph.json`` with low-weight ``governs`` edges so Kou Steiner
context packing automatically surfaces non-obvious operational rules whenever
governed modules are seeded.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

DEFAULT_CANDIDATES = (
    Path(".steiner/graph.json"),
    Path("graphify-out/graph.json"),
)

DOMAIN_INVARIANTS: List[Dict[str, Any]] = [
    {
        "node": {
            "id": "invariant::symlink_ssot",
            "kind": "domain_invariant",
            "weight": 10.0,
            "summary": (
                "Never copy static asset bundles across build directories; "
                "preserve the symlinked single source of truth path."
            ),
        },
        "targets": (
            "src/assets.py",
            "src/deploy.py",
            "web/manifest.js",
        ),
        "edge_weight": 0.15,
        "relation": "governs",
    },
    {
        "node": {
            "id": "invariant::staging_port_isolation",
            "kind": "domain_invariant",
            "weight": 10.0,
            "summary": (
                "Staging and headless QA servers must bind isolated loopback ports "
                "(127.0.0.1) and never reuse production listener ports."
            ),
        },
        "targets": (
            "src/server.py",
            "config/ports.yaml",
            "scripts/qa_runner.py",
        ),
        "edge_weight": 0.15,
        "relation": "governs",
    },
]


def resolve_graph_path(explicit: str | None) -> Path:
    """Resolve explicit graph path or pick the first existing standard location."""
    if explicit:
        return Path(explicit)
    for candidate in DEFAULT_CANDIDATES:
        if candidate.exists():
            return candidate
    return DEFAULT_CANDIDATES[0]


def enrich(graph_path: Path) -> Dict[str, int]:
    """Idempotently anchor domain_invariant nodes and governing edges into graph JSON."""
    if not graph_path.exists():
        raise FileNotFoundError(
            f"Graph file not found: '{graph_path}'. Run Phase-0 Tree-sitter indexing first."
        )

    data: Dict[str, Any] = json.loads(graph_path.read_text(encoding="utf-8"))
    nodes: List[Dict[str, Any]] = data.setdefault("nodes", [])
    edges: List[Dict[str, Any]] = data.setdefault("edges", [])

    existing_node_ids = {n.get("id") for n in nodes if isinstance(n, dict)}
    existing_edges = {
        (e.get("source"), e.get("target"), e.get("relation"))
        for e in edges
        if isinstance(e, dict)
    }

    added_nodes = 0
    added_edges = 0

    for spec in DOMAIN_INVARIANTS:
        inv_node = spec["node"]
        inv_id = inv_node["id"]
        if inv_id not in existing_node_ids:
            nodes.append(dict(inv_node))
            existing_node_ids.add(inv_id)
            added_nodes += 1

        for target in spec["targets"]:
            edge_key = (inv_id, target, spec["relation"])
            if edge_key not in existing_edges:
                edges.append(
                    {
                        "source": inv_id,
                        "target": target,
                        "weight": spec["edge_weight"],
                        "relation": spec["relation"],
                    }
                )
                existing_edges.add(edge_key)
                added_edges += 1

    graph_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"added_nodes": added_nodes, "added_edges": added_edges}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Anchor domain_invariant nodes into .steiner/graph.json or graphify-out/graph.json."
    )
    parser.add_argument(
        "--graph",
        default=None,
        help="Path to target graph.json (defaults to .steiner/graph.json or graphify-out/graph.json).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    graph_path = resolve_graph_path(args.graph)
    stats = enrich(graph_path)
    print(
        f"[enrich_graph] Updated {graph_path}: "
        f"+{stats['added_nodes']} invariant nodes, +{stats['added_edges']} edges"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
