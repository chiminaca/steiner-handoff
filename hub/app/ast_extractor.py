"""Local --code-only Tree-sitter AST extractor with stdlib AST / regex fallbacks."""

from __future__ import annotations

import ast
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from hub.app.privacy_guard import is_tier1_private, load_tier1_patterns

try:
    from tree_sitter import Language, Parser  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    Language = None  # type: ignore[assignment]
    Parser = None  # type: ignore[assignment]

try:
    import tree_sitter_python as _ts_py  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    _ts_py = None

try:
    import tree_sitter_javascript as _ts_js  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    _ts_js = None

try:
    import tree_sitter_typescript as _ts_ts  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    _ts_ts = None


CODE_EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
}

DEFAULT_IGNORED_DIRS: frozenset[str] = frozenset({
    ".git",
    "node_modules",
    "dist",
    "build",
    "vendor",
    ".venv",
    "venv",
    "target",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "graphify-out",
    ".steiner",
    ".steiner-hub",
})

DEFAULT_IGNORED_GLOBS: tuple[str, ...] = (
    "*.min.js",
    "*.min.css",
    "*.graph.json",
    "*.pyc",
    "*.pyo",
    "*.map",
)


def get_workspace_root() -> Path:
    return Path(os.environ.get("STEINER_WORKSPACE_ROOT", "/srv/workspaces")).expanduser().resolve()


def get_state_dir(workspace_root: Path | None = None) -> Path:
    env_dir = os.environ.get("STEINER_STATE_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    root = workspace_root or get_workspace_root()
    return (root / ".steiner-hub").resolve()


def _sanitize_id(raw: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw.strip())
    return cleaned.strip("_") or "item"


def _estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 3.6))


def _load_ignore_rules(repo_root: Path) -> tuple[set[str], list[str]]:
    dir_names = set(DEFAULT_IGNORED_DIRS)
    globs = list(DEFAULT_IGNORED_GLOBS)
    ignore_file = repo_root / ".graphifyignore"
    if ignore_file.is_file():
        for raw_line in ignore_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.endswith("/"):
                dir_names.add(line.rstrip("/"))
                globs.append(line.rstrip("/") + "/*")
            else:
                globs.append(line)
    return dir_names, globs


def _is_ignored(rel_posix: str, is_dir: bool, dir_names: set[str], globs: list[str]) -> bool:
    parts = rel_posix.split("/")
    if any(p in dir_names for p in parts):
        return True
    name = parts[-1]
    for pat in globs:
        pat_clean = pat.rstrip("/")
        if fnmatch(rel_posix, pat_clean) or fnmatch(name, pat_clean):
            return True
        if is_dir and fnmatch(rel_posix + "/", pat):
            return True
    return False


def _get_ts_parser(lang_name: str) -> Any:
    if Language is None or Parser is None:
        return None
    try:
        if lang_name == "python" and _ts_py is not None:
            return Parser(Language(_ts_py.language()))
        if lang_name == "javascript" and _ts_js is not None:
            return Parser(Language(_ts_js.language()))
        if lang_name == "typescript" and _ts_ts is not None:
            return Parser(Language(_ts_ts.language_typescript()))
        if lang_name == "tsx" and _ts_ts is not None:
            return Parser(Language(_ts_ts.language_tsx()))
    except Exception:
        return None
    return None


