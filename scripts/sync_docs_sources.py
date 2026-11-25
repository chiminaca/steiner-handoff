#!/usr/bin/env python3
"""Synchronize real repository source files into docs/index.html REPO_FILES."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SOURCE_FILES = [
    "topology.example.yaml",
    "compiler/build_prompt.py",
    "hub/app/steiner_packer.py",
    "hub/app/privacy_guard.py",
    "hub/app/ast_extractor.py",
    "bootstrap/enrich_graph.example.py",
    "scripts/opsec_check.py",
    "LICENSE",
]

START_MARKER = "// <REPO_FILES_START>"
END_MARKER = "// <REPO_FILES_END>"


def build_payload(root: Path) -> dict[str, dict[str, str]]:
    payload: dict[str, dict[str, str]] = {}
    for rel in SOURCE_FILES:
        file_path = root / rel
        if not file_path.is_file():
            continue
        payload[rel] = {
            "content": file_path.read_text(encoding="utf-8"),
        }
    return payload


def sync(root: Path) -> int:
    docs_html = root / "docs" / "index.html"
    if not docs_html.is_file():
        print(f"Missing target: {docs_html}", file=sys.stderr)
        return 1

    html = docs_html.read_text(encoding="utf-8")
    payload = build_payload(root)
    js_block = f"{START_MARKER}\n    const REPO_FILES = {json.dumps(payload, indent=2)};\n    {END_MARKER}"

    marker_pattern = re.compile(
        re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER),
        re.DOTALL,
    )
    if marker_pattern.search(html):
        updated = marker_pattern.sub(lambda _: js_block, html, count=1)
    else:
        fallback_pattern = re.compile(
            r"const\s+REPO_FILES\s*=\s*\{.*?\n\s*\};",
            re.DOTALL,
        )
        if not fallback_pattern.search(html):
            print("Could not locate REPO_FILES block in docs/index.html", file=sys.stderr)
            return 1
        updated = fallback_pattern.sub(lambda _: js_block, html, count=1)

    docs_html.write_text(updated, encoding="utf-8")
    print(f"Synced {len(payload)} source file(s) into docs/index.html")
    return 0


if __name__ == "__main__":
    repo_root = Path(__file__).resolve().parent.parent
    sys.exit(sync(repo_root))
