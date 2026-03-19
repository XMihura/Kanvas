const { Plugin, Notice, TFile, setIcon, addIcon } = require("obsidian");

// Custom icons: horizontal lines + down arrow (vertical layout)
//               vertical lines + right arrow (horizontal layout)
// Icons use Obsidian's 0 0 100 100 viewBox convention
// Icons use Obsidian's 0 0 100 100 viewBox convention
addIcon("dag-layout-v", `
  <line x1="8"  y1="20" x2="55" y2="20" stroke="currentColor" stroke-width="8" stroke-linecap="round"/>
  <line x1="8"  y1="50" x2="55" y2="50" stroke="currentColor" stroke-width="8" stroke-linecap="round"/>
  <line x1="8"  y1="80" x2="55" y2="80" stroke="currentColor" stroke-width="8" stroke-linecap="round"/>
  <line x1="82" y1="10" x2="82" y2="90" stroke="currentColor" stroke-width="8" stroke-linecap="round"/>
  <polyline points="68,76 82,90 96,76" fill="none" stroke="currentColor" stroke-width="8" stroke-linecap="round" stroke-linejoin="round"/>
`);
addIcon("dag-layout-h", `
  <line x1="20" y1="45" x2="20" y2="92" stroke="currentColor" stroke-width="8" stroke-linecap="round"/>
  <line x1="50" y1="45" x2="50" y2="92" stroke="currentColor" stroke-width="8" stroke-linecap="round"/>
  <line x1="80" y1="45" x2="80" y2="92" stroke="currentColor" stroke-width="8" stroke-linecap="round"/>
  <line x1="10" y1="18" x2="90" y2="18" stroke="currentColor" stroke-width="8" stroke-linecap="round"/>
  <polyline points="76,4 90,18 76,32" fill="none" stroke="currentColor" stroke-width="8" stroke-linecap="round" stroke-linejoin="round"/>
`);

// --- Constants ---
const TASK_ID_PATTERN = /^## [A-Z]{1,3}-\d{2}/;
const TASK_ID_EXTRACT = /^## ([A-Z]{1,3})-(\d{2})/;
const ERRORS_CARD_ID = "canvas-errors";
const WARNINGS_CARD_ID = "canvas-warnings";
const LEGACY_LINT_ID = "canvas-lint";
const MANAGED_IDS = new Set([ERRORS_CARD_ID, WARNINGS_CARD_ID, LEGACY_LINT_ID]);
const COLOR = { GRAY: "0", RED: "1", ORANGE: "2", YELLOW: "3", GREEN: "4", CYAN: "5", PURPLE: "6" };
const STATUS_CARD_HEIGHT = 200;
const STATUS_CARD_GAP = 20;
const DEBOUNCE_MS = 500;

// --- Helpers ---

function isTaskCard(node) {
  return node.type === "text" && TASK_ID_PATTERN.test(node.text || "");
}

function isManagedCard(node) {
  return MANAGED_IDS.has(node.id);
}

function getDependencies(taskNodeId, edges) {
  return edges.filter((e) => e.toNode === taskNodeId).map((e) => e.fromNode);
}

function taskLabel(node) {
  return node.text.split("\n")[0].replace("## ", "");
}

function isTaskLike(node) {
  return node.type === "text" && !isManagedCard(node) && node.id !== "legend" && (node.text || "").startsWith("## ");
}

function findLegendCard(nodes) {
  return nodes.find((n) => n.type === "text" && (n.text || "").startsWith("## Legend"));
}

function isWorkflowCanvas(canvas) {
  const nodes = canvas.nodes || [];
  const legend = findLegendCard(nodes);
  if (!legend) return false;
  return (legend.text || "").includes("Red") && (legend.text || "").includes("Blocked");
}

// --- Validation checks ---

function checkCircularDeps(nodes, edges) {
  const errors = [];
  const taskNodes = nodes.filter(isTaskLike);
  const nodeIds = new Set(taskNodes.map((n) => n.id));

  const adj = new Map();
  for (const e of edges) {
    if (!nodeIds.has(e.fromNode) || !nodeIds.has(e.toNode)) continue;
    if (!adj.has(e.fromNode)) adj.set(e.fromNode, []);
    adj.get(e.fromNode).push(e.toNode);
  }

  const visited = new Set();
  const inStack = new Set();
  const nodeMap = new Map(nodes.map((n) => [n.id, n]));
  const reported = new Set();

  function dfs(id, trail) {
    if (inStack.has(id)) {
      const cycleStart = trail.indexOf(id);
      const cycle = trail.slice(cycleStart).map((cid) => {
        const n = nodeMap.get(cid);
        return n ? taskLabel(n) : cid;
      });
      const key = [...cycle].sort().join(",");
      if (!reported.has(key)) {
        reported.add(key);
        errors.push("Circular dependency: " + cycle.join(" \u2192 ") + " \u2192 " + cycle[0]);
      }
      return;
    }
    if (visited.has(id)) return;
    visited.add(id);
    inStack.add(id);
    for (const next of adj.get(id) || []) {
      dfs(next, [...trail, id]);
    }
    inStack.delete(id);
  }

  for (const n of taskNodes) {
    if (!visited.has(n.id)) dfs(n.id, []);
  }
  return errors;
}

