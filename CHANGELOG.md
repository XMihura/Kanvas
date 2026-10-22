# Changelog
## v0.4.0 — 2026-10-19
Kanvas runs without Obsidian. The board is a JSON file, agents use a new CLI, and you plan and review in an interactive HTML page.

- Add `kanvas.html`: a single-file board with a dependency graph (swimlanes per group, left-to-right or top-to-bottom, adjustable spacing, lineage highlighting, drag to connect) and a kanban view, right-click menus for cards, dependencies and empty space, undo and redo that keep agents' changes, a task panel with Markdown, notes and review actions, filters, search, dark mode and keyboard shortcuts
- Status at a glance: cards are outlined in their state colour over a faint fill, proposed cards are dashed, blocked cards hatched, a light circles running tasks and their arrows flow, and the header shows overall progress
- Agent activity shows as it happens: a toast says what was proposed, started or finished, and by whom when the change came with a note, and the changed cards flash
- YOLO chains glow gold: their target cards get a ring of fire, their arrows are thick glowing gold lines, turning YOLO on sweeps along the chain, and a header chip counts the tasks agents may take on and jumps to each target
- New logo, a K whose arms are arrows, in the header, the favicon and `img/icon.svg`
- A calmer toolbar: the state counts form one filter control, settings you choose once (graph direction, card order, spacing, finished tasks, theme) live in a labelled Display menu, renaming the board and managing groups move to a menu on the board name, the zoom controls form a small horizontal bar, and toolbar tooltips show each control's shortcut
- Works from the keyboard and with screen readers: menus move with the arrow keys, close with Escape or Tab and give focus back to their button, the Display panel keeps focus while you change settings, Tab goes round the task panel's controls and focus stays there when the panel updates, tooltips also appear on keyboard focus, and the sync status, progress, YOLO chip and icon-only buttons have names
- The task panel opens on the left and leads with what to do next: a card in the task's state colour holds the latest agent summary and the actions for that state (approve, start, review, reopen), what it waits on, and a copyable `work on ID` for ready tasks; state and group change from the header, the description shows its checklist progress, and notes can be edited or deleted (hover a note, or double-click it); an edited note keeps its author and time and records when it was edited
- Add `kanvas.py serve`: live two-way sync between the browser, the file and your agents, with compare-and-swap saves
- Open `kanvas.html` directly in Chrome or Edge to edit `kanvas.json` in place, or export a read-only snapshot with `kanvas.py export`
- Replace `canvas-tool.py` with `kanvas.py`; boards live in `kanvas.json` with stable formatting for clean diffs, which the CLI and the browser write byte for byte the same
- `finish` requires a summary, stored as a note, so task descriptions stay as written (#10)
- New tasks pick what they depend on and what they block from a searchable multi-select, or by clicking cards on the graph; choices that would create a cycle are greyed out
- Tidier graph: arrows that skip levels run between cards instead of under them, arrow ends spread along the card edge, cards are ordered to cut crossings and lined up so more arrows are straight
- Manual graph layout (`M`): cards keep the order of `kanvas.json`; drag a card up or down to reorder it, or into another swimlane to change its group; drag a swimlane by its name to reorder the groups
- Delete a group from its swimlane's right-click menu or from Groups… on the board name: its tasks stay without a group, or go with it if you tick the box
- Arrow keys select the next card in that direction, in the graph and the board
- The graph hides arrows for dependencies that another dependency already implies, and draws them dashed while one of their tasks is selected
- YOLO: mark a task in the board and agents may start and finish everything it depends on without approval or review, then the task itself, which comes back to you for review and ends the YOLO; YOLO on a task further along takes over the chain, so each chain ends with one review; its confirmation can be skipped with "Don't ask again" and brought back from the keyboard shortcuts dialog
- Agents add notes with `note`; notes record the author (`--as`, `KANVAS_AGENT`)
- Group prefixes may contain digits, such as `L0C-01` (#12), and IDs continue past 99
- Install Kanvas as the `kanvas` command on Linux, macOS and Windows with `uv tool install kanvas-board` or `pipx install kanvas-board`, or try it with `uvx kanvas-board`; uv downloads Python when it is missing, and agents keep using the project's copy, so they need nothing installed
- `init` installs an agent skill in `.agents/skills/kanvas/`, where Codex, Cursor, Gemini CLI, OpenCode and GitHub Copilot find it, with a copy in `.claude/skills/` for Claude Code; it creates an `AGENTS.md` pointing to it when the project has none, and never modifies existing files (#8); run without a folder, it sets up the folder of an existing board, else the top folder of the git repository, and `kanvas init .` keeps the current folder
- `serve` in a project without a board offers to create one, so a first-time user needs only `kanvas serve`; without a person at the terminal it fails with a message naming `kanvas init`
- Or let your agent set Kanvas up: the README has one sentence to paste into Claude Code, Codex, Cursor, Gemini CLI or OpenCode, which runs `init` and starts planning with you. When a sandbox keeps `.agents/` read-only, as Codex's does, `init` changes nothing and says to run it outside the sandbox
- Messages name the command you typed: `kanvas ...` for the installed command, `python .agents/skills/kanvas/kanvas.py ...` for the project's copy
- The browser page suggests `kanvas serve`, or the project's copy when the command is not installed, and the empty board asks you to have your agent interview you and plan the project
- `status` and `serve` say when a project's agent copy is out of date and suggest `kanvas init`, or `uv tool upgrade kanvas-board` when the installed `kanvas` is the older one
- Text options read UTF-8 from `--stdin` or `--file` on every platform, fixing multi-line and non-ASCII text on Windows (#20, #23)
- Agents can rework proposals until you approve them: `edit` changes the title, description or group, `withdraw` deletes proposals and `undepend` removes dependencies. Approved tasks and proposals under YOLO stay yours to change, and the board shows a notice when an agent revises or withdraws a proposal
- The skill's planning guidance fits any kind of work: agents first look at what the project already holds and what you want from the board, and size tasks to how you want to follow them, from large milestones to small steps
- The skill asks agents to sign notes with their tool's name (`claude-code`, `cursor`, `codex`, ...) so the board shows who did what
- Text arguments may follow options, as in `note DV-01 --as claude "..."`
- Add `check`, `depend`, `add-group`, `import` (migrates Obsidian `.canvas` boards) and `export`
- Writes are locked and atomic, so several agents and the browser can work at once; a board in a folder you cannot write to fails with a clear message
- Remove the Obsidian plugin, `canvas-watcher.js`, `RULES.md` and the `.canvas` examples
- Add a test suite and CI on Linux, macOS and Windows

## v0.3.1 — 2026-03-19
- Fix `add-dep` incorrectly rejecting valid edges when a pre-existing unrelated cycle existed elsewhere in the graph
## v0.3.0 — 2026-03-19
- Add DAG auto-layout engine to the Canvas Watcher plugin
- Add vertical and horizontal layout buttons to the Obsidian canvas toolbar
- Add layer-gap slider to control spacing between dependency levels, persisted across sessions
- Layout respects group membership, resolves depth conflicts via graph coloring, and applies transitive reduction
- Add `.gitattributes` to normalize line endings

## v0.2.0 — 2026-03-16
- Add `init` command to set up Kanvas in a project directory
- Add `--no-plugin` flag to skip Obsidian plugin install during init
- Improve README setup instructions

## v0.1.0 — 2026-03-13
- Initial release
- CLI tool (`canvas-tool.py`) with workflow enforcement
- Workflow rules (`RULES.md`) and agent instructions (`CLAUDE.md`, `AGENTS.md`)
- Canvas Watcher Obsidian plugin
- Standalone canvas watcher (`canvas-watcher.js`)
- Example boards (blank template, sample project)
