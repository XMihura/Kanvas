/**
 * Canvas Watcher — linter and blocked-state manager for Canvas Workflow.
 *
 * What it does:
 *   1. Auto-manages blocked state (red ↔ gray based on dependencies)
 *   2. Validates the canvas and writes status cards:
 *      - ERRORS card (red) — circular deps, orphaned edges
 *      - WARNINGS card (yellow) — non-standard naming, missing color, out-of-group tasks
 *   3. Shows "✓ None" when a list is empty (cards always visible)
 *
 * Usage:
 *   node canvas-watcher.js                        # watch mode (reacts to Obsidian auto-save)
 *   node canvas-watcher.js "Some Specific.canvas"  # one-shot
 */

const fs = require("fs");
const path = require("path");

const TASK_ID_PATTERN = /^## [A-Z]{1,3}-\d{2}/;
const TASK_ID_EXTRACT = /^## ([A-Z]{1,3})-(\d{2})/;
const ERRORS_CARD_ID = "canvas-errors";
const WARNINGS_CARD_ID = "canvas-warnings";
const LEGACY_LINT_ID = "canvas-lint";
const MANAGED_IDS = new Set([ERRORS_CARD_ID, WARNINGS_CARD_ID, LEGACY_LINT_ID]);
const COLOR = { GRAY: "0", RED: "1", ORANGE: "2", YELLOW: "3", GREEN: "4", CYAN: "5", PURPLE: "6" };
const STATUS_CARD_HEIGHT = 200;
const STATUS_CARD_GAP = 20;

let writing = false;

function isTaskCard(node) {
  return node.type === "text" && TASK_ID_PATTERN.test(node.text || "");
}

function isManagedCard(node) {
  return MANAGED_IDS.has(node.id);
}

function getDependencies(taskNodeId, edges) {
  return edges
    .filter((e) => e.toNode === taskNodeId)
    .map((e) => e.fromNode);
}

function taskLabel(node) {
  return node.text.split("\n")[0].replace("## ", "");
}

function isTaskLike(node) {
  return node.type === "text" && !isManagedCard(node) && node.id !== "legend" && (node.text || "").startsWith("## ");
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
        errors.push(`Circular dependency: ${cycle.join(" → ")} → ${cycle[0]}`);
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
      errors.push(`Orphaned edge ${e.id}: fromNode "${e.fromNode}" not found`);
    }
    if (!nodeIds.has(e.toNode)) {
      errors.push(`Orphaned edge ${e.id}: toNode "${e.toNode}" not found`);
    }
  }
  return errors;
}

function checkNaming(nodes, edges) {
  const warnings = [];
  for (const node of nodes) {
    if (!isTaskLike(node)) continue;
    const text = node.text || "";
    if (TASK_ID_PATTERN.test(text)) continue; // already has proper ID

    // Skip non-task notes: originally-gray cards with no dependency edges
    if (node.color === COLOR.GRAY && getDependencies(node.id, edges).length === 0) continue;

    const title = text.split("\n")[0].replace("## ", "");
    warnings.push(`"${title}" has no task ID`);
  }
  return warnings;
}

function checkMissingColor(nodes) {
  const warnings = [];
  for (const node of nodes) {
    if (!isTaskCard(node)) continue;
    if (!node.hasOwnProperty("color")) {
      warnings.push(`${taskLabel(node)} has no color`);
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
      warnings.push(`${taskLabel(node)} is done but depends on: ${names.join(", ")}`);
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
      warnings.push(`${taskLabel(task)} is outside all groups`);
    }
  }
  return warnings;
}

// --- Status card management ---

function findLegendCard(nodes) {
  return nodes.find((n) => n.type === "text" && (n.text || "").startsWith("## Legend"));
}

function isWorkflowCanvas(canvas) {
  const nodes = canvas.nodes || [];
  const legend = findLegendCard(nodes);
  if (!legend) return false;
  const text = legend.text || "";
  return text.includes("Red") && text.includes("Blocked");
}

function upsertStatusCard(canvas, cardId, title, items, color, slot) {
  const nodes = canvas.nodes;
  const existingIdx = nodes.findIndex((n) => n.id === cardId);

  const text = items.length === 0
    ? `## ${title}\n✓ None`
    : `## ${title}\n${items.map((w) => `- ${w}`).join("\n")}`;

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

  nodes.push({ id: cardId, type: "text", text, x, y, width: 380, height: STATUS_CARD_HEIGHT, color });
  return true;
}