def _node_text(node: Any, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _extract_python_stdlib(
    source_text: str,
) -> tuple[list[dict[str, Any]], list[str], list[tuple[str, str]]]:
    defs: list[dict[str, Any]] = []
    imports: list[str] = []
    calls: list[tuple[str, str]] = []
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return defs, imports, calls

    lines = source_text.splitlines()

    def _collect_calls(body_node: ast.AST, caller_qname: str) -> None:
        for child in ast.walk(body_node):
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Name):
                    calls.append((caller_qname, func.id))
                elif isinstance(func, ast.Attribute):
                    calls.append((caller_qname, func.attr))

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod:
                imports.append(mod)
        elif isinstance(node, ast.ClassDef):
            start = node.lineno
            end = getattr(node, "end_lineno", start) or start
            snippet = "\n".join(lines[start - 1 : end])
            defs.append({
                "qname": node.name,
                "label": node.name,
                "kind": "class",
                "callable": False,
                "parent": None,
                "start_line": start,
                "end_line": end,
                "tokens_est": _estimate_tokens(snippet),
                "bases": [
                    b.id for b in node.bases if isinstance(b, ast.Name)
                ],
            })
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    m_start = sub.lineno
                    m_end = getattr(sub, "end_lineno", m_start) or m_start
                    m_snip = "\n".join(lines[m_start - 1 : m_end])
                    qname = f"{node.name}.{sub.name}"
                    defs.append({
                        "qname": qname,
                        "label": f"{qname}()",
                        "kind": "method",
                        "callable": True,
                        "parent": node.name,
                        "start_line": m_start,
                        "end_line": m_end,
                        "tokens_est": _estimate_tokens(m_snip),
                        "bases": [],
                    })
                    _collect_calls(sub, qname)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = node.lineno
            end = getattr(node, "end_lineno", start) or start
            snippet = "\n".join(lines[start - 1 : end])
            defs.append({
                "qname": node.name,
                "label": f"{node.name}()",
                "kind": "function",
                "callable": True,
                "parent": None,
                "start_line": start,
                "end_line": end,
                "tokens_est": _estimate_tokens(snippet),
                "bases": [],
            })
            _collect_calls(node, node.name)
        else:
            _collect_calls(node, "__module__")

    return defs, imports, calls


def _extract_python_treesitter(
    source_bytes: bytes, parser: Any
) -> tuple[list[dict[str, Any]], list[str], list[tuple[str, str]]]:
    tree = parser.parse(source_bytes)
    root = tree.root_node
    defs: list[dict[str, Any]] = []
    imports: list[str] = []
    calls: list[tuple[str, str]] = []

    def _walk_calls(node: Any, caller_qname: str) -> None:
        if node.type == "call":
            fn = node.child_by_field_name("function")
            if fn is not None:
                if fn.type == "identifier":
                    calls.append((caller_qname, _node_text(fn, source_bytes)))
                elif fn.type == "attribute":
                    attr = fn.child_by_field_name("attribute")
                    if attr is not None:
                        calls.append((caller_qname, _node_text(attr, source_bytes)))
        for ch in node.children:
            if ch.type not in ("function_definition", "class_definition"):
                _walk_calls(ch, caller_qname)

    def _walk_top(node: Any, current_class: str | None = None) -> None:
        if node.type == "import_statement":
            txt = _node_text(node, source_bytes)
            for part in re.findall(r"import\s+([A-Za-z0-9_.]+)", txt):
                imports.append(part)
        elif node.type == "import_from_statement":
            mod = node.child_by_field_name("module_name")
            if mod is not None:
                imports.append(_node_text(mod, source_bytes))
        elif node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            cls_name = _node_text(name_node, source_bytes) if name_node else "Class"
            start = node.start_point[0] + 1
            end = node.end_point[0] + 1
            defs.append({
                "qname": cls_name,
                "label": cls_name,
                "kind": "class",
                "callable": False,
                "parent": None,
                "start_line": start,
                "end_line": end,
                "tokens_est": _estimate_tokens(_node_text(node, source_bytes)),
                "bases": [],
            })
            body = node.child_by_field_name("body")
            if body is not None:
                for ch in body.children:
                    _walk_top(ch, current_class=cls_name)
            return
        elif node.type == "function_definition":
            name_node = node.child_by_field_name("name")
            fn_name = _node_text(name_node, source_bytes) if name_node else "fn"
            start = node.start_point[0] + 1
            end = node.end_point[0] + 1
            qname = f"{current_class}.{fn_name}" if current_class else fn_name
            defs.append({
                "qname": qname,
                "label": f"{qname}()",
                "kind": "method" if current_class else "function",
                "callable": True,
                "parent": current_class,
                "start_line": start,
                "end_line": end,
                "tokens_est": _estimate_tokens(_node_text(node, source_bytes)),
                "bases": [],
            })
            body = node.child_by_field_name("body")
            if body is not None:
                _walk_calls(body, qname)
            return
        elif node.type == "decorated_definition":
            for ch in node.children:
                if ch.type in ("class_definition", "function_definition"):
                    _walk_top(ch, current_class=current_class)
            return
        else:
            if current_class is None and node.type == "expression_statement":
                _walk_calls(node, "__module__")
        for ch in node.children:
            if node.type == "module":
                _walk_top(ch, current_class=None)

    _walk_top(root)
    return defs, imports, calls