function checkOrphanedEdges(nodes, edges) {
  const errors = [];
  const nodeIds = new Set(nodes.map((n) => n.id));
  for (const e of edges) {
    if (!nodeIds.has(e.fromNode)) {
      errors.push("Orphaned edge " + e.id + ': fromNode "' + e.fromNode + '" not found');
    }
    if (!nodeIds.has(e.toNode)) {
      errors.push("Orphaned edge " + e.id + ': toNode "' + e.toNode + '" not found');
    }
  }
  return errors;
}

function checkNaming(nodes, edges) {
  const warnings = [];
  for (const node of nodes) {
    if (!isTaskLike(node)) continue;
    const text = node.text || "";
    if (TASK_ID_PATTERN.test(text)) continue;
    if (node.color === COLOR.GRAY && getDependencies(node.id, edges).length === 0) continue;
    const title = text.split("\n")[0].replace("## ", "");
    warnings.push('"' + title + '" has no task ID');
  }
  return warnings;
}

function checkMissingColor(nodes) {
  const warnings = [];
  for (const node of nodes) {
    if (!isTaskCard(node)) continue;
    if (!node.hasOwnProperty("color")) {
      warnings.push(taskLabel(node) + " has no color");
    }
  }
  return warnings;
}

function checkDoneWithPendingDeps(nodes, edges) {
  const warnings = [];
  const nodeMap = new Map(nodes.map((n) => [n.id, n]));
  for (const node of nodes) {
    if (!isTaskCard(node)) continue;
    if (node.color !== COLOR.GREEN) continue;
    const deps = getDependencies(node.id, edges);
    const pending = deps.filter((id) => {
      const d = nodeMap.get(id);
      return d && d.color !== COLOR.GREEN;
    });
    if (pending.length > 0) {
      const names = pending.map((id) => { const d = nodeMap.get(id); return d ? taskLabel(d) : id; });
      warnings.push(taskLabel(node) + " is done but depends on: " + names.join(", "));
    }
  }
  return warnings;
}

function checkGroupMembership(nodes) {
  const warnings = [];
  const groups = nodes.filter((n) => n.type === "group");
  const tasks = nodes.filter(isTaskCard);
  for (const task of tasks) {
    const match = TASK_ID_EXTRACT.exec(task.text);
    if (!match) continue;
    const inside = groups.some((g) =>
      task.x >= g.x && task.y >= g.y &&
      task.x + task.width <= g.x + g.width &&
      task.y + task.height <= g.y + g.height
    );
    if (!inside) {
      warnings.push(taskLabel(task) + " is outside all groups");
    }
  }
  return warnings;
}

// --- Status card management ---

function upsertStatusCard(canvas, cardId, title, items, color, slot) {
  const nodes = canvas.nodes;
  const existingIdx = nodes.findIndex((n) => n.id === cardId);

  const text = items.length === 0
    ? "## " + title + "\n\u2713 None"
    : "## " + title + "\n" + items.map((w) => "- " + w).join("\n");

  const legend = findLegendCard(nodes);
  const x = legend ? legend.x : -600;
  const baseY = legend ? legend.y : -500;
  const y = baseY - (STATUS_CARD_HEIGHT + STATUS_CARD_GAP) * (slot + 1);

  if (existingIdx !== -1) {
    const card = nodes[existingIdx];
    if (card.text === text && card.x === x && card.y === y) return false;
    card.text = text;
    card.x = x;
    card.y = y;
    card.height = STATUS_CARD_HEIGHT;
    return true;
  }

  nodes.push({ id: cardId, type: "text", text: text, x: x, y: y, width: 380, height: STATUS_CARD_HEIGHT, color: color });
  return true;
}

// --- Blocked state management ---

