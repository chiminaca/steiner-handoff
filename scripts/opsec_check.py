#!/usr/bin/env python3
"""Static OPSEC leak scanner for steiner-handoff.

Scans tracked text files for RFC1918 IPs, local .lan hostnames, user home paths,
cloud/git API tokens, and PEM private keys. Exits 1 on findings, 0 on pass.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "dist",
    "__pycache__",
    "graphify-out",
    ".steiner",
    ".steiner-hub",
    "node_modules",
}

BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".pdf",
    ".zip",
    ".gz",
    ".pyc",
    ".pyo",
    ".so",
    ".dll",
    ".exe",
}

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("RFC1918 192.168.x.x", re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b")),
    ("RFC1918 10.x.x.x", re.compile(r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")),
    (
        "RFC1918 172.16-31.x.x",
        re.compile(r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b"),
    ),
    ("Local .lan hostname", re.compile(r"\b[a-zA-Z0-9-]+\.lan\b")),
    ("POSIX home path", re.compile(r"/home/[a-zA-Z0-9._-]+/")),
    ("Windows user path", re.compile(r"[A-Za-z]:[/\\]Users[/\\][a-zA-Z0-9._-]+[/\\]")),
    ("GitHub classic PAT", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b")),
    ("GitHub fine-grained PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("OpenAI/API sk- key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("AWS Access Key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("PEM private key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
]


def is_binary(path: Path) -> bool:
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return True
    try:
        chunk = path.read_bytes()[:2048]
    except OSError:
        return True
    return b"\x00" in chunk


def scan_repo(root: Path) -> list[tuple[Path, int, str, str]]:
    findings: list[tuple[Path, int, str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if is_binary(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue

        for lineno, line in enumerate(text.splitlines(), start=1):
            for label, regex in PATTERNS:
                match = regex.search(line)
                if match:
                    findings.append((path.relative_to(root), lineno, label, match.group(0)))
    return findings


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    findings = scan_repo(root)
    if findings:
        print(f"OPSEC CHECK FAILED: {len(findings)} potential leak(s) found:", file=sys.stderr)
        for rel_path, lineno, label, sample in findings:
            print(f"  {rel_path}:{lineno} [{label}] -> {sample}", file=sys.stderr)
        return 1
    print("OPSEC CHECK PASSED: 0 leaks found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
