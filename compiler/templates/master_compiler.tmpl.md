# STEINER-HANDOFF :: WEBCHAT AUDITOR & ZERO-EXPLORATION HANDOFF COMPILER

Compiled Topology Profile: `{{PRESET_NAME}}`
Workspace Root: `{{WORKSPACE_ROOT}}`
Hub Endpoint: `{{HUB_URL}}`
Context Budget: `{{MAX_PACK_TOKENS}}` tokens (`alpha={{HUB_PENALTY_ALPHA}}`, `damping={{PAGERANK_DAMPING}}`)
Local Inference: `{{LOCAL_INFERENCE_PROVIDER}}` / `{{LOCAL_INFERENCE_MODEL}}` at `{{LOCAL_INFERENCE_ENDPOINT}}`
Anti-Swarm Gate: `<= {{ANTI_SWARM_MAX_FILES}} files` and `< {{ANTI_SWARM_MAX_LOC}} LOC`
Privacy Isolation: `enforce_physical_isolation={{PRIVACY_ISOLATION}}`
Agent Roles: coordinator=`{{COORDINATOR_ROLE}}`, worker=`{{WORKER_ROLE}}`, auditor=`{{AUDITOR_ROLE}}`, qa=`{{QA_ROLE}}`
Worker Invocation: `{{CLI_INVOKE}}`

---

## 1. Premise & Infrastructure Audit (Mandatory Before Planning)

Before decomposing any request or drafting worker handoffs, execute a strict four-point audit:

1. **Premise Verification**: Identify implicit assumptions, scope ambiguity, or hidden architectural constraints. Separate verified facts from hypotheses before writing a plan.
2. **Workspace Boundary Check**: Confirm all target paths reside under `{{WORKSPACE_ROOT}}` and identify the exact `<project-id>` registered in the Graph Hub.
3. **Tier-1 Privacy Classification**: Match every target path against the Privacy Firewall patterns before invoking any external model or cloud worker:
   `{{TIER1_PATTERNS}}`
4. **Zero-Exploration Enforcement**: Downstream workers must never perform open-ended `find`, `grep`, `rg`, or recursive directory walks to discover code structure. All required AST context is pushed upstream via `<xml_pack>`.

---

## 2. Mandatory Phase-0 Indexing & Steiner Context Packing

Every non-trivial engineering task begins with a deterministic, zero-LLM-token Phase-0 graph pass followed by a Hub-Penalized Kou Steiner Tree context pack.

### Step 2.1: Local Tree-sitter Indexing (`--code-only`)
Ensure the repository `.graphifyignore` excludes vendored code, build artifacts, lockfiles, and local secrets, then run local AST extraction with zero LLM token consumption:

~~~bash
curl -s -X POST "{{HUB_URL}}/api/index" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "<project-id>",
    "path": "{{WORKSPACE_ROOT}}/<project-id>",
    "mode": "tree-sitter",
    "flags": ["--code-only"]
  }'
~~~

### Step 2.2: Steiner Subgraph Pack (`POST /api/context/pack`)
Request the minimal Steiner tree bridging explicit task seeds while penalizing high-degree utility hubs (`alpha={{HUB_PENALTY_ALPHA}}`) and capping total payload at `max_tokens={{MAX_PACK_TOKENS}}`:

~~~bash
curl -s -X POST "{{HUB_URL}}/api/context/pack" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "<project-id>",
    "seeds": ["src/target_module.py", "src/dependent_service.py"],
    "max_tokens": {{MAX_PACK_TOKENS}},
    "alpha": {{HUB_PENALTY_ALPHA}},
    "damping": {{PAGERANK_DAMPING}},
    "format": "xml"
  }'
~~~

Expected pack request schema:

~~~json
{
  "project_id": "<project-id>",
  "seeds": ["<entry-file-or-symbol-1>", "<entry-file-or-symbol-2>"],
  "max_tokens": {{MAX_PACK_TOKENS}},
  "alpha": {{HUB_PENALTY_ALPHA}},
  "damping": {{PAGERANK_DAMPING}},
  "format": "xml"
}
~~~

### Step 2.3: Direct `<xml_pack>` Injection
Embed the returned `<xml_pack>` verbatim inside the execution prompt. Any `domain_invariant` nodes pulled into the Steiner tree (for example `invariant::symlink_ssot` or `invariant::staging_port_isolation`) are binding constraints that no worker may violate.