function manageBlockedStates(nodes, edges) {
  const nodeMap = new Map(nodes.map((n) => [n.id, n]));
  var changed = false;
  const log = [];

  for (const node of nodes) {
    if (!isTaskLike(node)) continue;

    const color = node.color || "0";
    if (color !== COLOR.RED && color !== COLOR.GRAY) continue;

    const deps = getDependencies(node.id, edges);
    if (deps.length === 0) {
      if (color === COLOR.GRAY) {
        node.color = COLOR.RED;
        changed = true;
        log.push(taskLabel(node) + " \u2014 unblocked (no dependencies)");
      }
      continue;
    }

    const allDepsGreen = deps.every((depId) => {
      const dep = nodeMap.get(depId);
      return dep && dep.color === COLOR.GREEN;
    });

    if (color === COLOR.RED && !allDepsGreen) {
      node.color = COLOR.GRAY;
      changed = true;
      const blocking = deps
        .filter((id) => { const d = nodeMap.get(id); return d && d.color !== COLOR.GREEN; })
        .map((id) => { const d = nodeMap.get(id); return d ? taskLabel(d) : id; });
      log.push(taskLabel(node) + " \u2014 blocked by: " + blocking.join(", "));
    } else if (color === COLOR.GRAY && allDepsGreen) {
      node.color = COLOR.RED;
      changed = true;
      log.push(taskLabel(node) + " \u2014 unblocked!");
    }
  }

  // Handle task-like cards with no color
  for (const node of nodes) {
    if (!isTaskLike(node)) continue;
    if (node.hasOwnProperty("color")) continue;

    const label = taskLabel(node);
    const deps = getDependencies(node.id, edges);
    if (deps.length === 0) {
      node.color = COLOR.RED;
      changed = true;
      log.push(label + " \u2014 had no color, no deps \u2192 red");
    } else {
      const allDepsGreen = deps.every((depId) => {
        const dep = nodeMap.get(depId);
        return dep && dep.color === COLOR.GREEN;
      });
      node.color = allDepsGreen ? COLOR.RED : COLOR.GRAY;
      changed = true;
      log.push(label + " \u2014 had no color \u2192 " + (allDepsGreen ? "red" : "gray"));
    }
  }

  return { changed: changed, log: log };
}

// --- DAG Layout Engine ---
//
// Self-contained port of organize-dag-groups.py.
// Call DagLayout.organize(canvas, { horizontal, layerGap, rowGap }) → new canvas.

