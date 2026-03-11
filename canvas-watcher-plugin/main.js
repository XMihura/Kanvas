const { Plugin, Notice, TFile } = require("obsidian");

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

module.exports = class CanvasWatcherPlugin extends Plugin {

  async onload() {
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

    // Ribbon icon
    this.addRibbonIcon("shield-check", "Run Canvas Watcher", () => {
      const file = this.app.workspace.getActiveFile();
      if (!file || file.extension !== "canvas") {
        new Notice("No active canvas file.");
        return;
      }
      this.processFile(file);
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
