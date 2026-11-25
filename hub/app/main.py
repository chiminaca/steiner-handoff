"""Steiner Hub FastAPI service: local AST indexing, privacy-isolated sync, Steiner context packing, and L0-L3 Cockpit."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from hub.app.ast_extractor import get_state_dir, get_workspace_root, index_project
from hub.app.privacy_guard import filter_syncable_projects, load_tier1_patterns
from hub.app.steiner_packer import SeedResolutionError, SteinerPacker


UI_DIR = Path(__file__).resolve().parent.parent / "ui"


class IndexRequest(BaseModel):
    path: str = Field(..., description="Project directory path inside STEINER_WORKSPACE_ROOT")
    id: str | None = Field(default=None, description="Optional explicit project identifier")
    name: str | None = Field(default=None, description="Optional human-readable project name")
    private: bool | None = Field(default=None, description="Flag project as Tier-1 private")
    group: str = Field(default="services", description="L1 logical service group")
    host: str = Field(default="workspace", description="L0 workspace host bucket")


class ContextPackRequest(BaseModel):
    project_id: str | None = Field(default=None, description="Scope packing to a specific project ID")
    seeds: list[str] = Field(default_factory=list, description="Seed symbols, files, or node IDs")
    max_tokens: int = Field(default=3500, ge=200, le=32000)
    alpha: float = Field(default=1.4, ge=0.0, le=10.0)


def _registry_path() -> Path:
    return get_state_dir() / "registry.json"


def _global_graph_path() -> Path:
    return get_state_dir() / "global-graph.json"


def _load_registry() -> dict[str, Any]:
    rpath = _registry_path()
    if rpath.is_file():
        try:
            data = json.loads(rpath.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("projects"), dict):
                return data
        except Exception:
            pass
    return {"projects": {}}


def _repo_paths_from_registry(registry: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pid, meta in registry.get("projects", {}).items():
        if isinstance(meta, dict) and meta.get("path"):
            out[pid] = str(meta["path"])
    return out


def _resolve_workspace_target(raw_path: str) -> Path:
    ws_root = get_workspace_root()
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = (ws_root / candidate).resolve()
    else:
        candidate = candidate.resolve()

    if not candidate.is_relative_to(ws_root):
        raise HTTPException(
            status_code=400,
            detail=f"Path must reside under STEINER_WORKSPACE_ROOT ({ws_root}).",
        )
    if not candidate.is_dir():
        raise HTTPException(status_code=404, detail=f"Project directory not found: {candidate}")
    return candidate


def _load_packer(project_id: str | None = None, alpha: float = 1.4) -> SteinerPacker:
    registry = _load_registry()
    projects = registry.get("projects", {})
    repo_paths = _repo_paths_from_registry(registry)

    if project_id:
        meta = projects.get(project_id)
        if not meta:
            raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found.")
        gpath = Path(meta.get("graph_path", get_state_dir() / "projects" / f"{project_id}.json"))
        if not gpath.is_file():
            raise HTTPException(status_code=404, detail=f"Graph JSON missing for '{project_id}'.")
        return SteinerPacker(gpath, repo_paths=repo_paths, alpha=alpha)

    global_path = _global_graph_path()
    if global_path.is_file():
        return SteinerPacker(global_path, repo_paths=repo_paths, alpha=alpha)

    merged_nodes: list[dict[str, Any]] = []
    merged_links: list[dict[str, Any]] = []
    patterns = load_tier1_patterns(get_workspace_root() / "topology.yaml")
    for proj in filter_syncable_projects(projects.values(), patterns=patterns):
        if not isinstance(proj, dict):
            continue
        gpath = Path(str(proj.get("graph_path", "")))
        if gpath.is_file():
            data = json.loads(gpath.read_text(encoding="utf-8"))
            merged_nodes.extend(data.get("nodes", []))
            merged_links.extend(data.get("links", data.get("edges", [])))
    return SteinerPacker({"nodes": merged_nodes, "links": merged_links}, repo_paths=repo_paths, alpha=alpha)


app = FastAPI(
    title="Steiner Hub",
    version="0.1.0",
    description="Clean-room AST Knowledge Mesh & Hub-Penalized Kou Steiner Context Packer",
)

if UI_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(UI_DIR)), name="ui")


@app.get("/")
def serve_index() -> FileResponse:
    index_file = UI_DIR / "index.html"
    if not index_file.is_file():
        raise HTTPException(status_code=404, detail="Cockpit UI index.html not found.")
    return FileResponse(str(index_file))


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    registry = _load_registry()
    projects = registry.get("projects", {})
    global_path = _global_graph_path()
    g_nodes = 0
    g_edges = 0
    if global_path.is_file():
        try:
            gdata = json.loads(global_path.read_text(encoding="utf-8"))
            g_nodes = len(gdata.get("nodes", []))
            g_edges = len(gdata.get("links", gdata.get("edges", [])))
        except Exception:
            pass
    return {
        "ok": True,
        "status": "healthy",
        "workspace_root": str(get_workspace_root()),
        "state_dir": str(get_state_dir()),
        "projects_count": len(projects),
        "global_nodes": g_nodes,
        "global_edges": g_edges,
    }


@app.get("/api/projects")
def api_projects() -> dict[str, Any]:
    registry = _load_registry()
    patterns = load_tier1_patterns(get_workspace_root() / "topology.yaml")
    syncable_ids = {
        p["id"]
        for p in filter_syncable_projects(registry.get("projects", {}).values(), patterns=patterns)
        if isinstance(p, dict) and "id" in p
    }
    items: list[dict[str, Any]] = []
    for pid, meta in sorted(registry.get("projects", {}).items()):
        if not isinstance(meta, dict):
            continue
        entry = dict(meta)
        entry["syncable"] = pid in syncable_ids
        items.append(entry)
    return {"ok": True, "projects": items}


@app.post("/api/index")
def api_index(req: IndexRequest) -> dict[str, Any]:
    target = _resolve_workspace_target(req.path)
    result = index_project(
        repo_path=target,
        project_id=req.id,
        name=req.name,
        private=req.private,
        group=req.group,
        host=req.host,
        state_dir=get_state_dir(),
    )
    return {"ok": True, **result}


@app.post("/api/refresh/{project_id}")
def api_refresh(project_id: str) -> dict[str, Any]:
    registry = _load_registry()
    meta = registry.get("projects", {}).get(project_id)
    if not meta or not isinstance(meta, dict):
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not registered.")

    repo_path = Path(str(meta.get("path", ""))).resolve()
    if not repo_path.is_dir():
        raise HTTPException(status_code=404, detail=f"Project directory missing: {repo_path}")

    result = index_project(
        repo_path=repo_path,
        project_id=project_id,
        name=meta.get("name"),
        private=bool(meta.get("private", False)),
        group=str(meta.get("group", "services")),
        host=str(meta.get("host", "workspace")),
        state_dir=get_state_dir(),
    )
    return {"ok": True, "refreshed": project_id, **result}


@app.post("/api/sync-global")
def api_sync_global() -> dict[str, Any]:
    registry = _load_registry()
    projects = list(registry.get("projects", {}).values())
    patterns = load_tier1_patterns(get_workspace_root() / "topology.yaml")
    syncable = filter_syncable_projects(projects, patterns=patterns)
    syncable_ids = {p["id"] for p in syncable if isinstance(p, dict) and "id" in p}
    blocked_ids = [
        p.get("id", "unknown")
        for p in projects
        if isinstance(p, dict) and p.get("id") not in syncable_ids
    ]

    merged_nodes: list[dict[str, Any]] = []
    merged_links: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()
    seen_edges: set[tuple[str, str, str]] = set()

    for proj in syncable:
        if not isinstance(proj, dict):
            continue
        gpath = Path(str(proj.get("graph_path", "")))
        if not gpath.is_file():
            continue
        data = json.loads(gpath.read_text(encoding="utf-8"))
        for node in data.get("nodes", []):
            if isinstance(node, dict) and node.get("id") and node["id"] not in seen_nodes:
                seen_nodes.add(node["id"])
                merged_nodes.append(node)
        for edge in data.get("links", data.get("edges", [])):
            if not isinstance(edge, dict):
                continue
            sig = (
                str(edge.get("source", "")),
                str(edge.get("target", "")),
                str(edge.get("relation", "links")),
            )
            if sig[0] and sig[1] and sig not in seen_edges:
                seen_edges.add(sig)
                merged_links.append(edge)

    global_payload = {
        "synced_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "included_projects": sorted(syncable_ids),
        "nodes": merged_nodes,
        "links": merged_links,
    }
    gpath = _global_graph_path()
    gpath.parent.mkdir(parents=True, exist_ok=True)
    gpath.write_text(json.dumps(global_payload, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "included_projects": global_payload["included_projects"],
        "blocked_private_count": len(blocked_ids),
        "nodes": len(merged_nodes),
        "edges": len(merged_links),
        "global_graph_path": str(gpath),
    }


@app.get("/api/search")
def api_search(
    q: str = Query(default="", max_length=120),
    limit: int = Query(default=15, ge=1, le=15),
    project_id: str | None = Query(default=None),
) -> JSONResponse:
    packer = _load_packer(project_id=project_id)
    results = packer.search(q, limit=limit)
    payload = {"ok": True, "q": q, "results": results}
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    while len(encoded) >= 1024 and len(results) > 1:
        results.pop()
        payload = {"ok": True, "q": q, "results": results}
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return JSONResponse(content=payload)


@app.post("/api/context/pack")
def api_context_pack(req: ContextPackRequest) -> dict[str, Any]:
    if not req.seeds:
        raise HTTPException(status_code=400, detail="At least one seed must be provided.")
    t0 = time.perf_counter()
    packer = _load_packer(project_id=req.project_id, alpha=req.alpha)
    try:
        subgraph = packer.extract_subgraph(
            seeds=req.seeds,
            max_tokens=req.max_tokens,
            alpha=req.alpha,
        )
    except SeedResolutionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    xml_pack = packer.format_xml_pack(subgraph, project_id=req.project_id or subgraph.get("project"))
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    return {
        "ok": True,
        "elapsed_ms": elapsed_ms,
        "resolved_seeds": subgraph["resolved_seeds"],
        "budget_tokens": subgraph["budget_tokens"],
        "actual_tokens": subgraph["actual_tokens"],
        "stats": subgraph["stats"],
        "xml_pack": xml_pack,
        "subgraph": subgraph,
    }


@app.get("/api/cockpit/topology")
def api_cockpit_topology(
    project_id: str | None = Query(default=None),
    level: int = Query(default=1, ge=0, le=3),
) -> dict[str, Any]:
    packer = _load_packer(project_id=project_id if level >= 3 else None)
    elk_graph = packer.build_cockpit_elk_graph(project_id=project_id, level=level)
    return {
        "ok": True,
        "level": level,
        "project_id": project_id,
        "graph": elk_graph,
    }


@app.get("/api/cockpit/node")
def api_cockpit_node(
    id: str = Query(...),
    project_id: str | None = Query(default=None),
) -> dict[str, Any]:
    packer = _load_packer(project_id=project_id)
    if id not in packer.nodes:
        raise HTTPException(status_code=404, detail=f"Node '{id}' not found.")
    attr = dict(packer.nodes[id])
    snippet = packer._read_snippet(id, max_lines=40)
    return {"ok": True, "node": attr, "snippet": snippet}


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("STEINER_HOST", "127.0.0.1")
    port = int(os.environ.get("STEINER_PORT", "8088"))
    uvicorn.run("hub.app.main:app", host=host, port=port, reload=False)