const DagLayout = (() => {

  // ── Constants ──────────────────────────────────────────────────────────────

  const ROW_GAP   = 70;
  const LAYER_GAP = 250;
  const GROUP_GAP = 200;
  const MARGIN    = 80;
  const PAD_X     = 60;
  const PAD_TOP   = 40;
  const PAD_BOT   = 40;

  const TASK_RE   = /^## [A-Z]{1,3}-\d{2}/;
  const SKIP_IDS  = new Set(["canvas-errors", "canvas-warnings", "canvas-lint", "legend"]);

  // ── Tiny helpers ───────────────────────────────────────────────────────────

  const avg = arr => arr.reduce((s, x) => s + x, 0) / arr.length;

  const isTask = n => n.type === "text" && !SKIP_IDS.has(n.id) && TASK_RE.test(n.text || "");

  // ── Group membership (geometric centre-point containment) ──────────────────

  function inferMembership(canvas, taskIds) {
    const groups = (canvas.nodes || []).filter(n => n.type === "group");
    const membership = {};
    for (const n of (canvas.nodes || [])) {
      if (!taskIds.has(n.id)) continue;
      const cx = n.x + n.width  / 2;
      const cy = n.y + n.height / 2;
      let best = null, bestArea = Infinity;
      for (const g of groups) {
        if (cx >= g.x && cx <= g.x + g.width && cy >= g.y && cy <= g.y + g.height) {
          const area = g.width * g.height;
          if (area < bestArea) { best = g; bestArea = area; }
        }
      }
      if (best) membership[n.id] = best.id;
    }
    return membership;
  }

  // ── Graph ──────────────────────────────────────────────────────────────────

  function buildGraph(canvas, taskIds) {
    const incoming = {}, outgoing = {};
    for (const id of taskIds) { incoming[id] = new Set(); outgoing[id] = new Set(); }
    for (const e of (canvas.edges || [])) {
      if (taskIds.has(e.fromNode) && taskIds.has(e.toNode)) {
        outgoing[e.fromNode].add(e.toNode);
        incoming[e.toNode].add(e.fromNode);
      }
    }
    return { incoming, outgoing };
  }

  function computeDepths(taskIds, incoming, outgoing) {
    const depths   = {}, indegree = {};
    for (const id of taskIds) { depths[id] = 0; indegree[id] = incoming[id].size; }
    const queue    = [...taskIds].filter(id => indegree[id] === 0);
    let   visited  = 0;
    while (queue.length) {
      const id = queue.shift();
      visited++;
      for (const child of outgoing[id]) {
        depths[child] = Math.max(depths[child], depths[id] + 1);
        if (--indegree[child] === 0) queue.push(child);
      }
    }
    if (visited !== taskIds.size) throw new Error("Cycle detected — cannot compute depths.");
    return depths;
  }

  function transitiveReduction(taskIds, outgoing) {
    const reducedOut = {};
    for (const id of taskIds) {
      const indirect = new Set();
      for (const child of outgoing[id]) {
        const stack = [...outgoing[child]];
        while (stack.length) {
          const node = stack.pop();
          if (!indirect.has(node)) { indirect.add(node); stack.push(...outgoing[node]); }
        }
      }
      reducedOut[id] = new Set([...outgoing[id]].filter(c => !indirect.has(c)));
    }
    const reducedIn = {};
    for (const id of taskIds) reducedIn[id] = new Set();
    for (const [id, children] of Object.entries(reducedOut)) {
      for (const child of children) reducedIn[child].add(id);
    }
    return { reducedOut, reducedIn };
  }

  // ── Card sizing ────────────────────────────────────────────────────────────

  function estimateSize(node) {
    const lines   = (node.text || "").split("\n");
    const longest = Math.max(...lines.map(l => l.length), 0);
    return [
      Math.max(260, Math.min(420, Math.floor(longest * 8) + 52)),
      Math.max(180, lines.length * 24 + 60),
    ];
  }

  function uniformSizes(tasks) {
    const raw  = Object.fromEntries(tasks.map(t => [t.id, estimateSize(t)]));
    const maxW = Math.max(...Object.values(raw).map(([w]) => w));
    return Object.fromEntries(Object.entries(raw).map(([id, [, h]]) => [id, [maxW, h]]));
  }

  // ── Layout engine ──────────────────────────────────────────────────────────

  function layoutRows(tasks, depths, incoming, outgoing, membership, opts = {}) {
    const {
      rowGap   = ROW_GAP,
      layerGap = LAYER_GAP,
      groupGap = GROUP_GAP,
      marginX  = MARGIN,
      marginY  = MARGIN,
      horizontal = false,
    } = opts;

    // Group cards by depth, sorted by text label for determinism
    const rows = {};
    for (const t of tasks) {
      const d = depths[t.id];
      (rows[d] || (rows[d] = [])).push(t);
    }
    const sortedDepths = Object.keys(rows).map(Number).sort((a, b) => a - b);
    for (const d of sortedDepths) rows[d].sort((a, b) => (a.text || "").localeCompare(b.text || ""));

    // In horizontal mode swap w/h so spacing math stays axis-agnostic
    let sizes = uniformSizes(tasks);
    if (horizontal) sizes = Object.fromEntries(Object.entries(sizes).map(([id, [w, h]]) => [id, [h, w]]));

    const hasMembership = Object.keys(membership).length > 0;

    // ── Group conflict coloring ──────────────────────────────────────────────
    // Two groups conflict when their depth RANGES overlap (bounding boxes would collide).
    // Greedy coloring → non-conflicting groups share an x-column.

    const groupDepthSet = {};
    for (const t of tasks) {
      const gid = membership[t.id];
      if (gid != null) (groupDepthSet[gid] || (groupDepthSet[gid] = new Set())).add(depths[t.id]);
    }

    const groupOrder = [];
    for (const d of sortedDepths) {
      for (const t of rows[d]) {
        const gid = membership[t.id];
        if (gid != null && !groupOrder.includes(gid)) groupOrder.push(gid);
      }
    }

    const depthRange = Object.fromEntries(
      Object.entries(groupDepthSet).map(([gid, ds]) => [gid, [Math.min(...ds), Math.max(...ds)]])
    );

    const conflicts = {};
    for (let i = 0; i < groupOrder.length; i++) {
      const ga = groupOrder[i], [aLo, aHi] = depthRange[ga];
      for (let j = i + 1; j < groupOrder.length; j++) {
        const gb = groupOrder[j], [bLo, bHi] = depthRange[gb];
        if (aLo <= bHi && bLo <= aHi) {
          (conflicts[ga] || (conflicts[ga] = new Set())).add(gb);
          (conflicts[gb] || (conflicts[gb] = new Set())).add(ga);
        }
      }
    }

    const groupColor = {};
    for (const gid of groupOrder) {
      const used = new Set([...(conflicts[gid] || [])].filter(nb => nb in groupColor).map(nb => groupColor[nb]));
      let color = 0;
      while (used.has(color)) color++;
      groupColor[gid] = color;
    }

    const numCols      = groupOrder.length ? Math.max(...Object.values(groupColor)) + 1 : 0;
    const UNGROUPED    = numCols;
    const colOf        = id => { const gid = membership[id]; return gid != null ? groupColor[gid] : UNGROUPED; };

    // Column widths: widest row that appears in each column
    const colRowWidths = {};
    for (const d of sortedDepths) {
      const colCards = {};
      for (const t of rows[d]) (colCards[colOf(t.id)] || (colCards[colOf(t.id)] = [])).push(t.id);
      for (const [col, tids] of Object.entries(colCards)) {
        const c   = Number(col);
        const row = tids.reduce((s, id) => s + sizes[id][0], 0) + rowGap * Math.max(0, tids.length - 1);
        (colRowWidths[c] || (colRowWidths[c] = {}))[d] = row;
      }
    }

    const colWidths = {};
    for (const t of tasks) {
      const c = colOf(t.id);
      colWidths[c] = Math.max(colWidths[c] ?? 0, ...Object.values(colRowWidths[c] || { _: 0 }));
    }

    const colXStart = {};
    let xCursor = marginX;
    for (const col of Object.keys(colWidths).map(Number).sort((a, b) => a - b)) {
      colXStart[col] = xCursor;
      xCursor += colWidths[col] + groupGap;
    }

    // ── Card placement helpers ───────────────────────────────────────────────

    const xPos       = {};
    const cardCenter = id => xPos[id] + sizes[id][0] / 2;

    const placeRowInColumn = (col, tids, targets) => {
      const sorted   = [...tids].sort((a, b) => (targets[a] ?? 0) - (targets[b] ?? 0));
      const colStart = colXStart[col];
      let x          = colStart;
      for (const id of sorted) {
        const w    = sizes[id][0];
        const tgt  = targets[id] ?? (x + w / 2);
        const left = Math.max(x, Math.max(colStart, Math.floor(tgt - w / 2)));
        xPos[id]   = left;
        x          = left + w + rowGap;
      }
      return sorted;
    };

    const parentTargets = depth => {
      const targets = {};
      for (const t of rows[depth]) {
        const myCol   = colOf(t.id);
        const parents = [...(incoming[t.id] || [])].filter(p => p in xPos && colOf(p) === myCol);
        targets[t.id] = parents.length
          ? avg(parents.map(cardCenter))
          : (xPos[t.id] ?? (colXStart[myCol] ?? marginX)) + sizes[t.id][0] / 2;
      }
      return targets;
    };

    const childTargets = depth => {
      const targets = {};
      for (const t of rows[depth]) {
        const myCol    = colOf(t.id);
        const children = [...(outgoing[t.id] || [])].filter(c => c in xPos && colOf(c) === myCol);
        targets[t.id]  = children.length ? avg(children.map(cardCenter)) : cardCenter(t.id);
      }
      return targets;
    };

    const placeDepth = (depth, targets) => {
      const colCards = {};
      for (const t of rows[depth]) (colCards[colOf(t.id)] || (colCards[colOf(t.id)] = [])).push(t.id);
      const orderIds = [];
      for (const col of Object.keys(colCards).map(Number).sort((a, b) => a - b))
        orderIds.push(...placeRowInColumn(col, colCards[col], targets));
      return orderIds;
    };

    // ── Passes ───────────────────────────────────────────────────────────────

    // 1. Initial top-down
    const order = {};
    for (const depth of sortedDepths) {
      const targets = depth === sortedDepths[0]
        ? Object.fromEntries(rows[depth].map(t => [t.id, (colXStart[colOf(t.id)] ?? marginX) + sizes[t.id][0] / 2]))
        : parentTargets(depth);
      order[depth] = placeDepth(depth, targets);
    }

    // 2. Four alternating bottom-up / top-down refinement sweeps
    for (let pass = 0; pass < 4; pass++) {
      for (const d of [...sortedDepths].reverse().slice(1))  order[d] = placeDepth(d, childTargets(d));
      for (const d of sortedDepths.slice(1))                 order[d] = placeDepth(d, parentTargets(d));
    }

    // 3. Compact: snap leftmost card in each col×depth to colXStart
    if (hasMembership) {
      for (const depth of sortedDepths) {
        const colCards = {};
        for (const t of rows[depth]) (colCards[colOf(t.id)] || (colCards[colOf(t.id)] = [])).push(t.id);
        for (const [col, tids] of Object.entries(colCards)) {
          const shift = Math.min(...tids.map(id => xPos[id])) - colXStart[Number(col)];
          if (shift !== 0) tids.forEach(id => { xPos[id] -= shift; });
        }
      }
    } else {
      const shift = Math.min(...Object.values(xPos)) - marginX;
      if (shift !== 0) Object.keys(xPos).forEach(id => { xPos[id] -= shift; });
    }

    // 4. Final top-down: restore parent–child alignment broken by compaction.
    //    Singletons (alone in col×depth) with no same-col parent align to group centroid.
    const parentTargetsFinal = depth => {
      const slotCounts = {};
      for (const t of rows[depth]) slotCounts[colOf(t.id)] = (slotCounts[colOf(t.id)] || 0) + 1;

      const targets = {};
      for (const t of rows[depth]) {
        const myCol   = colOf(t.id);
        const parents = [...(incoming[t.id] || [])].filter(p => p in xPos && colOf(p) === myCol);

        if (parents.length) {
          targets[t.id] = avg(parents.map(cardCenter));
          // fall through — last line overwrites with current x_pos (matches Python semantics)
        } else if (hasMembership && slotCounts[myCol] === 1) {
          const gid       = membership[t.id];
          const sameGroup = Object.keys(xPos).filter(o => membership[o] === gid && o !== t.id);
          if (sameGroup.length) {
            targets[t.id] = avg(sameGroup.map(cardCenter));
            continue;  // only cohesion case skips the fallback
          }
        }

        targets[t.id] = (xPos[t.id] ?? (colXStart[myCol] ?? marginX)) + sizes[t.id][0] / 2;
      }
      return targets;
    };

    for (const depth of sortedDepths.slice(1)) order[depth] = placeDepth(depth, parentTargetsFinal(depth));

    // ── Build final positions with y ─────────────────────────────────────────

    const realSizes = uniformSizes(tasks);   // un-swapped sizes for final output
    const positions = {};
    let yCursor = marginY;
    for (const depth of sortedDepths) {
      const rowIds = order[depth];
      const rowH   = Math.max(...rowIds.map(id => sizes[id][1]));
      for (const id of rowIds) {
        const [w, h] = realSizes[id];
        positions[id] = [xPos[id], yCursor + Math.floor((rowH - sizes[id][1]) / 2), w, h];
      }
      yCursor += rowH + layerGap;
    }

    return { positions, groupColor };
  }

  // ── Group bounding boxes ───────────────────────────────────────────────────

  function computeGroupBounds(positions, membership) {
    const members = {};
    for (const [tid, gid] of Object.entries(membership)) {
      if (tid in positions) (members[gid] || (members[gid] = [])).push(tid);
    }
    const bounds = {};
    for (const [gid, tids] of Object.entries(members)) {
      const minX = Math.min(...tids.map(id => positions[id][0]));
      const minY = Math.min(...tids.map(id => positions[id][1]));
      const maxX = Math.max(...tids.map(id => positions[id][0] + positions[id][2]));
      const maxY = Math.max(...tids.map(id => positions[id][1] + positions[id][3]));
      bounds[gid] = [minX - PAD_X, minY - PAD_TOP, (maxX - minX) + 2 * PAD_X, (maxY - minY) + PAD_TOP + PAD_BOT];
    }
    return bounds;
  }

  // ── Apply layout ───────────────────────────────────────────────────────────

  function applyLayout(canvas, positions, reducedOut, membership, horizontal) {
    const nodes   = JSON.parse(JSON.stringify(canvas.nodes || []));
    const edges   = JSON.parse(JSON.stringify(canvas.edges || []));
    const taskIds = new Set(Object.keys(positions));

    // Transpose axes for horizontal mode: layout runs top-to-bottom, then swap x↔y
    const placed = horizontal
      ? Object.fromEntries(Object.entries(positions).map(([id, [x, y, w, h]]) => [id, [y, x, w, h]]))
      : positions;

    // Group bounds computed AFTER transpose so padding is applied correctly
    const groupBounds = computeGroupBounds(placed, membership);

    // Update or drop group nodes
    const newNodes = nodes.filter(n => {
      if (n.type !== "group") return true;
      if (!(n.id in groupBounds)) return false;
      const [x, y, w, h] = groupBounds[n.id];
      n.x = Math.round(x);  n.y = Math.round(y);
      n.width = Math.round(w); n.height = Math.round(h);
      return true;
    });

    // Apply task positions
    for (const n of newNodes) {
      if (!taskIds.has(n.id)) continue;
      const [x, y, w, h] = placed[n.id];
      n.x = Math.round(x);  n.y = Math.round(y);
      n.width = Math.round(w); n.height = Math.round(h);
    }

    // Retain only essential edges (transitive reduction) that reference live nodes
    const validIds = new Set(newNodes.map(n => n.id));
    const fromSide = horizontal ? "right" : "bottom";
    const toSide   = horizontal ? "left"  : "top";
    const newEdges = edges.filter(e =>
      validIds.has(e.fromNode) && validIds.has(e.toNode) &&
      (reducedOut[e.fromNode] || new Set()).has(e.toNode)
    );
    for (const e of newEdges) { e.fromSide = fromSide; e.toSide = toSide; }

    return { ...canvas, nodes: newNodes, edges: newEdges };
  }

  // ── Public entry point ─────────────────────────────────────────────────────

  function organize(canvas, { horizontal = false, layerGap = LAYER_GAP, rowGap = ROW_GAP } = {}) {
    const tasks = (canvas.nodes || []).filter(isTask);
    if (!tasks.length) return canvas;

    const taskIds                  = new Set(tasks.map(t => t.id));
    const { incoming, outgoing }   = buildGraph(canvas, taskIds);
    const depths                   = computeDepths(taskIds, incoming, outgoing);
    const { reducedOut, reducedIn } = transitiveReduction(taskIds, outgoing);
    const membership               = inferMembership(canvas, taskIds);

    const { positions } = layoutRows(tasks, depths, reducedIn, reducedOut, membership,
      { rowGap, layerGap, horizontal });

    return applyLayout(canvas, positions, reducedOut, membership, horizontal);
  }

  return { organize };

})();

