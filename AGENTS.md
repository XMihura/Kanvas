# Canvas Workflow — Agent Instructions

This project uses Obsidian Canvas files as visual task boards. A Python CLI tool manages all board modifications.

## CRITICAL RULE

**NEVER edit `.canvas` files directly.** All canvas modifications MUST go through the CLI tool:

```bash
python canvas-tool.py "<file>.canvas" <command> [args]
```

Direct JSON editing of `.canvas` files is **forbidden**. The CLI tool enforces workflow rules (valid transitions, cycle detection, blocked states) so you don't have to remember them.

## Session Protocol

### 1. Start of session — read the board

```bash
python canvas-tool.py "Project.canvas" status
```

Review the board state. Run `normalize` if needed. Report ready tasks, blocked tasks, and any anomalies to the user.

### 2. Pick a task

```bash
python canvas-tool.py "Project.canvas" ready            # see what's available
python canvas-tool.py "Project.canvas" show <TASK-ID>    # read task details
python canvas-tool.py "Project.canvas" start <TASK-ID>   # begin work (red → orange)
```

If multiple tasks are ready, ask the user which to prioritize.

### 3. Work on the task

Execute the task. If you discover subtasks, propose them:

```bash
python canvas-tool.py "Project.canvas" propose Development "Subtask title" "Description" --depends-on DV-01
```

Update notes on your in-progress task:

```bash
python canvas-tool.py "Project.canvas" edit <TASK-ID> "Updated description with findings."
```

### 4. Finish the task

```bash
python canvas-tool.py "Project.canvas" finish <TASK-ID>   # orange → cyan
```

Tell the user what was done. Do NOT attempt to set the card green — only the human does that.

### 5. Repeat

After the human marks your task green, check for newly unblocked tasks:

```bash
python canvas-tool.py "Project.canvas" normalize
python canvas-tool.py "Project.canvas" ready
```

## What you CAN do

- **Read** the board: `status`, `show`, `list`, `blocked`, `blocking`, `ready`, `dump`
- **Normalize** the board: `normalize`
- **Propose** tasks: `propose` or `batch` (creates purple cards)
- **Propose** groups: `propose-group` or `batch`
- **Start** a task: `start <ID>` (red → orange)
- **Finish** a task: `finish <ID>` (orange → cyan)
- **Pause** a task: `pause <ID>` (orange → red)
- **Edit** task text: `edit <ID> "<text>"` (only orange tasks)
- **Add dependencies**: `add-dep <FROM> <TO>`

## What you CANNOT do

- Edit `.canvas` files directly
- Mark any card green (done) — human only
- Work on purple cards (proposals awaiting approval)
- Work on gray cards (blocked)
- Work on cyan cards (awaiting human review)
- Remove cards or edges
- Change green cards

## CLI Quick Reference

### Read-only commands
| Command | Description |
|---------|-------------|
| `status` | Board overview |
| `show <ID>` | Task detail with dependencies |
| `list [STATE\|GROUP]` | List tasks (filtered) |
| `blocked` | Gray tasks and blockers |
| `blocking` | Tasks that block others |
| `ready` | Red tasks with deps met |
| `dump` | Raw JSON |

### Lifecycle commands
| Command | Transition |
|---------|-----------|
| `start <ID>` | Red → Orange |
| `finish <ID>` | Orange → Cyan |
| `pause <ID>` | Orange → Red |

### Proposal commands
| Command | Description |
|---------|-------------|
| `propose <GROUP> "<TITLE>" "<DESC>" [--depends-on ID ...]` | Add task |
| `propose-group "<LABEL>"` | Add group |
| `batch` | Bulk-add from JSON on stdin |

### Other commands
| Command | Description |
|---------|-------------|
| `edit <ID> "<TEXT>"` | Update text (orange only) |
| `add-dep <FROM> <TO>` | Add dependency |
| `normalize` | Assign IDs, fix blocked states |

## Color meanings

| Color | Meaning |
|-------|---------|
| 🟣 Purple | Proposed by agent |
| 🔴 Red | To Do (ready) |
| 🟠 Orange | Doing |
| 🔵 Cyan | Ready to review |
| 🟢 Green | Done |
| ⬜ Gray | Blocked |

## Two-agent setup (overrides the sections above where they conflict)

This project runs two roles. Everything above applies to the
ORCHESTRATOR only.

- Orchestrator: the only role that runs canvas-tool.py. Plans, delegates,
  reviews, moves cards.
- Executor (gentle-ai-worker): implements ONE card and reports back.
  It must NEVER run canvas-tool.py, edit the .canvas file, or move cards.

### Canvas-first rule (mandatory, applies before any other workflow)
ALL work requests go through the canvas, no matter how small. This
takes priority over any default workflow (ODD, plans, feature documents).
1. On any request to build, change or fix something, FIRST propose the
   work as cards with `batch` (purple), then STOP and wait for the
   human to approve them. Do not edit project files before that.
2. Small request = one single card.
3. The human approving a card (purple to red) IS the authorization to
   execute it. Do not ask for authorization again, and do not create
   separate plans, task lists or feature documents.
4. After approval, do NOT start until the human says to start.
5. If the request names a branch, create it when starting the first card.
6. Questions, explanations and reading the code do not need cards.

### Picking a task
- If several tasks are ready, pick them in dependency order. Only ask
  the human which to prioritize if the choice is genuinely ambiguous.

### Delegating
1. Run `start <ID>` (orange), then delegate to the executor.
2. Trivial edits (one file, a few lines, no exploration needed) may be
   done directly by the orchestrator. Delegate everything else to
   gentle-ai-worker.
3. Give the executor only: goal, scope, and the "done when" command from
   the card.
4. The executor implements and returns a short summary of what changed.

### Reviewing before cyan
1. Run the card's "done when" command, then check that the result
   matches the card's goal (not only that the command passes).
2. If it fails or something is missing: delegate again with what is
   missing. Do NOT run `finish`.
3. If it passes: run `edit <ID>` to add a 2-3 line summary of what
   changed, then `finish <ID>` (cyan).
4. Never mark green. The human checks the diff and does that.

### Writing cards
Every proposed card must contain:
- Goal: one sentence.
- Scope: probable files or folders.
- Done when: a command that proves it works (test, build, lint).
One card = one executor session. Split anything bigger.

### Style
- Task state lives ONLY in the canvas. Do not create parallel task
  lists or feature documents for a single card.
- One commit per card, including the .canvas file.
- Commit locally after each card, but NEVER push. The human reviews
  the commit and pushes.
- Modify project files with the edit/write tools, not with shell
  commands like sed, echo or redirects, so the human can review the
  changes in Gentle Changes.
- Ask the human only for real scope decisions; otherwise keep going.
