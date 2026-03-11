# Kanvas

A visual project board for **coordinating humans and AI agents** using [Obsidian](https://obsidian.md) Canvas.

---

## Why This Exists

AI coding agents are getting good — and they perform best inside their own sandbox CLI environments, where companies optimize their RL training. Claude Code, Codex, Gemini CLI, Aider — each runs in its own terminal, each with its own strengths.

But **who coordinates them?**

In a real project you might use Cursor or VS Code to review diffs, Claude Code or Codex to write code, and still do some tasks yourself — hardware work, manual testing, design decisions. You need a way to **plan, delegate, and track progress** — for agents *and* for yourself — in one place.

Traditional task lists fall short here: dependencies between tasks are hard to see, parallel workstreams are invisible, and you end up spending more time managing the tool than doing the work. What you really want is a visual board where you can see the whole project at a glance — what's blocked, what's in progress, what's done, and how it all connects.

Obsidian already has a powerful Canvas tool: freeform nodes, groups, arrows, colors. This project turns it into a **shared task board** where both humans and agents actively collaborate — not just following instructions, but contributing to the plan.

The board is a **two-way conversation**. You add tasks, organize groups, set priorities, and draw dependencies in Obsidian. Agents read the board, propose new tasks they think are needed, start work, and report back. Either side can shape the project — you stay in control by approving what matters and marking what's done, while agents bring structure, suggestions, and execution.

```
You (Obsidian Canvas)  ←→  canvas-tool.py  ←→  Any AI Agent
     ↕ add, organize,              ↕ propose, start,
       prioritize, review            execute, report
```

### What's in this project

1. **The workflow prompt** (`RULES.md`) — the core of the project. A structured protocol that defines how tasks move through states, how dependencies work, and what agents can and cannot do. Copy the agent instructions (`CLAUDE.md` / `AGENTS.md`) into your project and any LLM agent can follow the workflow.

2. **The CLI tool** (`canvas-tool.py`) — keeps agents honest. Instead of letting agents edit `.canvas` JSON directly (where they inevitably forget rules and break things), the CLI enforces valid transitions, dependency checks, and blocked state management. Plain Python, zero dependencies, model-agnostic.

3. **The Canvas Watcher plugin** (optional) — handles the human side. When you edit the canvas in Obsidian, the plugin automatically lints on save: updates blocked states, catches circular dependencies, flags warnings. A convenience, not a requirement.

---

## How It Works

Task cards are color-coded by state. Dependency arrows control ordering — blocked tasks turn gray automatically.

| Color  | Meaning         | Who controls it   |
|--------|----------------|--------------------|
| 🟣 Purple | Proposed by agent | Agent via `propose` |
| 🔴 Red    | To Do (ready)     | Human sets          |
| 🟠 Orange | Doing             | Agent via `start`   |
| 🔵 Cyan   | Ready to review   | Agent via `finish`  |
| 🟢 Green  | Done              | Human only          |
| ⬜ Gray   | Blocked           | Auto-managed        |

The lifecycle:

```
Agent proposes → Purple → Human approves → Red → Agent starts → Orange → Agent finishes → Cyan → Human verifies → Green
```

The human retains final authority: approving proposals, setting priorities, and marking tasks done. Tasks for yourself (hardware, manual work) follow the same flow — just skip the agent steps and move the cards yourself.

---

## Quick Start

### 1. Get the tool

Clone or copy this repo somewhere accessible from your project:

```bash
git clone https://github.com/YOUR_USER/kanvas.git
```

**Requirements:** Python 3.7+ (stdlib only, no pip install needed).

### 2. Create your first board

```bash
python canvas-tool.py "My Project.canvas" batch <<'EOF'
{
  "groups": ["Research", "Development", "Delivery"],
  "tasks": [
    {"group": "Research", "title": "Define objectives", "desc": "Clarify project goals and scope."},
    {"group": "Research", "title": "Gather requirements", "desc": "Document key needs.", "depends_on": ["Define objectives"]},
    {"group": "Development", "title": "Build prototype", "desc": "First working version.", "depends_on": ["Gather requirements"]},
    {"group": "Delivery", "title": "Prepare deliverables", "desc": "Package outputs.", "depends_on": ["Build prototype"]}
  ]
}
EOF
```

### 3. Open in Obsidian

Open the `.canvas` file in Obsidian. You'll see groups, cards, and dependency arrows. Turn the cards you want to work on (or have an agent work on) to **red** (right-click → change color).

### 4. Point your agent to the board

Copy the right instruction file into your project root and go. See [Platform Setup](#platform-setup).

---

## CLI Reference

```
python canvas-tool.py "<file>.canvas" <command> [args]
```

### Read-only

| Command | Description |
|---------|-------------|
| `status` | Board overview: groups, task counts by state |
| `show <ID>` | Full task detail with dependencies |
| `list [STATE\|GROUP]` | List tasks (all, or filtered by state/group) |
| `blocked` | Gray tasks and what blocks each one |
| `blocking` | Non-green tasks that block others |
| `ready` | Red tasks with all dependencies met |
| `dump` | Raw canvas JSON |

### Task lifecycle

| Command | Transition | Rejects if |
|---------|-----------|------------|
| `start <ID>` | Red → Orange | Not red, or has unmet deps |
| `finish <ID>` | Orange → Cyan | Not orange |
| `pause <ID>` | Orange → Red | Not orange |

### Proposals (creates purple cards)

| Command | Description |
|---------|-------------|
| `propose <GROUP> "<TITLE>" "<DESC>" [--depends-on ID ...]` | Add a single task |
| `propose-group "<LABEL>"` | Create a new group |
| `batch` | Bulk-add groups + tasks from JSON on stdin |

### Editing

| Command | Description |
|---------|-------------|
| `edit <ID> "<TEXT>"` | Update task text (must be orange) |
| `add-dep <FROM> <TO>` | Add dependency edge (rejects cycles) |
| `normalize` | Assign IDs, update blocked states |

### Intentionally excluded

- **No `delete`** — agents cannot remove cards or edges
- **No `done`/`approve`** — only humans set green
- **No raw JSON editing** — all mutations go through validated commands

---

## Platform Setup

### Claude Code

Copy `CLAUDE.md` into your project root:

```bash
cp kanvas/CLAUDE.md ./CLAUDE.md
```

Optionally auto-allow the tool in `.claude/settings.local.json`:

```json
{
  "permissions": {
    "allow": [
      "Bash(python canvas-tool.py *)"
    ]
  }
}
```

### OpenAI Codex

```bash
cp kanvas/AGENTS.md ./AGENTS.md
```

Codex reads `AGENTS.md` automatically.

### Google Gemini CLI

```bash
cp kanvas/AGENTS.md ./GEMINI.md
```

Same instructions, different filename.

### Other agents (Aider, Cursor, etc.)

Copy the content of `AGENTS.md` into whatever system prompt or instruction file your agent uses. The key rules are:

1. **Never edit `.canvas` files directly** — always use `canvas-tool.py`
2. **Read the board** with `status` at the start of each session
3. **Only work on red tasks** — `start` them first
4. **Finish tasks** with `finish` — never set green yourself

---

## Optional: Canvas Watcher Plugin

> Not required for the workflow to function. The CLI tool handles the agent side. The watcher is a quality-of-life addition for the human side.

When you edit the canvas in Obsidian (dragging cards, drawing arrows, changing colors), the watcher plugin automatically lints the board on save:

- Updates blocked states (red ↔ gray) based on dependency arrows
- Detects circular dependencies and orphaned edges
- Shows warnings for missing IDs, missing colors, tasks outside groups
- Writes status to "Errors" and "Warnings" cards on the canvas

### Install the plugin

```bash
node canvas-watcher-plugin/install.js
```

Then enable in Obsidian: **Settings → Community plugins → Canvas Watcher**.

### CLI alternative (no plugin)

If you prefer not to install the plugin, a standalone Node.js script does the same thing:

```bash
node canvas-watcher.js                         # watch mode (reacts to file changes)
node canvas-watcher.js "My Project.canvas"      # one-shot
```

---

## Project Structure

```
kanvas/
├── canvas-tool.py             # CLI tool (Python 3.7+, zero dependencies)
├── RULES.md                   # Full workflow protocol (the core prompt)
├── CLAUDE.md                  # Agent instructions for Claude Code
├── AGENTS.md                  # Agent instructions for Codex / Gemini / others
├── canvas-watcher.js          # Standalone watcher/linter (Node.js, optional)
├── canvas-watcher-plugin/     # Obsidian plugin (optional)
│   ├── main.js
│   ├── manifest.json
│   └── install.js
└── examples/
    ├── blank.canvas           # Empty board template with legend
    └── sample-project.canvas  # Example project board
```

---

## Full Documentation

See [RULES.md](RULES.md) for the complete workflow protocol: color lifecycle, agent rules, dependency logic, bootstrap procedure, and session protocol.

---

## License

MIT
