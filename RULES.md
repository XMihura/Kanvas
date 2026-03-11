# Canvas Workflow Rules

A collaborative task management protocol between a human and an AI agent using an Obsidian Canvas file as a shared task board.

---

## CRITICAL: Agents Must Use the CLI Tool

**Agents must NEVER edit `.canvas` files directly.** All canvas modifications go through the `canvas-tool.py` CLI, which enforces workflow rules and prevents invalid state changes.

```bash
python canvas-tool.py "<file>.canvas" <command> [args]
```

The tool is the **only** way agents interact with the canvas. It handles ID generation, placement, dependency validation, blocked state management, and all color transitions. Direct JSON editing is forbidden — the tool enforces the rules so the agent doesn't have to remember them.

See the [CLI Tool Reference](#cli-tool-reference) section for the full command list.

---

## How It Works

The canvas contains **task cards** (nodes) organized in **groups** (areas) and connected by **dependency arrows** (edges). Both the human and the agent can add and modify cards, but with different permissions. The human retains final authority over what gets approved and what counts as done.

At the start of each session, the human either points the agent to an existing `.canvas` file or the agent runs the **Project Bootstrap** to create one.

---

## Color Codes

Obsidian canvas color values and their meaning:

| Color  | Value | Meaning                                                        | Who controls it            |
| ------ | ----- | -------------------------------------------------------------- | -------------------------- |
| Gray   | `"0"` | **Blocked** — waiting on dependencies that aren't done yet     | Tool auto-manages          |
| Purple | `"6"` | **Proposed** — suggested by the agent, awaiting human approval | Agent via `propose`/`batch`|
| Red    | `"1"` | **To Do** — approved and ready to be worked on                 | Human sets                 |
| Orange | `"2"` | **Doing** — actively being worked on                           | Agent via `start`          |
| Cyan   | `"5"` | **Review** — agent finished, awaiting human verification       | Agent via `finish`         |
| Green  | `"4"` | **Done** — completed and verified                              | Human only                 |

### Color lifecycle
```
[Agent proposes] → Purple → [Human approves] → Red → [Agent starts] → Orange → [Agent finishes] → Cyan → [Human verifies] → Green
                      ↓                          ↓
               [Human deletes]          [Tool detects blocked] → Gray → [Dependencies met] → Red
```

### Non-task cards
Cards like legends and status cards use `"0"` (or no color). The tool distinguishes them from blocked tasks by content: **non-task cards don't have a `## XX-NN` task ID pattern** in their text.

### Rejecting proposals
To reject a purple card, the human simply deletes it. The agent may re-propose something similar in a future session — the human can delete it again. This is by design: simplicity over ceremony.

---

## Groups and Task IDs

### Groups
Groups represent **project areas** (e.g., Research, Development, Delivery). They are visual containers on the canvas. The agent creates groups via `propose-group` or `batch`.

### Task IDs
Each task gets a short ID based on its group: `XX-NN` (e.g., `RS-01`, `DV-03`). The tool auto-assigns IDs — the agent never needs to compute them manually.

The **area prefix** is derived from the group label:
- **1 letter** if unambiguous (e.g., `D` for Design)
- **2-3 letters** when needed (e.g., `RS` for Research, `RP` for Report Writing)
- Multi-word labels use initials (e.g., `RW` for Report Writing)
- Tasks outside any group use `G` (general)

The **number** is 2-digit zero-padded (`01`, `02`, ..., `99`).

### How the human adds tasks
The human can:
1. Create a card in Obsidian with a title (and optionally a description)
2. Drag it into the appropriate group
3. Set the color to red
4. Optionally draw arrows to dependencies

**No ID or formatting needed.** Running `normalize` handles standardization.

---

## Agent Rules

### The agent CAN (via CLI tool):
- **Read** the board: `status`, `show`, `list`, `blocked`, `blocking`, `ready`, `dump`
- **Normalize** the board: `normalize` (assign IDs, update blocked states)
- **Propose tasks**: `propose` or `batch` (creates purple cards with dependencies)
- **Propose groups**: `propose-group` or `batch`
- **Start a task**: `start <TASK-ID>` (red → orange)
- **Finish a task**: `finish <TASK-ID>` (orange → cyan)
- **Pause a task**: `pause <TASK-ID>` (orange → red)
- **Edit task text**: `edit <TASK-ID> "<text>"` (only orange/in-progress tasks)
- **Add dependencies**: `add-dep <FROM> <TO>` (with cycle detection)

### The agent CANNOT:
- **Edit `.canvas` files directly** — always use the CLI tool
- **Mark any card as green** — only the human marks tasks as done
- **Work on purple cards** — they are proposals, not approved tasks
- **Work on gray cards** — they are blocked
- **Work on cyan cards** — they are awaiting human review
- **Remove cards or edges** created by the human
- **Change green cards** — done is done

The tool enforces all of these rules. Invalid operations return an error and exit code 1.

### Requesting completion
When the agent finishes work on an orange task:
1. Run `finish <TASK-ID>` (changes orange → cyan)
2. Inform the human what was done
3. The human reviews and marks cyan → green (or back to red for rework)

---

## Edge Conventions

Edges encode dependencies: **`fromNode` is the dependency (blocker), `toNode` is the dependent (blocked)**.

Reading an edge: "Task A (`fromNode`) blocks Task B (`toNode`)" — meaning A must be done before B can start.

A task is **blocked** if ANY of its dependencies are not green.
A task is **ready** if ALL of its dependencies are green, or if it has no dependencies.

The tool handles edge creation, side selection, and cycle detection automatically.

---

## Project Bootstrap

When no canvas file exists yet, the agent creates one using the `batch` command.

### Step 1: Understand the project
Ask the human **3-5 focused questions**:
- What is the project about? (one sentence)
- What are the main areas of work? (these become groups)
- What's the first thing that needs to happen? (immediate priorities)
- Is there an existing codebase or files to look at?

### Step 2: Create the canvas using `batch`
Based on the answers, create a blank `.canvas` file with the legend, then use `batch`:

```bash
cat <<'EOF' | python canvas-tool.py "Project.canvas" batch
{
  "groups": ["Research", "Development", "Delivery"],
  "tasks": [
    {"group": "Research", "title": "Define objectives", "desc": "Clarify project goals."},
    {"group": "Research", "title": "Gather requirements", "desc": "Document key needs.", "depends_on": ["Define objectives"]},
    {"group": "Development", "title": "Build prototype", "desc": "First version.", "depends_on": ["Gather requirements"]}
  ]
}
EOF
```

**Keep it small**: 2-4 groups, 3-5 tasks per group max.

### Step 3: Hand off to the human
- Present the board via `status`
- Ask the human to review in Obsidian and turn the cards they want to start with to red
- Once at least one card is red, proceed with the Session Protocol

---

## Session Protocol

### 1. Start
```bash
python canvas-tool.py "Project.canvas" status
```
Review the board state. Run `normalize` if needed. Report ready tasks, blocked tasks, and anomalies.

### 2. Pick a task
```bash
python canvas-tool.py "Project.canvas" ready           # see what's available
python canvas-tool.py "Project.canvas" show <TASK-ID>  # read task details
python canvas-tool.py "Project.canvas" start <TASK-ID> # begin work
```
If multiple tasks are ready, ask the human which to prioritize.

### 3. Work
- Execute the task
- If subtasks are discovered, propose them:
```bash
python canvas-tool.py "Project.canvas" propose Development "Subtask title" "Description" --depends-on DV-01
```
- Update notes on the in-progress task:
```bash
python canvas-tool.py "Project.canvas" edit <TASK-ID> "Updated description with findings."
```

### 4. Complete
```bash
python canvas-tool.py "Project.canvas" finish <TASK-ID>
```
Inform the human the task is done. Do NOT attempt to set the card green.

### 5. Repeat
Once the human marks the task green, check for newly unblocked tasks:
```bash
python canvas-tool.py "Project.canvas" normalize
python canvas-tool.py "Project.canvas" ready
```

### 6. End of session
```bash
python canvas-tool.py "Project.canvas" status
```
Summarize what was accomplished and what remains.

---

## CLI Tool Reference

**Usage:** `python canvas-tool.py "<file>.canvas" <command> [args]`

### Read-only commands

| Command | Args | Description |
|---|---|---|
| `status` | — | Board overview: groups, task counts by state, anomalies |
| `show` | `<TASK-ID>` | Full task detail: text, state, dependencies, dependents |
| `list` | `[STATE\|GROUP]` | All tasks (or filtered by state name or group label) |
| `blocked` | — | All gray tasks and what blocks each one |
| `blocking` | — | All non-green tasks that block others |
| `ready` | — | All red tasks with no unmet dependencies |
| `dump` | — | Raw canvas JSON |

### Task lifecycle commands

| Command | Args | Transition | Rejects if |
|---|---|---|---|
| `start` | `<TASK-ID>` | Red → Orange | Not red, or has unmet deps |
| `finish` | `<TASK-ID>` | Orange → Cyan | Not orange |
| `pause` | `<TASK-ID>` | Orange → Red | Not orange |

### Proposal commands

| Command | Args | Description |
|---|---|---|
| `propose` | `<GROUP> "<TITLE>" "<DESC>" [--depends-on ID ...]` | Add a purple card with optional dependencies |
| `propose-group` | `"<LABEL>"` | Create a new empty group |
| `batch` | *(JSON on stdin)* | Bulk-add groups and tasks in one call |

**Batch JSON format:**
```json
{
  "groups": ["GroupA", "GroupB"],
  "tasks": [
    {"group": "GroupA", "title": "Task 1", "desc": "...", "depends_on": ["Task ID or title"]},
    {"group": "GroupA", "title": "Task 2", "desc": "...", "depends_on": ["Task 1"]}
  ]
}
```
Tasks in a batch can reference earlier tasks by title (case-insensitive) or by existing task IDs on the board.

### Editing commands

| Command | Args | Rejects if |
|---|---|---|
| `edit` | `<TASK-ID> "<NEW-TEXT>"` | Not orange |
| `add-dep` | `<FROM-ID> <TO-ID>` | Would create circular dependency |

### Maintenance commands

| Command | Description |
|---|---|
| `normalize` | Assign IDs to unnamed cards, update blocked states (red↔gray) |

### Intentionally excluded

- **No `delete` command** — agents cannot remove cards or edges
- **No `done`/`approve` command** — only humans set green
- **No raw JSON editing** — all mutations go through validated commands

---

## Optional: Canvas Watcher Plugin

An Obsidian plugin that **automatically lints workflow canvases on save**. It manages blocked states, detects circular dependencies, and updates status cards — no terminal needed. This complements the CLI tool by handling the human side (when the human edits the canvas in Obsidian).

### What it does
- **Auto-manages blocked state**: red ↔ gray based on dependency arrows
- **Detects errors**: circular dependencies, orphaned edges
- **Detects warnings**: missing task IDs, missing colors, tasks outside groups, done tasks with pending dependencies
- **Updates status cards**: "Errors" and "Warnings" cards positioned above the Legend
- **Only runs on workflow canvases** (canvases with a Legend card containing "Red" and "Blocked")
- Triggers automatically on every canvas save (500ms debounce)
- Also available via ribbon icon (shield-check) or Command Palette → "Run Canvas Watcher"

### Installation

The plugin source lives in the `canvas-watcher-plugin/` folder at the root of this project:

```
canvas-watcher-plugin/
  main.js          # plugin logic
  manifest.json    # plugin metadata
  install.js       # installer script
```

To install into the current vault:

```bash
node canvas-watcher-plugin/install.js
```

This copies `main.js` and `manifest.json` into `.obsidian/plugins/canvas-watcher/`.

Then enable it in Obsidian: **Settings → Community plugins → Canvas Watcher**.

> **Note:** If Community plugins are disabled, you first need to turn them on in Settings → Community plugins → "Turn on community plugins".

### CLI fallback

If you prefer not to use the plugin, the standalone `canvas-watcher.js` script at the project root provides the same logic via terminal:

```bash
node canvas-watcher.js                        # watch mode (reacts to file changes)
node canvas-watcher.js "Some Specific.canvas"  # one-shot on a specific file
```