// --- Core processing (shared between CLI and plugin) ---

function processCanvasData(canvas) {
  const nodes = canvas.nodes || [];
  const edges = canvas.edges || [];

  // 1. Manage blocked states
  const blockResult = manageBlockedStates(nodes, edges);

  // 2. Run validations
  const errors = [
    ...checkCircularDeps(nodes, edges),
    ...checkOrphanedEdges(nodes, edges),
  ];

  const warnings = [
    ...checkNaming(nodes, edges),
    ...checkMissingColor(nodes),
    ...checkGroupMembership(nodes),
    ...checkDoneWithPendingDeps(nodes, edges),
  ];

  // 3. Remove legacy lint card if present
  const legacyIdx = nodes.findIndex((n) => n.id === LEGACY_LINT_ID);
  const legacyRemoved = legacyIdx !== -1;
  if (legacyRemoved) {
    nodes.splice(legacyIdx, 1);
  }

  // 4. Update status cards
  const warnChanged = upsertStatusCard(canvas, WARNINGS_CARD_ID, "Warnings", warnings, COLOR.YELLOW, 0);
  const errChanged = upsertStatusCard(canvas, ERRORS_CARD_ID, "Errors", errors, COLOR.RED, 1);

  const changed = blockResult.changed || errChanged || warnChanged || legacyRemoved;

  return {
    changed: changed,
    errors: errors,
    warnings: warnings,
    log: blockResult.log,
  };
}

