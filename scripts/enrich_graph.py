#!/usr/bin/env python3
"""Phase-0 domain invariant enrichment hook for steiner-handoff."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

INVARIANTS: list[dict[str, Any]] = [
    {
        "id": "invariant::clean_room_zero_leak",
        "label": "Clean-Room Zero-Leak OPSEC Firewall",
        "kind": "domain_invariant",
        "type": "domain_invariant",
        "weight": 10.0,
        "summary": (
            "Enforce clean-room OSS boundaries: zero RFC1918 IPs, local usernames, "
            "tokens, or private repository identifiers in tracked files or global-graph.json."
        ),
        "governs": [
            "scripts/opsec_check.py",
            "hub/app/privacy_guard.py",
            "topology.example.yaml",
            "compiler/build_prompt.py",
        ],
    },
    {
        "id": "invariant::push_steiner_context_packing",
        "label": "Push Kou Steiner Context Packing",
        "kind": "domain_invariant",
        "type": "domain_invariant",
        "weight": 10.0,
        "summary": (
            "Maintain deterministic local Tree-sitter indexing, sparse PageRank, "
            "and Hub-Penalized Kou Steiner subgraph packing within max_tokens=3500 (alpha=1.4)."
        ),
        "governs": [
            "hub/app/ast_extractor.py",
            "hub/app/steiner_packer.py",
            "hub/app/main.py",
        ],
    },
    {
        "id": "invariant::github_pages_microsite_sync",
        "label": "GitHub Pages Microsite & Source Sync",
        "kind": "domain_invariant",
        "type": "domain_invariant",
        "weight": 10.0,
        "summary": (
            "Keep docs/index.html static CSS and embedded REPO_FILES synchronized "
            "with real repository source files via scripts/sync_docs_sources.py."
        ),
        "governs": [
            "docs/index.html",
            "docs/styles.css",
            "scripts/sync_docs_sources.py",
            "README.md",
        ],
    },
]


def _resolve_targets(cli_arg: str | None) -> list[Path]:
    if cli_arg:
        p = Path(cli_arg)
        return [p] if p.exists() else []
    repo_root = Path(__file__).resolve().parent.parent
    candidates = [
        repo_root / "graphify-out" / "graph.json",
        repo_root / ".steiner" / "graph.json",
    ]
    return [p for p in candidates if p.exists()]


def enrich_file(graph_path: Path) -> int:
    data = json.loads(graph_path.read_text(encoding="utf-8"))
    nodes: list[dict[str, Any]] = data.setdefault("nodes", [])
    edge_key = "links" if "links" in data else "edges"
    edges: list[dict[str, Any]] = data.setdefault(edge_key, [])

    existing_node_ids = {n.get("id") for n in nodes if isinstance(n, dict)}
    existing_edges = {
        (e.get("source"), e.get("target"), e.get("relation", e.get("label", "")))
        for e in edges
        if isinstance(e, dict)
    }

    node_by_file: dict[str, list[str]] = {}
    for n in nodes:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id", ""))
        src = str(n.get("source_file", n.get("file", ""))).replace("\\", "/")
        if src:
            node_by_file.setdefault(src, []).append(nid)

    added = 0
    for inv in INVARIANTS:
        if inv["id"] not in existing_node_ids:
            nodes.append(
                {
                    "id": inv["id"],
                    "label": inv["label"],
                    "kind": inv["kind"],
                    "type": inv["type"],
                    "weight": inv["weight"],
                    "summary": inv["summary"],
                    "source_file": "scripts/enrich_graph.py",
                }
            )
            existing_node_ids.add(inv["id"])
            added += 1

        for target_rel in inv["governs"]:
            matched_ids = [
                nid
                for src, nids in node_by_file.items()
                if src == target_rel or src.endswith("/" + target_rel)
                for nid in nids
            ]
            targets = matched_ids or [target_rel]
            for tgt in targets:
                sig = (inv["id"], tgt, "governs")
                if sig not in existing_edges:
                    edges.append(
                        {
                            "source": inv["id"],
                            "target": tgt,
                            "weight": 0.15,
                            "relation": "governs",
                        }
                    )
                    existing_edges.add(sig)

    graph_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return added


def main() -> int:
    cli_arg = sys.argv[1] if len(sys.argv) > 1 else None
    targets = _resolve_targets(cli_arg)
    for target in targets:
        enrich_file(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
