"""Physical Tier-1 privacy isolation guard for global mesh synchronization."""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


DEFAULT_TIER1_PATTERNS: tuple[str, ...] = (
    "*vault*",
    "*secret*",
    "*.private",
    "private-*",
)


class PrivacyViolationError(PermissionError):
    """Raised when a Tier-1 private project is targeted for global synchronization."""


def load_tier1_patterns(topology_path: str | Path | None = None) -> list[str]:
    """Load `tier1_private_patterns` from a topology YAML file if available."""
    patterns = list(DEFAULT_TIER1_PATTERNS)
    if not topology_path or yaml is None:
        return patterns
    path = Path(topology_path)
    if not path.is_file():
        return patterns
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return patterns
    if not isinstance(data, dict):
        return patterns

    raw_patterns: list[Any] = []
    if isinstance(data.get("tier1_private_patterns"), list):
        raw_patterns.extend(data["tier1_private_patterns"])
    privacy_cfg = data.get("privacy")
    if isinstance(privacy_cfg, dict) and isinstance(privacy_cfg.get("tier1_private_patterns"), list):
        raw_patterns.extend(privacy_cfg["tier1_private_patterns"])

    for item in raw_patterns:
        pat = str(item).strip()
        if pat and pat not in patterns:
            patterns.append(pat)
    return patterns


def _candidate_strings(project: str | dict[str, Any]) -> list[str]:
    if isinstance(project, str):
        p = Path(project)
        return [project, p.name]
    candidates: list[str] = []
    for key in ("id", "project_id", "name", "slug", "path"):
        val = project.get(key)
        if val is not None and str(val).strip():
            sval = str(val).strip()
            candidates.append(sval)
            if "/" in sval or "\\" in sval:
                candidates.append(Path(sval).name)
    return candidates


def is_tier1_private(
    project: str | dict[str, Any],
    patterns: Sequence[str] | None = None,
) -> bool:
    """Return True if a project is flagged `private: true` or matches Tier-1 patterns."""
    if isinstance(project, dict):
        if bool(project.get("private", False)):
            return True
        tier = str(project.get("tier", "")).strip().lower()
        if tier in {"1", "tier1", "tier-1", "private"}:
            return True

    active_patterns = list(patterns) if patterns is not None else list(DEFAULT_TIER1_PATTERNS)
    if not active_patterns:
        return False

    candidates = _candidate_strings(project)
    for cand in candidates:
        cand_lower = cand.lower()
        for pat in active_patterns:
            pat_str = str(pat).strip()
            if not pat_str:
                continue
            if fnmatch(cand_lower, pat_str.lower()):
                return True
    return False


def assert_global_sync_allowed(
    project: str | dict[str, Any],
    patterns: Sequence[str] | None = None,
) -> None:
    """Raise PrivacyViolationError if `project` must never enter the global mesh."""
    if is_tier1_private(project, patterns=patterns):
        label = (
            project.get("id") or project.get("name") or project.get("path") or "unknown"
            if isinstance(project, dict)
            else project
        )
        raise PrivacyViolationError(
            f"Project '{label}' is Tier-1 private and cannot be merged into the global mesh."
        )


def filter_syncable_projects(
    projects: Iterable[dict[str, Any] | str],
    patterns: Sequence[str] | None = None,
) -> list[dict[str, Any] | str]:
    """Return only projects cleared for global mesh synchronization."""
    syncable: list[dict[str, Any] | str] = []
    for proj in projects:
        if not is_tier1_private(proj, patterns=patterns):
            syncable.append(proj)
    return syncable