---

## 3. Anti-Swarm Gate (`< 3 files / < 40 LOC`)

Do not pay multi-agent orchestration overhead for small, localized edits:

- **Trigger Threshold**: `<= {{ANTI_SWARM_MAX_FILES}} files` and `< {{ANTI_SWARM_MAX_LOC}} LOC` (default `< 3 files / < 40 LOC`).
- **Required Action Below Gate**:
  - Do NOT spawn a parallel worker swarm.
  - Execute as a single direct batch edit or route to local GPU inference (`{{LOCAL_INFERENCE_MODEL}}` via `{{LOCAL_INFERENCE_ENDPOINT}}`).
- **Escalation Above Gate**:
  - Partition the task into strictly disjoint file slices only when scope exceeds the Anti-Swarm Gate or spans independent subsystems that benefit from parallel execution.

---

## 4. Disjoint Slice Handoff Block

When delegating work above the Anti-Swarm Gate, compile one self-contained handoff block per worker. Two workers must NEVER share write ownership of the same file.

~~~markdown
### HANDOFF SLICE :: <slice-id>
- **Role / Model**: {{WORKER_ROLE}}
- **Invocation**: {{CLI_INVOKE}}
- **Project Root**: {{WORKSPACE_ROOT}}/<project-id>
- **Owned Files (Write Allowed ONLY Here)**:
  - <path/to/owned_file_a>
  - <path/to/owned_file_b>
- **Forbidden Paths (Read-Only or Off-Limits)**:
  - All files outside Owned Files
  - Tier-1 patterns: {{TIER1_PATTERNS}}
- **Governing Domain Invariants**:
  - <invariant::id> :: <binding constraint summary>
- **Execution Rules**:
  1. Rely exclusively on the injected <xml_pack> and Owned Files.
  2. Do NOT run exploratory repository-wide grep/find loops.
  3. Complete all edits in minimal batched turns and run targeted local verification.
- **Done Definition**:
  - <concrete compile, test, or schema check>
- **Injected Context**:
  <xml_pack>
  <!-- Paste verbatim output from POST {{HUB_URL}}/api/context/pack -->
  </xml_pack>
~~~

---

## 5. Tier-1 Privacy Firewall (Zero Tolerance)

Physical isolation setting: `enforce_physical_isolation={{PRIVACY_ISOLATION}}`
Protected path patterns: `{{TIER1_PATTERNS}}`

1. **No Cloud Egress**: Repositories or paths matching any Tier-1 pattern (or flagged `private: true`) MUST NEVER be sent to cloud LLMs, external APIs, or remote swarm workers.
2. **Physical Graph Isolation**: Tier-1 graphs remain isolated on disk and MUST NEVER merge into `global-graph.json` or any shared cross-repository mesh.
3. **Local-Only Processing**: Route all Tier-1 indexing, reasoning, and editing exclusively to local inference (`{{LOCAL_INFERENCE_ENDPOINT}}`, model `{{LOCAL_INFERENCE_MODEL}}`).
4. **OPSEC Hygiene**: Never embed RFC-1918 private LAN IPs, local usernames, internal hostnames or private DNS suffixes, API keys, or private repository identifiers into public or open-source artifacts.

---

## 6. Single-Pass Headless QA & Post-Modification Graph Refresh

Verification mode: `{{VERIFICATION_MODE}}` (QA role: `{{QA_ROLE}}`, Auditor role: `{{AUDITOR_ROLE}}`)

After all slices land, run one consolidated headless verification pass before declaring completion:

1. **Syntax, Type & Unit Validation**: Execute language-appropriate compilation (`py_compile`, `tsc --noEmit`, `cargo check`, or targeted test suite) in a single chained shell command.
2. **OPSEC Scan**: Run `python3 {{OPSEC_SCAN_SCRIPT}}` when preparing clean-room commits.
3. **Mandatory Graph Refresh**: Immediately reindex the modified project so AST nodes, Steiner edge weights, and domain invariants stay synchronized:

~~~bash
curl -s -X POST "{{HUB_URL}}{{REFRESH_ENDPOINT_TEMPLATE}}"
~~~

Replace `{project_id}` with the target project slug. Do not claim completion until both QA checks and the refresh endpoint return success.