def _extract_jsts_regex(
    source_text: str,
) -> tuple[list[dict[str, Any]], list[str], list[tuple[str, str]]]:
    defs: list[dict[str, Any]] = []
    imports: list[str] = []
    calls: list[tuple[str, str]] = []
    lines = source_text.splitlines()
    total = len(lines)

    for m in re.finditer(r"""(?:import\s+.*?\s+from\s+|require\s*\(\s*)['"]([^'"]+)['"]""", source_text):
        imports.append(m.group(1))

    class_pat = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)")
    fn_pat = re.compile(
        r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\("
    )
    arrow_pat = re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*=>"
    )
    iface_pat = re.compile(r"^\s*(?:export\s+)?(?:interface|type)\s+([A-Za-z_$][A-Za-z0-9_$]*)")
    method_pat = re.compile(r"^\s{2,4}(?:async\s+)?([A-Za-z_$][A-Za-z0-9_$]*)\s*\([^)]*\)\s*\{")

    current_class: str | None = None
    brace_depth = 0
    class_depth = -1

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue

        if current_class is not None and brace_depth <= class_depth:
            current_class = None
            class_depth = -1

        cm = class_pat.match(line)
        fm = fn_pat.match(line)
        am = arrow_pat.match(line)
        im = iface_pat.match(line)
        mm = method_pat.match(line) if current_class else None

        if cm:
            cname = cm.group(1)
            current_class = cname
            class_depth = brace_depth
            defs.append({
                "qname": cname,
                "label": cname,
                "kind": "class",
                "callable": False,
                "parent": None,
                "start_line": idx,
                "end_line": min(total, idx + 20),
                "tokens_est": 24,
                "bases": [],
            })
        elif mm and mm.group(1) not in {"if", "for", "while", "switch", "catch", "constructor"}:
            mname = mm.group(1)
            qname = f"{current_class}.{mname}"
            defs.append({
                "qname": qname,
                "label": f"{qname}()",
                "kind": "method",
                "callable": True,
                "parent": current_class,
                "start_line": idx,
                "end_line": min(total, idx + 14),
                "tokens_est": 18,
                "bases": [],
            })
        elif fm:
            fname = fm.group(1)
            defs.append({
                "qname": fname,
                "label": f"{fname}()",
                "kind": "function",
                "callable": True,
                "parent": None,
                "start_line": idx,
                "end_line": min(total, idx + 16),
                "tokens_est": 20,
                "bases": [],
            })
        elif am:
            aname = am.group(1)
            defs.append({
                "qname": aname,
                "label": f"{aname}()",
                "kind": "function",
                "callable": True,
                "parent": None,
                "start_line": idx,
                "end_line": min(total, idx + 12),
                "tokens_est": 16,
                "bases": [],
            })
        elif im:
            iname = im.group(1)
            defs.append({
                "qname": iname,
                "label": iname,
                "kind": "interface",
                "callable": False,
                "parent": None,
                "start_line": idx,
                "end_line": min(total, idx + 8),
                "tokens_est": 12,
                "bases": [],
            })

        brace_depth += line.count("{") - line.count("}")

    for call_m in re.finditer(r"\b([A-Za-z_$][A-Za-z0-9_$]*)\s*\(", source_text):
        callee = call_m.group(1)
        if callee not in {"if", "for", "while", "switch", "catch", "function", "return", "typeof"}:
            calls.append(("__module__", callee))

    return defs, imports, calls


