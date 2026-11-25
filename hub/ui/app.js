/* Steiner Hub L0-L3 ELK Spatial Cockpit with offline SVG layered fallback */

(function () {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";

  const state = {
    level: 1,
    projectId: "",
    projects: [],
    seeds: [],
    maxTokens: 3500,
    alpha: 1.4,
    activeSubgraph: null,
    lastLayout: null,
    transform: { x: 28, y: 28, scale: 1.0 },
    isPanning: false,
    panStart: { x: 0, y: 0 },
  };

  const els = {
    healthPill: document.getElementById("health-pill"),
    projectSelect: document.getElementById("project-select"),
    levelTabs: document.querySelectorAll(".tab-btn"),
    searchInput: document.getElementById("search-input"),
    searchResults: document.getElementById("search-results"),
    btnOpenIndex: document.getElementById("btn-open-index"),
    btnRefreshProject: document.getElementById("btn-refresh-project"),
    btnSyncGlobal: document.getElementById("btn-sync-global"),
    layoutBadge: document.getElementById("layout-engine-badge"),
    topoStats: document.getElementById("topology-stats"),
    svgStage: document.getElementById("svg-stage"),
    svg: document.getElementById("cockpit-svg"),
    viewportGroup: document.getElementById("viewport-group"),
    edgesLayer: document.getElementById("edges-layer"),
    nodesLayer: document.getElementById("nodes-layer"),
    emptyState: document.getElementById("empty-canvas-state"),
    btnEmptyIndex: document.getElementById("btn-empty-index"),
    btnZoomIn: document.getElementById("btn-zoom-in"),
    btnZoomOut: document.getElementById("btn-zoom-out"),
    btnFitView: document.getElementById("btn-fit-view"),
    btnClearOverlay: document.getElementById("btn-clear-overlay"),
    zoomReadout: document.getElementById("zoom-readout"),
    packForm: document.getElementById("pack-form"),
    seedInput: document.getElementById("seed-input"),
    btnAddSeed: document.getElementById("btn-add-seed"),
    seedChips: document.getElementById("seed-chips"),
    maxTokensInput: document.getElementById("max-tokens-input"),
    maxTokensVal: document.getElementById("max-tokens-val"),
    alphaInput: document.getElementById("alpha-input"),
    alphaVal: document.getElementById("alpha-val"),
    packLatency: document.getElementById("pack-latency"),
    metricTokens: document.getElementById("metric-tokens"),
    metricTiers: document.getElementById("metric-tiers"),
    metricEdges: document.getElementById("metric-edges"),
    xmlOutput: document.getElementById("xml-output"),
    btnCopyXml: document.getElementById("btn-copy-xml"),
    indexModal: document.getElementById("index-modal"),
    indexForm: document.getElementById("index-form"),
    btnCloseIndex: document.getElementById("btn-close-index"),
    btnCancelIndex: document.getElementById("btn-cancel-index"),
    idxPath: document.getElementById("idx-path"),
    idxId: document.getElementById("idx-id"),
    idxGroup: document.getElementById("idx-group"),
    idxPrivate: document.getElementById("idx-private"),
    toastRegion: document.getElementById("toast-region"),
  };

  function showToast(message, kind) {
    const item = document.createElement("div");
    item.className = "toast" + (kind === "ok" ? " is-ok" : kind === "error" ? " is-error" : "");
    item.textContent = message;
    els.toastRegion.appendChild(item);
    window.setTimeout(() => {
      item.remove();
    }, 3600);
  }

  async function apiJson(url, options) {
    const res = await fetch(url, options);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.detail || data.error || "Request failed (" + res.status + ")");
    }
    return data;
  }

  function applyTransform() {
    const t = state.transform;
    els.viewportGroup.setAttribute(
      "transform",
      "translate(" + Math.round(t.x) + "," + Math.round(t.y) + ") scale(" + t.scale.toFixed(3) + ")"
    );
    els.zoomReadout.textContent = Math.round(t.scale * 100) + "%";
  }

  function fitView(graphWidth, graphHeight) {
    const rect = els.svgStage.getBoundingClientRect();
    const cw = rect.width || 800;
    const ch = rect.height || 520;
    const gw = Math.max(200, graphWidth || 800);
    const gh = Math.max(160, graphHeight || 500);
    const pad = 40;
    const scale = Math.min(1.15, Math.max(0.28, Math.min((cw - pad * 2) / gw, (ch - pad * 2) / gh)));
    state.transform.scale = scale;
    state.transform.x = Math.max(16, Math.round((cw - gw * scale) / 2));
    state.transform.y = Math.max(16, Math.round((ch - gh * scale) / 2));
    applyTransform();
  }

  function deterministicFallbackLayout(rootGraph) {
    function layoutNode(node) {
      const children = node.children || [];
      if (children.length === 0) {
        return {
          ...node,
          width: node.width || 220,
          height: node.height || 64,
        };
      }
      const laidChildren = children.map(layoutNode);
      const tier = (node.properties && node.properties.tier) || "ROOT";
      const headerTop = tier === "ROOT" ? 0 : 42;
      const padX = 20;
      const padBottom = 20;
      const gap = 20;
      const cols = laidChildren.length > 4 ? 3 : laidChildren.length > 1 ? 2 : 1;

      let curX = padX;
      let curY = headerTop;
      let rowMaxH = 0;
      let maxRight = 240;

      laidChildren.forEach((child, idx) => {
        if (idx > 0 && idx % cols === 0) {
          curX = padX;
          curY += rowMaxH + gap;
          rowMaxH = 0;
        }
        child.x = curX;
        child.y = curY;
        curX += child.width + gap;
        maxRight = Math.max(maxRight, curX);
        rowMaxH = Math.max(rowMaxH, child.height);
      });

      const totalW = Math.max(node.width || 260, maxRight);
      const totalH = Math.max(node.height || 100, curY + rowMaxH + padBottom);
      return {
        ...node,
        width: totalW,
        height: totalH,
        children: laidChildren,
      };
    }

    const laidRoot = layoutNode(rootGraph);
    return laidRoot;
  }

  async function computeLayout(graph) {
    if (typeof window.ELK === "function") {
      try {
        const elk = new window.ELK();
        const result = await elk.layout(graph);
        els.layoutBadge.textContent = "ELK.js";
        return result;
      } catch (_err) {
        els.layoutBadge.textContent = "SVG Fallback";
        return deterministicFallbackLayout(graph);
      }
    }
    els.layoutBadge.textContent = "SVG Fallback";
    return deterministicFallbackLayout(graph);
  }

  function svgEl(tag, attrs) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      el.setAttribute(k, String(v));
    }
    return el;
  }

  function clearSvgLayers() {
    while (els.edgesLayer.firstChild) els.edgesLayer.removeChild(els.edgesLayer.firstChild);
    while (els.nodesLayer.firstChild) els.nodesLayer.removeChild(els.nodesLayer.firstChild);
  }

  function collectSubgraphOverlay() {
    const sg = state.activeSubgraph;
    if (!sg) return null;
    const seeds = new Set(sg.resolved_seeds || []);
    const backbone = new Set((sg.tier1_backbone || []).map((n) => n.id));
    const ppr = new Set((sg.tier2_neighbors || []).map((n) => n.id));
    const activeRepos = new Set();
    for (const n of sg.tier1_backbone || []) if (n.repo) activeRepos.add("repo::" + n.repo);
    for (const n of sg.tier2_neighbors || []) if (n.repo) activeRepos.add("repo::" + n.repo);
    const activeEdgePairs = new Set();
    for (const ed of sg.tier3_topology || []) {
      activeEdgePairs.add(ed.source + "->" + ed.target);
      activeEdgePairs.add(ed.target + "->" + ed.source);
      const ru = (ed.source || "").split("::")[0];
      const rv = (ed.target || "").split("::")[0];
      if (ru && rv && ru !== rv) {
        activeEdgePairs.add("repo::" + ru + "->repo::" + rv);
        activeEdgePairs.add("repo::" + rv + "->repo::" + ru);
      }
    }
    return { seeds, backbone, ppr, activeRepos, activeEdgePairs };
  }

  function renderLayoutGraph(layoutRoot) {
    clearSvgLayers();
    state.lastLayout = layoutRoot;
    const absCoords = new Map();
    const overlay = collectSubgraphOverlay();
    let totalNodes = 0;

    function walkNodes(node, offsetX, offsetY) {
      const props = node.properties || {};
      const tier = props.tier;
      const x = (node.x || 0) + offsetX;
      const y = (node.y || 0) + offsetY;
      const w = node.width || 200;
      const h = node.height || 64;

      if (tier) {
        totalNodes += 1;
        absCoords.set(node.id, { x, y, width: w, height: h });

        const g = svgEl("g", {
          class: "svg-node",
          "data-node-id": node.id,
          "data-tier": tier,
        });

        let fill = "#0d1117";
        let stroke = "#27272a";
        let strokeWidth = 1;
        let opacity = 1;

        if (tier === "L0") {
          fill = "#0c0f14";
          stroke = "#30363d";
        } else if (tier === "L1") {
          fill = "#11161f";
          stroke = "#27272a";
        } else if (tier === "L2") {
          fill = props.drilled ? "#0f1722" : "#141923";
          stroke = props.drilled ? "#38bdf8" : "#30363d";
        } else if (tier === "L3") {
          fill = props.invariant ? "#1c160b" : "#0d1117";
          stroke = props.invariant ? "#f59e0b" : "#27272a";
        }

        if (overlay) {
          if (overlay.seeds.has(node.id)) {
            stroke = "#14b8a6";
            strokeWidth = 2.4;
          } else if (overlay.backbone.has(node.id)) {
            stroke = "#38bdf8";
            strokeWidth = 2.0;
          } else if (overlay.ppr.has(node.id)) {
            stroke = "#a1a1aa";
            strokeWidth = 1.4;
          } else if (tier === "L2" && overlay.activeRepos.has(node.id)) {
            stroke = "#14b8a6";
            strokeWidth = 2.0;
          } else if (tier === "L2" || tier === "L3") {
            opacity = 0.38;
          }
        }

        g.setAttribute("opacity", String(opacity));

        g.appendChild(
          svgEl("rect", {
            class: "svg-node-box",
            x,
            y,
            width: w,
            height: h,
            rx: tier === "L0" ? 10 : 7,
            fill,
            stroke,
            "stroke-width": strokeWidth,
          })
        );

        g.appendChild(
          svgEl("rect", {
            x: x + w - 34,
            y: y + 8,
            width: 24,
            height: 16,
            rx: 4,
            fill: "#09090b",
            stroke: "#27272a",
          })
        );

        const badgeTxt = svgEl("text", {
          x: x + w - 22,
          y: y + 19,
          "text-anchor": "middle",
          fill: tier === "L3" && props.invariant ? "#f59e0b" : "#a1a1aa",
          "font-size": "9.5",
          "font-family": "JetBrains Mono, monospace",
        });
        badgeTxt.textContent = tier;
        g.appendChild(badgeTxt);

        const rawLabel = String(props.label || node.id);
        const label = rawLabel.length > 26 ? rawLabel.slice(0, 24) + ".." : rawLabel;
        const titleTxt = svgEl("text", {
          x: x + 12,
          y: y + 21,
          fill: "#f4f4f5",
          "font-size": tier === "L0" ? "13" : "12",
          "font-weight": "600",
          "font-family": tier === "L2" || tier === "L3" ? "JetBrains Mono, monospace" : "Inter, sans-serif",
        });
        titleTxt.textContent = label;
        g.appendChild(titleTxt);

        const rawSub = String(props.sub || "");
        const sub = rawSub.length > 34 ? rawSub.slice(0, 32) + ".." : rawSub;
        if (sub) {
          const subTxt = svgEl("text", {
            x: x + 12,
            y: y + 38,
            fill: "#71717a",
            "font-size": "10",
            "font-family": "JetBrains Mono, monospace",
          });
          subTxt.textContent = sub;
          g.appendChild(subTxt);
        }

        g.addEventListener("click", (ev) => {
          ev.stopPropagation();
          if (tier === "L2" || tier === "L3") {
            const seedVal = tier === "L2" ? (props.repo || node.id.replace(/^repo::/, "")) : node.id;
            addSeed(seedVal);
          }
        });

        g.addEventListener("dblclick", (ev) => {
          ev.stopPropagation();
          if (tier === "L2") {
            const repo = props.repo || node.id.replace(/^repo::/, "");
            state.projectId = repo;
            els.projectSelect.value = repo;
            setLevel(3);
          }
        });

        els.nodesLayer.appendChild(g);
      }

      for (const child of node.children || []) {
        walkNodes(child, tier ? x : offsetX, tier ? y : offsetY);
      }
    }

    walkNodes(layoutRoot, 0, 0);

    const edges = layoutRoot.edges || [];
    for (const edge of edges) {
      const srcId = edge.sources && edge.sources[0];
      const dstId = edge.targets && edge.targets[0];
      const props = edge.properties || {};

      let pts = [];
      if (edge.sections && edge.sections.length > 0) {
        const sec = edge.sections[0];
        if (sec.startPoint) pts.push(sec.startPoint);
        if (sec.bendPoints) pts.push(...sec.bendPoints);
        if (sec.endPoint) pts.push(sec.endPoint);
      } else if (absCoords.has(srcId) && absCoords.has(dstId)) {
        const a = absCoords.get(srcId);
        const b = absCoords.get(dstId);
        pts = [
          { x: a.x + a.width, y: a.y + a.height / 2 },
          { x: b.x, y: b.y + b.height / 2 },
        ];
      }

      if (pts.length < 2) continue;

      const d = pts
        .map((p, i) => (i === 0 ? "M " : "L ") + Math.round(p.x) + " " + Math.round(p.y))
        .join(" ");

      const key = srcId + "->" + dstId;
      const isSteiner = overlay && overlay.activeEdgePairs.has(key);
      const stroke = isSteiner ? "#14b8a6" : "#52525b";
      const strokeWidth = isSteiner ? "2.6" : "1.4";
      const opacity = overlay ? (isSteiner ? "1" : "0.18") : "0.7";
      const marker = isSteiner ? "url(#arrow-steiner)" : "url(#arrow-muted)";

      els.edgesLayer.appendChild(
        svgEl("path", {
          d,
          fill: "none",
          stroke,
          "stroke-width": strokeWidth,
          "stroke-opacity": opacity,
          "marker-end": marker,
        })
      );
    }

    const hostCount = (layoutRoot.children || []).length;
    els.topoStats.textContent = hostCount + " hosts | " + totalNodes + " boxes | " + edges.length + " edges";
  }

  async function refreshHealthAndProjects() {
    try {
      const [health, projData] = await Promise.all([
        apiJson("/api/health"),
        apiJson("/api/projects"),
      ]);
      els.healthPill.textContent = "Online | " + health.projects_count + " proj";
      els.healthPill.classList.add("is-ok");

      state.projects = projData.projects || [];
      const current = state.projectId;
      els.projectSelect.innerHTML = '<option value="">Global Non-Private Mesh</option>';
      for (const p of state.projects) {
        const opt = document.createElement("option");
        opt.value = p.id;
        opt.textContent = p.id + (p.private ? " [Tier-1 Private]" : "");
        els.projectSelect.appendChild(opt);
      }
      if (current && state.projects.some((p) => p.id === current)) {
        els.projectSelect.value = current;
      } else if (state.projects.length > 0 && !current) {
        if (state.seeds.length === 0) {
          addSeed(state.projects[0].id, true);
        }
      }
    } catch (err) {
      els.healthPill.textContent = "Offline";
      els.healthPill.classList.remove("is-ok");
      showToast(err.message, "error");
    }
  }

  async function loadTopology() {
    try {
      const params = new URLSearchParams({ level: String(state.level) });
      if (state.projectId) params.set("project_id", state.projectId);
      const data = await apiJson("/api/cockpit/topology?" + params.toString());
      const graph = data.graph || { id: "root", children: [], edges: [] };
      const hasNodes = (graph.children || []).length > 0;
      els.emptyState.classList.toggle("hidden", hasNodes);

      const laid = await computeLayout(graph);
      renderLayoutGraph(laid);
      fitView(laid.width || 800, laid.height || 480);
    } catch (err) {
      showToast("Topology load failed: " + err.message, "error");
    }
  }

  function setLevel(lvl) {
    state.level = lvl;
    els.levelTabs.forEach((btn) => {
      btn.classList.toggle("is-active", Number(btn.dataset.level) === lvl);
    });
    loadTopology();
  }

  function renderSeedChips() {
    els.seedChips.innerHTML = "";
    for (const seed of state.seeds) {
      const chip = document.createElement("span");
      chip.className = "seed-chip";

      const txt = document.createElement("span");
      txt.className = "seed-chip-text";
      txt.textContent = seed;
      chip.appendChild(txt);

      const rm = document.createElement("button");
      rm.type = "button";
      rm.className = "seed-chip-remove";
      rm.setAttribute("aria-label", "Remove seed " + seed);
      rm.innerHTML =
        '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>';
      rm.addEventListener("click", () => {
        state.seeds = state.seeds.filter((s) => s !== seed);
        renderSeedChips();
      });
      chip.appendChild(rm);
      els.seedChips.appendChild(chip);
    }
  }

  function addSeed(value, silent) {
    const parts = String(value || "").split(",");
    for (const part of parts) {
      const clean = part.trim();
      if (!clean) continue;
      if (!state.seeds.includes(clean)) {
        state.seeds.push(clean);
        if (!silent) showToast("Added seed: " + clean, "ok");
      }
    }
    renderSeedChips();
  }

  async function runContextPack(ev) {
    if (ev) ev.preventDefault();
    if (state.seeds.length === 0) {
      showToast("Add at least one seed before running Context Pack.", "error");
      return;
    }
    try {
      const payload = {
        project_id: state.projectId || null,
        seeds: state.seeds,
        max_tokens: Number(state.maxTokens),
        alpha: Number(state.alpha),
      };
      const data = await apiJson("/api/context/pack", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      state.activeSubgraph = data.subgraph;
      els.btnClearOverlay.classList.remove("hidden");
      els.packLatency.textContent = data.elapsed_ms + " ms";
      els.metricTokens.textContent = data.actual_tokens + " / " + data.budget_tokens;
      els.metricTiers.textContent =
        (data.stats ? data.stats.packed_tier1 : 0) + " / " + (data.stats ? data.stats.packed_tier2 : 0);
      els.metricEdges.textContent = String(data.stats ? data.stats.packed_edges : 0);
      els.xmlOutput.textContent = data.xml_pack || "";

      if (state.lastLayout) {
        renderLayoutGraph(state.lastLayout);
      }
      showToast("Packed " + data.actual_tokens + " tokens in " + data.elapsed_ms + " ms.", "ok");
    } catch (err) {
      showToast(err.message, "error");
    }
  }

  let searchTimer = null;
  async function runSearch(term) {
    try {
      const params = new URLSearchParams({ q: term, limit: "15" });
      if (state.projectId) params.set("project_id", state.projectId);
      const data = await apiJson("/api/search?" + params.toString());
      const items = data.results || [];
      els.searchResults.innerHTML = "";
      if (items.length === 0) {
        els.searchResults.classList.add("hidden");
        return;
      }
      for (const item of items) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "search-item";

        const left = document.createElement("span");
        left.textContent = item.label || item.id;
        const right = document.createElement("span");
        right.className = "search-item-meta";
        right.textContent = (item.kind || "code") + " | " + (item.repo || "");

        btn.appendChild(left);
        btn.appendChild(right);
        btn.addEventListener("click", () => {
          addSeed(item.id);
          els.searchResults.classList.add("hidden");
          els.searchInput.value = "";
        });
        els.searchResults.appendChild(btn);
      }
      els.searchResults.classList.remove("hidden");
    } catch (_err) {
      els.searchResults.classList.add("hidden");
    }
  }

  function bindEvents() {
    els.levelTabs.forEach((btn) => {
      btn.addEventListener("click", () => setLevel(Number(btn.dataset.level)));
    });

    els.projectSelect.addEventListener("change", () => {
      state.projectId = els.projectSelect.value;
      loadTopology();
    });

    els.btnAddSeed.addEventListener("click", () => {
      addSeed(els.seedInput.value);
      els.seedInput.value = "";
    });

    els.seedInput.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        addSeed(els.seedInput.value);
        els.seedInput.value = "";
      }
    });

    els.maxTokensInput.addEventListener("input", () => {
      state.maxTokens = Number(els.maxTokensInput.value);
      els.maxTokensVal.textContent = String(state.maxTokens);
    });

    els.alphaInput.addEventListener("input", () => {
      state.alpha = Number(els.alphaInput.value);
      els.alphaVal.textContent = state.alpha.toFixed(1);
    });

    els.packForm.addEventListener("submit", runContextPack);

    els.btnCopyXml.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(els.xmlOutput.textContent || "");
        showToast("Copied <xml_pack> to clipboard.", "ok");
      } catch (_err) {
        showToast("Clipboard unavailable in this context.", "error");
      }
    });

    els.btnClearOverlay.addEventListener("click", () => {
      state.activeSubgraph = null;
      els.btnClearOverlay.classList.add("hidden");
      if (state.lastLayout) renderLayoutGraph(state.lastLayout);
    });

    els.btnZoomIn.addEventListener("click", () => {
      state.transform.scale = Math.min(2.5, state.transform.scale * 1.18);
      applyTransform();
    });

    els.btnZoomOut.addEventListener("click", () => {
      state.transform.scale = Math.max(0.22, state.transform.scale / 1.18);
      applyTransform();
    });

    els.btnFitView.addEventListener("click", () => {
      if (state.lastLayout) fitView(state.lastLayout.width, state.lastLayout.height);
    });

    els.svgStage.addEventListener("mousedown", (ev) => {
      state.isPanning = true;
      state.panStart = { x: ev.clientX - state.transform.x, y: ev.clientY - state.transform.y };
    });

    window.addEventListener("mousemove", (ev) => {
      if (!state.isPanning) return;
      state.transform.x = ev.clientX - state.panStart.x;
      state.transform.y = ev.clientY - state.panStart.y;
      applyTransform();
    });

    window.addEventListener("mouseup", () => {
      state.isPanning = false;
    });

    els.svgStage.addEventListener(
      "wheel",
      (ev) => {
        ev.preventDefault();
        const factor = ev.deltaY < 0 ? 1.09 : 1 / 1.09;
        state.transform.scale = Math.min(2.6, Math.max(0.22, state.transform.scale * factor));
        applyTransform();
      },
      { passive: false }
    );

    els.searchInput.addEventListener("input", () => {
      const term = els.searchInput.value.trim();
      window.clearTimeout(searchTimer);
      if (!term) {
        els.searchResults.classList.add("hidden");
        return;
      }
      searchTimer = window.setTimeout(() => runSearch(term), 180);
    });

    document.addEventListener("click", (ev) => {
      if (!els.searchResults.contains(ev.target) && ev.target !== els.searchInput) {
        els.searchResults.classList.add("hidden");
      }
    });

    const openIndex = () => els.indexModal.classList.remove("hidden");
    const closeIndex = () => els.indexModal.classList.add("hidden");
    els.btnOpenIndex.addEventListener("click", openIndex);
    els.btnEmptyIndex.addEventListener("click", openIndex);
    els.btnCloseIndex.addEventListener("click", closeIndex);
    els.btnCancelIndex.addEventListener("click", closeIndex);

    els.indexForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try {
        const res = await apiJson("/api/index", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            path: els.idxPath.value.trim() || ".",
            id: els.idxId.value.trim() || null,
            group: els.idxGroup.value.trim() || "services",
            private: els.idxPrivate.checked,
          }),
        });
        closeIndex();
        showToast("Indexed " + res.project.id + " (" + res.nodes + " nodes).", "ok");
        state.projectId = res.project.id;
        await refreshHealthAndProjects();
        await loadTopology();
      } catch (err) {
        showToast(err.message, "error");
      }
    });

    els.btnRefreshProject.addEventListener("click", async () => {
      const target = state.projectId || (state.projects[0] && state.projects[0].id);
      if (!target) {
        showToast("No registered project selected to refresh.", "error");
        return;
      }
      try {
        const res = await apiJson("/api/refresh/" + encodeURIComponent(target), { method: "POST" });
        showToast("Refreshed " + target + " (" + res.nodes + " nodes).", "ok");
        await refreshHealthAndProjects();
        await loadTopology();
      } catch (err) {
        showToast(err.message, "error");
      }
    });

    els.btnSyncGlobal.addEventListener("click", async () => {
      try {
        const res = await apiJson("/api/sync-global", { method: "POST" });
        showToast(
          "Synced global mesh: " + res.nodes + " nodes (" + res.blocked_private_projects.length + " private isolated).",
          "ok"
        );
        await refreshHealthAndProjects();
        await loadTopology();
      } catch (err) {
        showToast(err.message, "error");
      }
    });
  }

  async function init() {
    bindEvents();
    await refreshHealthAndProjects();
    await loadTopology();
  }

  init();
})();
