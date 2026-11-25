# STEINER-HANDOFF :: COMPACT MID-SESSION BOOSTER

Profile: `{{PRESET_NAME}}` | Hub: `{{HUB_URL}}` | Root: `{{WORKSPACE_ROOT}}`
Budget: `max_tokens={{MAX_PACK_TOKENS}}` | Penalty: `alpha={{HUB_PENALTY_ALPHA}}` | Damping: `{{PAGERANK_DAMPING}}`
Roles: coordinator=`{{COORDINATOR_ROLE}}` | worker=`{{WORKER_ROLE}}` | qa=`{{QA_ROLE}}` | invoke=`{{CLI_INVOKE}}`

## Active Guardrails

1. **Zero-Exploration Rule**: Do not run recursive `find`/`grep` sweeps. Refresh AST locally with Tree-sitter `--code-only`, then pull a bounded Steiner pack:

~~~bash
curl -s -X POST "{{HUB_URL}}/api/context/pack" \
  -H "Content-Type: application/json" \
  -d '{"project_id":"<project-id>","seeds":["<seed-1>","<seed-2>"],"max_tokens":{{MAX_PACK_TOKENS}},"alpha":{{HUB_PENALTY_ALPHA}},"format":"xml"}'
~~~

2. **Anti-Swarm Gate**: If change scope is `<= {{ANTI_SWARM_MAX_FILES}} files` and `< {{ANTI_SWARM_MAX_LOC}} LOC` (`< 3 files / < 40 LOC`), skip multi-agent orchestration; execute directly or via local GPU (`{{LOCAL_INFERENCE_MODEL}}` at `{{LOCAL_INFERENCE_ENDPOINT}}`).
3. **Disjoint Slice Lock**: Above the gate, assign each worker an explicit, non-overlapping file list plus verbatim `<xml_pack>` and active `domain_invariant` nodes.
4. **Tier-1 Privacy Firewall**: `enforce_physical_isolation={{PRIVACY_ISOLATION}}`. Paths matching `{{TIER1_PATTERNS}}` never leave local inference and never merge into `global-graph.json`.
5. **1-Pass Headless QA + Refresh**: Validate syntax/tests + `{{OPSEC_SCAN_SCRIPT}}` in one pass, then trigger:

~~~bash
curl -s -X POST "{{HUB_URL}}{{REFRESH_ENDPOINT_TEMPLATE}}"
~~~