def _extract_jsts_treesitter(
    source_bytes: bytes, parser: Any
) -> tuple[list[dict[str, Any]], list[str], list[tuple[str, str]]]:
    tree = parser.parse(source_bytes)
    root = tree.root_node
    defs: list[dict[str, Any]] = []
    imports: list[str] = []
    calls: list[tuple[str, str]] = []

    def _walk_calls(node: Any, caller: str) -> None:
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None:
                if fn.type == "identifier":
                    calls.append((caller, _node_text(fn, source_bytes)))
                elif fn.type == "member_expression":
                    prop = fn.child_by_field_name("property")
                    if prop is not None:
                        calls.append((caller, _node_text(prop, source_bytes)))
        for ch in node.children:
            _walk_calls(ch, caller)

    def _walk(node: Any, current_class: str | None = None) -> None:
        ntype = node.type
        if ntype == "import_statement":
            src = node.child_by_field_name("source")
            if src is not None:
                imports.append(_node_text(src, source_bytes).strip("'\""))
        elif ntype in ("class_declaration", "class"):
            name_n = node.child_by_field_name("name")
            cname = _node_text(name_n, source_bytes) if name_n else "Class"
            defs.append({
                "qname": cname,
                "label": cname,
                "kind": "class",
                "callable": False,
                "parent": None,
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "tokens_est": _estimate_tokens(_node_text(node, source_bytes)),
                "bases": [],
            })
            body = node.child_by_field_name("body")
            if body is not None:
                for ch in body.children:
                    _walk(ch, current_class=cname)
            return
        elif ntype == "method_definition":
            name_n = node.child_by_field_name("name")
            mname = _node_text(name_n, source_bytes) if name_n else "method"
            qname = f"{current_class}.{mname}" if current_class else mname
            defs.append({
                "qname": qname,
                "label": f"{qname}()",
                "kind": "method",
                "callable": True,
                "parent": current_class,
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "tokens_est": _estimate_tokens(_node_text(node, source_bytes)),
                "bases": [],
            })
            _walk_calls(node, qname)
            return
        elif ntype in ("function_declaration", "generator_function_declaration"):
            name_n = node.child_by_field_name("name")
            fname = _node_text(name_n, source_bytes) if name_n else "fn"
            defs.append({
                "qname": fname,
                "label": f"{fname}()",
                "kind": "function",
                "callable": True,
                "parent": None,
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "tokens_est": _estimate_tokens(_node_text(node, source_bytes)),
                "bases": [],
            })
            _walk_calls(node, fname)
            return
        elif ntype == "variable_declarator":
            name_n = node.child_by_field_name("name")
            val_n = node.child_by_field_name("value")
            if name_n is not None and val_n is not None and val_n.type in ("arrow_function", "function_expression"):
                vname = _node_text(name_n, source_bytes)
                defs.append({
                    "qname": vname,
                    "label": f"{vname}()",
                    "kind": "function",
                    "callable": True,
                    "parent": None,
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "tokens_est": _estimate_tokens(_node_text(node, source_bytes)),
                    "bases": [],
                })
                _walk_calls(val_n, vname)
                return
        elif ntype in ("interface_declaration", "type_alias_declaration"):
            name_n = node.child_by_field_name("name")
            iname = _node_text(name_n, source_bytes) if name_n else "Type"
            defs.append({
                "qname": iname,
                "label": iname,
                "kind": "interface",
                "callable": False,
                "parent": None,
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "tokens_est": _estimate_tokens(_node_text(node, source_bytes)),
                "bases": [],
            })
        for ch in node.children:
            _walk(ch, current_class=current_class)

    _walk(root)
    return defs, imports, calls


def _extract_file(
    file_path: Path, lang_name: str
) -> tuple[list[dict[str, Any]], list[str], list[tuple[str, str]], int, int]:
    raw_bytes = file_path.read_bytes()
    text = raw_bytes.decode("utf-8", errors="replace")
    line_count = max(1, text.count("\n") + (0 if text.endswith("\n") else 1))
    toks = _estimate_tokens(text)

    parser = _get_ts_parser(lang_name)
    if lang_name == "python":
        if parser is not None:
            defs, imports, calls = _extract_python_treesitter(raw_bytes, parser)
        else:
            defs, imports, calls = _extract_python_stdlib(text)
    else:
        if parser is not None:
            defs, imports, calls = _extract_jsts_treesitter(raw_bytes, parser)
        else:
            defs, imports, calls = _extract_jsts_regex(text)

    return defs, imports, calls, line_count, toks


def _load_registry(registry_path: Path) -> dict[str, Any]:
    if registry_path.is_file():
        try:
            data = json.loads(registry_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("projects"), dict):
                return data
        except Exception:
            pass
    return {"projects": {}}