// --- Blocked state management ---

function manageBlockedStates(nodes, edges) {
  const nodeMap = new Map(nodes.map((n) => [n.id, n]));
  let changed = false;
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
        log.push(`${taskLabel(node)} — unblocked (no dependencies)`);
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
      log.push(`${taskLabel(node)} — blocked by: ${blocking.join(", ")}`);
    } else if (color === COLOR.GRAY && allDepsGreen) {
      node.color = COLOR.RED;
      changed = true;
      log.push(`${taskLabel(node)} — unblocked!`);
    }
  }

  // Handle any task-like card (with or without proper ID) that has no color
  for (const node of nodes) {
    if (!isTaskLike(node)) continue;
    if (node.hasOwnProperty("color")) continue;

    const label = taskLabel(node);
    const deps = getDependencies(node.id, edges);
    if (deps.length === 0) {
      node.color = COLOR.RED;
      changed = true;
      log.push(`${label} — had no color, no deps → red`);
    } else {
      const allDepsGreen = deps.every((depId) => {
        const dep = nodeMap.get(depId);
        return dep && dep.color === COLOR.GREEN;
      });
      node.color = allDepsGreen ? COLOR.RED : COLOR.GRAY;
      changed = true;
      log.push(`${label} — had no color → ${allDepsGreen ? "red" : "gray"}`);
    }
  }

  return { changed, log };
}

// --- Main processing ---

function processCanvas(filePath) {
  let raw;
  try {
    raw = fs.readFileSync(filePath, "utf-8");
  } catch {
    return;
  }

  let canvas;
  try {
    canvas = JSON.parse(raw);
  } catch {
    console.log(`  [skip] ${path.basename(filePath)} — invalid JSON`);
    return;
  }

  // Only process Kanvas workflow canvases (must have Legend card with state keywords)
  if (!isWorkflowCanvas(canvas)) {
    console.log(`  [skip] ${path.basename(filePath)} — not a Kanvas workflow canvas`);
    return;
  }

  const nodes = canvas.nodes || [];
  const edges = canvas.edges || [];

  // 1. Manage blocked states
  const blockResult = manageBlockedStates(nodes, edges);
  for (const msg of blockResult.log) console.log(`  ${msg}`);

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

  for (const e of errors) console.log(`  [ERROR] ${e}`);
  for (const w of warnings) console.log(`  [WARN]  ${w}`);

  // 3. Remove legacy lint card if present
  const legacyIdx = nodes.findIndex((n) => n.id === LEGACY_LINT_ID);
  const legacyRemoved = legacyIdx !== -1;
  if (legacyRemoved) {
    nodes.splice(legacyIdx, 1);
    console.log("  [cleanup] Removed legacy canvas-lint card");
  }

  // 4. Update status cards
  const warnChanged = upsertStatusCard(canvas, WARNINGS_CARD_ID, "Warnings", warnings, COLOR.YELLOW, 0);
  const errChanged = upsertStatusCard(canvas, ERRORS_CARD_ID, "Errors", errors, COLOR.RED, 1);

  // 5. Save if anything changed
  if (blockResult.changed || errChanged || warnChanged || legacyRemoved) {
    writing = true;
    fs.writeFileSync(filePath, JSON.stringify(canvas, null, "\t"), "utf-8");
    console.log(`  [saved] ${path.basename(filePath)}`);
    setTimeout(() => { writing = false; }, 500);
  } else {
    console.log("  [ok] No changes needed");
  }
}

// --- Entry point ---
const targetFile = process.argv[2];
const dir = __dirname;

if (targetFile) {
  const filePath = path.resolve(dir, targetFile);
  console.log(`Processing ${path.basename(filePath)}...`);
  processCanvas(filePath);
} else {
  console.log(`Watching .canvas files in ${dir}...`);
  console.log("Press Ctrl+C to stop.\n");

  for (const file of fs.readdirSync(dir)) {
    if (file.endsWith(".canvas")) {
      console.log(`Initial scan: ${file}`);
      processCanvas(path.join(dir, file));
    }
  }

  fs.watch(dir, (eventType, filename) => {
    if (!filename || !filename.endsWith(".canvas") || writing) return;
    const filePath = path.join(dir, filename);
    setTimeout(() => {
      if (writing) return;
      console.log(`\n[${new Date().toLocaleTimeString()}] ${filename} changed`);
      processCanvas(filePath);
    }, 300);
  });
}
