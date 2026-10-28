# Changelog
## Unreleased

- Running tasks show their agent at work, after affectUI's Thinking Loader (MIT): a spinner of eight rays fading in turn replaces the pulsing dot on cards and in the task panel, a light sweeps over the agent's line, and a new note rolls the old line away
- Review from the inbox, after affectUI's Decision Inbox (MIT): tasks to review on this board get Mark done and Send back… right in the bell's list; a row confirms, then folds away, the count drops at once, and Undo in the toast brings it back. The heading counts what waits for you, or says All caught up
- An Agents panel on the right, opened with the people button by the bell, lists every agent on the open boards, from the notes and proposals they left: waiting for your reply with its question, working with its latest note, or idle since it was last seen. Click a task to open it, on another board too; the button counts the agents at work, and the panel stays open across visits

## v0.4.0 — 2026-10-27
Kanvas runs without Obsidian. The board is a JSON file, agents use a new CLI, and you plan and review in an interactive HTML page.

- Add `kanvas.html`: a single-file board with a dependency graph (swimlanes per group, left-to-right or top-to-bottom, adjustable spacing, lineage highlighting, drag to connect) and a kanban view, right-click menus for cards, dependencies and empty space, undo and redo that keep agents' changes, a task panel with Markdown, notes and review actions, filters, search, dark mode and keyboard shortcuts
- Status at a glance: cards are outlined in their state colour over a faint fill, proposed cards are dashed, blocked cards hatched, a light circles running tasks and their arrows flow, and the header shows overall progress
- Agent activity shows as it happens: a toast says what was proposed, started or finished, and by whom when the change came with a note, and the changed cards flash
- One inbox for every open board: the bell in the header counts the tasks waiting for your review across all boards the server has open, and lists them by board with who finished them and when; it reads the boards themselves, so the PC and the phone always agree. Toasts also report finished and started work on the other boards, while proposals, which plans bring in bulk, toast only on their own board; clicking a toast or an inbox row opens that task, switching board when needed. Toasts span the screen width on phones, keep the board name and the verb visible on long titles, and stack at most four
- Agents show when they wait for your reply: `ask ID "question"` marks a task in progress until the agent's next note, its card says **Waiting for you** with the question, a toast announces it on every open board, the bell lists questions above the work to review, the tab's title counts them, and `status` and `show` mark them for the next agent session. You reply in the agent's chat; Dismiss in the task panel clears a question whose chat is gone
- YOLO chains glow gold: their target cards get a ring of fire, their arrows are thick glowing gold lines, turning YOLO on sweeps along the chain, and a header chip counts the tasks agents may take on and jumps to each target
- New logo, a K whose arms are arrows, in the header, the favicon and `img/icon.svg`
- Groups wear a tag in their colour on cards, swimlanes, the task panel and menus, so they never look like a state's coloured square, and a Groups filter next to the state counts shows only the groups you tick, in the board view and in the graph, which lays out and fits just their swimlanes. Long group names stay on one line and end in an ellipsis, with the full name on hover, and cards are tall enough for a two-line title
- A calmer toolbar: the state counts form one filter control, settings you choose once (graph direction, card order, spacing, finished tasks, theme) live in a labelled Display menu, a button beside the filters hides or shows finished tasks in one click, renaming the board and managing groups move to a menu on the board name, the zoom controls form a small horizontal bar with the spacing slider and a button that sets spacing and zoom so the whole graph fills the screen; fitting and filling keep the graph clear of the zoom bar and of a task panel lying over the graph, and toolbar tooltips show each control's shortcut
- Works from the keyboard and with screen readers: menus move with the arrow keys, close with Escape or Tab and give focus back to their button, the Display panel keeps focus while you change settings, Tab goes round the task panel's controls and focus stays there when the panel updates, tooltips also appear on keyboard focus, and the sync status, progress, YOLO chip and icon-only buttons have names
- The task panel opens on the left and leads with what to do next: a card in the task's state colour holds the latest agent summary and the actions for that state (approve, start, review, reopen), what it waits on, and a copyable `work on ID` for ready tasks; state and group change from the header, the description shows its checklist progress, and notes can be edited or deleted (hover a note, or double-click it); an edited note keeps its author and time and records when it was edited
- Add `kanvas.py serve`: live two-way sync between the browser, the file and your agents, with compare-and-swap saves
- One Kanvas serves the boards of all your projects: `serve` in a second project opens its board in the Kanvas already running and gives the terminal back. Each board has its own address, named after its project folder; switch boards from the menu on the board's name or with Shift+← and Shift+→, or open each in a tab of its own
- Open your boards on your phone over Tailscale: Open on your phone… in the board menu shows a QR code to scan, and `kanvas serve --tailscale` prints one in the terminal; Kanvas listens only on this computer and its Tailscale address, answers only to this computer's names, and opens new boards only from this computer
- Kanvas remembers its open boards and phone access in your user folder and reopens them on the next start; `kanvas service install` starts it when you log in (a systemd user service on Linux, a LaunchAgent on macOS, a logon task on Windows), serving those boards with `kanvas serve --saved`
- Open `kanvas.html` directly in Chrome or Edge to edit `kanvas.json` in place, or export a read-only snapshot with `kanvas.py export`
- Replace `canvas-tool.py` with `kanvas.py`; boards live in `kanvas.json` with stable formatting for clean diffs, which the CLI and the browser write byte for byte the same
- `finish` requires a summary, stored as a note, so task descriptions stay as written (#10)
- New tasks pick what they depend on and what they block from a searchable multi-select, or by clicking cards on the graph; choices that would create a cycle are greyed out
- Tidier graph: arrows that skip levels run between cards instead of under them, arrow ends spread along the card edge, cards are ordered to cut crossings and lined up so more arrows are straight
- Manual graph layout (`M`): cards keep the order of `kanvas.json`; drag a card up or down to reorder it, or into another swimlane to change its group; drag a swimlane by its name to reorder the groups
- Delete a group from its swimlane's right-click menu or from Groups… on the board name: its tasks stay without a group, or go with it if you tick the box
- Hiding finished tasks (`H`) also hides the swimlanes of groups with nothing left to do, and fits the graph again
- Arrow keys select the next card in that direction, in the graph and the board, and keys act on the selected card: A approves, S starts, P pauses, D marks done, E renames, Delete or Backspace deletes (or rejects a proposal) after asking
- Act on several tasks at once: Ctrl+click or Shift+click cards (on a phone, choose Select from a card's menu), then click more cards to add or remove them; a bar approves the proposals, marks the reviewed tasks done or deletes them all, each in one step that Ctrl+Z undoes, and A, D and Delete do the same from the keyboard; right-clicking or long-pressing a selected card opens the same actions for the whole selection
- On phones the header is one row that gives the board's name the room it needs: a Filter button holds search, the state counts, finished tasks and the groups and lights up while a filter is on, the graph and board switch moves into the Display menu, undo and redo show when they have something to do, the board menu starts with the board's full name, and the live dot keeps its size
- The graph hides arrows for dependencies that another dependency already implies, and draws them dashed while one of their tasks is selected
- YOLO: mark a task in the board and agents may start and finish everything it depends on without approval or review, then the task itself, which comes back to you for review and ends the YOLO; a proposal an agent links into the chain later waits for your approval; YOLO on a task further along takes over the chain, so each chain ends with one review; its confirmation can be skipped with "Don't ask again" and brought back from the keyboard shortcuts dialog
- Agents add notes with `note`; notes and proposals record the author (`--as`, `KANVAS_AGENT`), and proposed cards, the task panel and `show` say who proposed a task and when
- Group prefixes may contain digits, such as `L0C-01` (#12), and IDs continue past 99; a new task takes the highest number in its group plus one, so the ID of a deleted or rejected task can come back
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
- Typed text survives a failed save: notes, note edits, descriptions, titles, Send back, Ask for changes, New task and the group dialogs get their text back, and the page asks before you close it with unsaved work
- A banner says when the page cannot reach Kanvas and how to start it again; changes are refused at once until Kanvas answers, so nothing looks saved that was not
- Ask for changes on a proposal: your request is a note, the proposal stays proposed, and the panel shows the request until the agent answers with a note
- Every quick action (approve, start, pause, done, send back, delete, dependency changes) confirms with a toast whose Undo button takes back that change, and a failed save takes the toast away
- Review in a row: in the board view, Mark done and Send back open the next task in review within your filters, and Undo comes back to the task; every task opens at the top of its panel
- The New task dialog says where the task lands and what it waits for, and remembers the group of the last task you made
- The graph opens at a readable zoom on the work that waits for you, groups that follow one another share rows, a swimlane's finished tasks fold into one "8 done" stub that opens on click, and boards with more than ten groups get colours of their own
- On phones a row of states with their counts sits above the columns, and you move cards by touch: hold a card until it lifts and drag it to a column or a state, or let go to open its menu
- States read apart without colour: each has a mark that fills up as the work moves along, from an empty circle for To do to a check for Done; text and badges reach WCAG AA contrast in both themes
- Cards are in the Tab order and named for screen readers, selection follows focus, pickers and menus announce their choices, and reduced motion holds every animation still
- The header shows the state names at laptop widths and gives the board title the free space; untitled boards go by their folder's name everywhere; the shortcuts dialog lists every shortcut in four groups; Apple keyboards see ⌘ where the page names Ctrl
- Markdown descriptions and notes render tables, nested lists, images as links and URLs with parentheses; task IDs inside link text stay text; deeply nested quotes, very long lines and Unicode line separators can no longer stall or break the panel
- `serve` finds a running Kanvas on any of its 20 ports and moves to the next free port when the first is taken, and `service status` and `service install` find it too; running `service install` again restarts the service
- Devices on your Tailscale network see board names without their paths, and phones that come through `tailscale serve` cannot open boards or change phone access
- `batch` checks its whole input before writing, a lock left by a crashed process is broken after 5 seconds, `start` reads its plan with `--stdin` or `--file`, `serve -h` documents `--port`, and board saves without `If-Match` or a task list are refused
- File mode keeps an agent's write that lands while the page saves, and saving a board kept in the tab downloads reliably
- The agent skill asks agents to write descriptions, summaries and notes for the human who decides from them: what matters for the decision first, what to check easy to find, and the rest left out
- Add a test suite, with headless browser smoke tests, and CI on Linux, macOS and Windows

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
