#!/usr/bin/env python3
"""Compile topology.yaml (and an optional agent preset) into Webchat Auditor prompts."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Dict

try:
    import yaml
except ImportError as exc:  # pragma: no cover - runtime environment check
    raise SystemExit(
        "PyYAML is required to run compiler/build_prompt.py. "
        "Install it with: pip install pyyaml"
    ) from exc

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
MASTER_TEMPLATE = TEMPLATES_DIR / "master_compiler.tmpl.md"
COMPACT_TEMPLATE = TEMPLATES_DIR / "compact_booster.tmpl.md"
PLACEHOLDER_RE = re.compile(r"\{\{[A-Z0-9_]+\}\}")


def resolve_input_path(raw_path: str) -> Path:
    """Resolve an input path relative to CWD first, falling back to repository root."""
    candidate = Path(raw_path)
    if candidate.is_absolute() or candidate.exists():
        return candidate
    fallback = ROOT / raw_path
    if fallback.exists():
        return fallback
    return candidate


def load_yaml_file(path: Path, label: str, hint: str | None = None) -> Dict[str, Any]:
    """Load and validate a YAML mapping file."""
    if not path.exists():
        message = f"Missing {label}: '{path}'."
        if hint:
            message = f"{message} {hint}"
        raise FileNotFoundError(message)
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML mapping in {label}: '{path}'.")
    return data


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge override mapping onto a copy of base mapping."""
    merged: Dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def build_context(topology: Dict[str, Any], preset: Dict[str, Any] | None) -> Dict[str, str]:
    """Build flat placeholder mapping for template interpolation."""
    merged = deep_merge(topology, preset) if preset else dict(topology)

    hub = merged.get("hub", {})
    local_inf = merged.get("local_inference", {})
    privacy = merged.get("privacy_firewall", {})
    roles = merged.get("agent_roles", {})
    verification = merged.get("verification", {})
    invocation = merged.get("invocation", {})

    host = str(hub.get("host", "127.0.0.1"))
    port = int(hub.get("port", 8088))
    hub_url = f"http://{host}:{port}"

    patterns_list = privacy.get("tier1_private_patterns", [])
    if isinstance(patterns_list, list):
        patterns_str = ", ".join(str(p) for p in patterns_list) or "none"
    else:
        patterns_str = str(patterns_list)

    preset_name = str(merged.get("preset_name", "default-topology"))
    cli_invoke = str(
        invocation.get("slice_command")
        or roles.get("cli_command")
        or "local-agent-cli --slice"
    )

    return {
        "PRESET_NAME": preset_name,
        "WORKSPACE_ROOT": str(merged.get("workspace_root", "/srv/workspaces")),
        "HUB_HOST": host,
        "HUB_PORT": str(port),
        "HUB_URL": hub_url,
        "MAX_PACK_TOKENS": str(hub.get("max_pack_tokens", 3500)),
        "HUB_PENALTY_ALPHA": str(hub.get("hub_penalty_alpha", 1.4)),
        "PAGERANK_DAMPING": str(hub.get("pagerank_damping", 0.85)),
        "LOCAL_INFERENCE_PROVIDER": str(local_inf.get("provider", "ollama")),
        "LOCAL_INFERENCE_ENDPOINT": str(
            local_inf.get("endpoint", "http://127.0.0.1:11434")
        ),
        "LOCAL_INFERENCE_MODEL": str(local_inf.get("model", "qwen2.5-coder:14b")),
        "ANTI_SWARM_MAX_FILES": str(local_inf.get("anti_swarm_max_files", 2)),
        "ANTI_SWARM_MAX_LOC": str(local_inf.get("anti_swarm_max_loc", 40)),
        "PRIVACY_ISOLATION": str(
            bool(privacy.get("enforce_physical_isolation", True))
        ).lower(),
        "TIER1_PATTERNS": patterns_str,
        "COORDINATOR_ROLE": str(roles.get("coordinator", "frontier-architect")),
        "WORKER_ROLE": str(roles.get("worker_slice", "fast-coder")),
        "AUDITOR_ROLE": str(roles.get("auditor", "reasoning-auditor")),
        "QA_ROLE": str(roles.get("headless_qa", "local-runner")),
        "CLI_INVOKE": cli_invoke,
        "VERIFICATION_MODE": str(verification.get("mode", "single_pass_headless")),
        "REFRESH_ENDPOINT_TEMPLATE": str(
            verification.get("refresh_endpoint_template", "/api/refresh/{project_id}")
        ),
        "OPSEC_SCAN_SCRIPT": str(
            verification.get("opsec_scan_script", "scripts/opsec_check.py")
        ),
    }


def render_template(template_path: Path, context: Dict[str, str]) -> str:
    """Interpolate {{KEY}} placeholders in a template and verify none remain."""
    raw = template_path.read_text(encoding="utf-8")
    rendered = raw
    for key, value in context.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)

    unresolved = sorted(set(PLACEHOLDER_RE.findall(rendered)))
    if unresolved:
        raise ValueError(
            f"Unresolved template placeholders in '{template_path.name}': {', '.join(unresolved)}"
        )
    return rendered


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compile steiner-handoff topology and preset YAML into Webchat Auditor prompts."
    )
    parser.add_argument(
        "--topology",
        default="topology.yaml",
        help="Path to declarative topology YAML (default: topology.yaml).",
    )
    parser.add_argument(
        "--preset",
        default=None,
        help="Optional path to an agent role/invocation preset YAML.",
    )
    parser.add_argument(
        "--out-dir",
        default="dist",
        help="Output directory for compiled Markdown prompts (default: dist).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    topology_path = resolve_input_path(args.topology)
    try:
        topology = load_yaml_file(
            topology_path,
            label="topology file",
            hint="Copy topology.example.yaml to topology.yaml first, or pass --topology <path>.",
        )
        preset: Dict[str, Any] | None = None
        if args.preset:
            preset_path = resolve_input_path(args.preset)
            preset = load_yaml_file(preset_path, label="preset file")
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        print(f"[build_prompt] ERROR: {exc}", file=sys.stderr)
        return 1

    context = build_context(topology, preset)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    master_out = out_dir / "WEBCHAT_SYSTEM_PROMPT.md"
    compact_out = out_dir / "WEBCHAT_COMPACT_BOOSTER.md"

    master_out.write_text(render_template(MASTER_TEMPLATE, context), encoding="utf-8")
    compact_out.write_text(render_template(COMPACT_TEMPLATE, context), encoding="utf-8")

    print(f"[build_prompt] Compiled {master_out} and {compact_out} (preset={context['PRESET_NAME']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