def _save_registry(registry_path: Path, registry: dict[str, Any]) -> None:
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")


def index_project(
    repo_path: str | Path,
    project_id: str | None = None,
    name: str | None = None,
    private: bool | None = None,
    group: str = "services",
    host: str = "workspace",
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Index a repository (--code-only) and persist its enriched AST graph JSON."""
    repo_root = Path(repo_path).expanduser().resolve()
    if not repo_root.is_dir():
        raise FileNotFoundError(f"Repository path does not exist: {repo_root}")

    pid = _sanitize_id(project_id or repo_root.name)
    pname = (name or pid).strip()
    target_state_dir = Path(state_dir).expanduser().resolve() if state_dir else get_state_dir()
    projects_dir = target_state_dir / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)

    patterns = load_tier1_patterns(repo_root / "topology.yaml")
    auto_private = is_tier1_private({"id": pid, "name": pname, "path": str(repo_root)}, patterns=patterns)
    is_private = bool(private) or auto_private

    dir_names, ignore_globs = _load_ignore_rules(repo_root)

    nodes: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    symbol_index: dict[str, list[str]] = {}
    file_node_by_stem: dict[str, str] = {}
    file_node_by_rel: dict[str, str] = {}
    pending_imports: list[tuple[str, str]] = []
    pending_calls: list[tuple[str, str]] = []
    pending_bases: list[tuple[str, str]] = []

    file_count = 0
    callable_count = 0
    total_tokens_est = 0

    for root_str, dirs, files in os.walk(repo_root):
        cur_root = Path(root_str)
        rel_dir = cur_root.relative_to(repo_root).as_posix()
        rel_dir_prefix = "" if rel_dir == "." else rel_dir + "/"

        kept_dirs: list[str] = []
        for d in sorted(dirs):
            rel_d = f"{rel_dir_prefix}{d}"
            if not _is_ignored(rel_d, is_dir=True, dir_names=dir_names, globs=ignore_globs):
                kept_dirs.append(d)
        dirs[:] = kept_dirs

        for fname in sorted(files):
            fpath = cur_root / fname
            ext = fpath.suffix.lower()
            if ext not in CODE_EXTENSIONS:
                continue
            rel_f = f"{rel_dir_prefix}{fname}"
            if _is_ignored(rel_f, is_dir=False, dir_names=dir_names, globs=ignore_globs):
                continue

            lang_name = CODE_EXTENSIONS[ext]
            defs, imports, calls, line_count, f_tokens = _extract_file(fpath, lang_name)
            file_count += 1
            total_tokens_est += f_tokens

            file_nid = f"{pid}::{_sanitize_id(rel_f)}"
            file_node_by_rel[rel_f] = file_nid
            stem_no_ext = rel_f.rsplit(".", 1)[0].replace("/", ".")
            file_node_by_stem[stem_no_ext] = file_nid
            file_node_by_stem[fpath.stem] = file_nid

            nodes.append({
                "id": file_nid,
                "label": rel_f,
                "kind": "file",
                "type": "code",
                "file_type": "code",
                "callable": False,
                "_callable": False,
                "repo": pid,
                "project_id": pid,
                "group": group,
                "host": host,
                "source_file": rel_f,
                "source_location": f"L1-L{line_count}",
                "start_line": 1,
                "end_line": line_count,
                "tokens_est": f_tokens,
            })

            def_id_map: dict[str, str] = {"__module__": file_nid}
            for item in defs:
                qname = item["qname"]
                def_nid = f"{pid}::{_sanitize_id(rel_f)}::{_sanitize_id(qname)}"
                def_id_map[qname] = def_nid
                short_name = qname.split(".")[-1]
                symbol_index.setdefault(short_name, []).append(def_nid)
                symbol_index.setdefault(qname, []).append(def_nid)
                if item["callable"]:
                    callable_count += 1

                nodes.append({
                    "id": def_nid,
                    "label": item["label"],
                    "kind": item["kind"],
                    "type": "code",
                    "file_type": "code",
                    "callable": item["callable"],
                    "_callable": item["callable"],
                    "repo": pid,
                    "project_id": pid,
                    "group": group,
                    "host": host,
                    "source_file": rel_f,
                    "source_location": f"L{item['start_line']}-L{item['end_line']}",
                    "start_line": item["start_line"],
                    "end_line": item["end_line"],
                    "tokens_est": item["tokens_est"],
                })

                parent_nid = def_id_map.get(item["parent"] or "__module__", file_nid)
                links.append({
                    "source": parent_nid,
                    "target": def_nid,
                    "relation": "defines",
                    "weight": 1.0,
                })

                for base_name in item.get("bases", []):
                    pending_bases.append((def_nid, base_name))

            for imp in imports:
                pending_imports.append((file_nid, imp))

            for caller_qname, callee_name in calls:
                caller_nid = def_id_map.get(caller_qname, file_nid)
                pending_calls.append((caller_nid, callee_name))

    seen_edges: set[tuple[str, str, str]] = {
        (l["source"], l["target"], l["relation"]) for l in links
    }

    def _add_edge(src: str, dst: str, relation: str, weight: float = 1.0) -> None:
        if src == dst:
            return
        sig = (src, dst, relation)
        if sig not in seen_edges:
            seen_edges.add(sig)
            links.append({"source": src, "target": dst, "relation": relation, "weight": weight})

    for file_nid, imp_mod in pending_imports:
        norm_mod = imp_mod.lstrip("./").replace("/", ".")
        target_file = file_node_by_stem.get(norm_mod) or file_node_by_stem.get(norm_mod.split(".")[-1])
        if target_file:
            _add_edge(file_nid, target_file, "imports_from", weight=1.0)

    for caller_nid, callee_name in pending_calls:
        targets = symbol_index.get(callee_name, [])
        for tgt in targets[:2]:
            _add_edge(caller_nid, tgt, "calls", weight=1.0)

    for sub_nid, base_name in pending_bases:
        for tgt in symbol_index.get(base_name, [])[:1]:
            _add_edge(sub_nid, tgt, "inherits", weight=1.0)

    graph_path = projects_dir / f"{pid}.json"
    graph_payload: dict[str, Any] = {
        "project_id": pid,
        "repo": pid,
        "name": pname,
        "private": is_private,
        "group": group,
        "host": host,
        "indexed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nodes": nodes,
        "links": links,
    }
    graph_path.write_text(json.dumps(graph_payload, indent=2), encoding="utf-8")

    enrich_script = repo_root / "scripts" / "enrich_graph.py"
    if enrich_script.is_file():
        try:
            subprocess.run(
                [sys.executable, str(enrich_script), str(graph_path)],
                cwd=str(repo_root),
                check=False,
                timeout=30,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            enriched = json.loads(graph_path.read_text(encoding="utf-8"))
            for node in enriched.get("nodes", []):
                if isinstance(node, dict):
                    node.setdefault("repo", pid)
                    node.setdefault("project_id", pid)
                    node.setdefault("group", group)
                    node.setdefault("host", host)
            enriched["project_id"] = pid
            enriched["repo"] = pid
            enriched["name"] = pname
            enriched["private"] = is_private
            enriched["group"] = group
            enriched["host"] = host
            graph_payload = enriched
            graph_path.write_text(json.dumps(graph_payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    final_nodes = graph_payload.get("nodes", [])
    final_links = graph_payload.get("links", graph_payload.get("edges", []))
    invariant_count = sum(
        1 for n in final_nodes if isinstance(n, dict) and n.get("kind") == "domain_invariant"
    )

    registry_path = target_state_dir / "registry.json"
    registry = _load_registry(registry_path)
    registry["projects"][pid] = {
        "id": pid,
        "name": pname,
        "path": str(repo_root),
        "private": is_private,
        "group": group,
        "host": host,
        "graph_path": str(graph_path),
        "files": file_count,
        "nodes": len(final_nodes),
        "edges": len(final_links),
        "callables": callable_count,
        "invariants": invariant_count,
        "tokens_est": total_tokens_est,
        "indexed_at": graph_payload["indexed_at"],
    }
    _save_registry(registry_path, registry)

    return {
        "project": registry["projects"][pid],
        "graph_path": str(graph_path),
        "nodes": len(final_nodes),
        "edges": len(final_links),
        "files": file_count,
        "invariants": invariant_count,
    }