// --- Obsidian Plugin ---

const DEFAULT_SETTINGS = { layerGap: 250 };

module.exports = class CanvasWatcherPlugin extends Plugin {

  async onload() {
    await this.loadSettings();
    this._writing = false;
    this._debounceTimers = new Map();

    // Auto-run on canvas file modifications
    this.registerEvent(
      this.app.vault.on("modify", (file) => {
        if (!(file instanceof TFile)) return;
        if (file.extension !== "canvas") return;
        if (this._writing) return;

        // Debounce: wait for rapid saves to settle
        const existing = this._debounceTimers.get(file.path);
        if (existing) clearTimeout(existing);

        this._debounceTimers.set(file.path, setTimeout(() => {
          this._debounceTimers.delete(file.path);
          if (!this._writing) {
            this.processFile(file);
          }
        }, DEBOUNCE_MS));
      })
    );

    // Command: run on active canvas
    this.addCommand({
      id: "run-canvas-watcher",
      name: "Run Canvas Watcher on active file",
      callback: () => {
        const file = this.app.workspace.getActiveFile();
        if (!file || file.extension !== "canvas") {
          new Notice("No active canvas file.");
          return;
        }
        this.processFile(file);
      },
    });

    // Ribbon icon — validator
    this.addRibbonIcon("shield-check", "Run Canvas Watcher", () => {
      const file = this.app.workspace.getActiveFile();
      if (!file || file.extension !== "canvas") {
        new Notice("No active canvas file.");
        return;
      }
      this.processFile(file);
    });

    // Inject layout buttons into the canvas toolbar whenever a canvas becomes active
    this.registerEvent(
      this.app.workspace.on("active-leaf-change", (leaf) => {
        if (leaf?.view?.getViewType() === "canvas") this.injectCanvasButtons(leaf.view);
      })
    );

    // Also inject into any canvas already open when the plugin loads
    this.app.workspace.getLeavesOfType("canvas").forEach(leaf => {
      this.injectCanvasButtons(leaf.view);
    });

    // Commands
    this.addCommand({
      id: "layout-canvas-vertical",
      name: "Organize canvas layout (vertical)",
      callback: () => {
        const file = this.app.workspace.getActiveFile();
        if (!file || file.extension !== "canvas") { new Notice("No active canvas file."); return; }
        this.layoutCanvas(file, false);
      },
    });

    this.addCommand({
      id: "layout-canvas-horizontal",
      name: "Organize canvas layout (horizontal)",
      callback: () => {
        const file = this.app.workspace.getActiveFile();
        if (!file || file.extension !== "canvas") { new Notice("No active canvas file."); return; }
        this.layoutCanvas(file, true);
      },
    });

    console.log("Canvas Watcher plugin loaded");
  }

  onunload() {
    for (const timer of this._debounceTimers.values()) {
      clearTimeout(timer);
    }
    this._debounceTimers.clear();
    console.log("Canvas Watcher plugin unloaded");
  }

  injectCanvasButtons(view) {
    const controls = view.containerEl.querySelector(".canvas-controls");
    if (!controls || controls.querySelector(".canvas-watcher-layout-btn")) return;

    const group = controls.createDiv("canvas-controls-group");

    const btnV = group.createEl("button", {
      cls: "canvas-control-btn clickable-icon canvas-watcher-layout-btn",
      attr: { "aria-label": "Organize layout (vertical)", "data-tooltip-position": "left" },
    });
    setIcon(btnV, "dag-layout-v");
    btnV.onclick = () => {
      const file = this.app.workspace.getActiveFile();
      if (file) this.layoutCanvas(file, false);
    };

    const btnH = group.createEl("button", {
      cls: "canvas-control-btn clickable-icon",
      attr: { "aria-label": "Organize layout (horizontal)", "data-tooltip-position": "left" },
    });
    setIcon(btnH, "dag-layout-h");
    btnH.onclick = () => {
      const file = this.app.workspace.getActiveFile();
      if (file) this.layoutCanvas(file, true);
    };

    // Layer-gap slider (vertical, below the layout buttons)
    const sliderWrap = group.createDiv();
    sliderWrap.style.cssText = "display:flex; align-items:center; justify-content:center; padding:4px 0 2px;";

    // Wrapper holds the rotated slider's visual space (72×20 becomes 20×72 after rotation)
    const sliderBox = sliderWrap.createDiv();
    sliderBox.style.cssText = "width:20px; height:72px; display:flex; align-items:center; justify-content:center; overflow:visible;";

    const slider = sliderBox.createEl("input");
    slider.type  = "range";
    slider.min   = "150";
    slider.max   = "600";
    slider.step  = "25";
    slider.value = String(750 - this.settings.layerGap); // inverted: down=more, up=less
    slider.setAttribute("aria-label", "Spacing between dependency levels");
    slider.setAttribute("data-tooltip-position", "left");
    slider.style.cssText = "width:72px; height:20px; transform:rotate(-90deg); cursor:pointer; margin:0; padding:0;";

    slider.oninput = () => {
      this.settings.layerGap = 750 - parseInt(slider.value);
      this.saveSettings();
    };
  }

  async layoutCanvas(file, horizontal) {
    try {
      const raw    = await this.app.vault.read(file);
      const canvas = JSON.parse(raw);
      const result = DagLayout.organize(canvas, { horizontal, layerGap: this.settings.layerGap });
      this._writing = true;
      await this.app.vault.modify(file, JSON.stringify(result, null, "\t"));
      setTimeout(() => { this._writing = false; }, DEBOUNCE_MS);
      new Notice(`Canvas Watcher: layout applied (${horizontal ? "horizontal" : "vertical"})`);
    } catch (err) {
      console.error("Canvas Watcher layout error:", err);
      new Notice("Canvas Watcher layout error: " + err.message);
    }
  }

  async loadSettings() {
    this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
  }

  async saveSettings() {
    await this.saveData(this.settings);
  }

  async processFile(file) {
    try {
      const raw = await this.app.vault.read(file);

      var canvas;
      try {
        canvas = JSON.parse(raw);
      } catch (e) {
        return; // not valid JSON, skip silently
      }

      // Only process workflow canvases (must have legend card)
      if (!isWorkflowCanvas(canvas)) return;

      const result = processCanvasData(canvas);

      if (result.changed) {
        this._writing = true;
        await this.app.vault.modify(file, JSON.stringify(canvas, null, "\t"));
        // Give Obsidian time to process the write before re-enabling
        setTimeout(() => { this._writing = false; }, DEBOUNCE_MS);
      }

      // Show notification summary
      var parts = [];
      if (result.errors.length > 0) {
        parts.push(result.errors.length + " error(s)");
      }
      if (result.warnings.length > 0) {
        parts.push(result.warnings.length + " warning(s)");
      }
      for (const msg of result.log) {
        parts.push(msg);
      }

      if (parts.length > 0) {
        new Notice("Canvas Watcher: " + file.basename + "\n" + parts.join("\n"), 5000);
      }
    } catch (err) {
      console.error("Canvas Watcher error:", err);
      new Notice("Canvas Watcher error: " + err.message);
    }
  }
};
