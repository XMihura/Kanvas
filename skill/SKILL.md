---
name: kanvas
description: Plan and track work on the project's Kanvas board (kanvas.json), which you share with a human who approves and reviews tasks. Use when the user mentions the board, Kanvas, tasks, planning, "what's next", or a task ID such as DV-03.
license: MIT
compatibility: Requires Python 3.8 or later.
---

# Kanvas

The board is `kanvas.json` at the project root. Change it only through the CLI in `.agents/skills/kanvas/`; the CLI enforces the workflow and keeps the file valid:

```bash
python .agents/skills/kanvas/kanvas.py <command>
```

Use `python3` where `python` is missing. Never edit `kanvas.json` by hand. The human watches the board in a browser, where your changes appear within a second. It opens with `kanvas serve` when Kanvas is installed, or `python .agents/skills/kanvas/kanvas.py serve`. When Kanvas already runs for another project, this opens the board there and returns at once; otherwise it keeps running until stopped.

## Workflow

```
proposed -> todo -> doing -> review -> done
```

| State | Moved there by | Meaning |
|-------|----------------|---------|
| proposed | you (`propose`, `batch`) | An idea waiting for human approval |
| todo | the human | Approved. Shown as **blocked** while a dependency is unfinished |
| doing | you (`start`) | In progress |
| review | you (`finish`, with a summary) | Waiting for the human to check it |
| done | the human | Accepted |

You may propose tasks and groups, start ready tasks, finish or pause tasks in progress, add notes and add dependencies. Until the human approves a proposal, you may also revise it (`edit`), withdraw it (`withdraw`) or remove its dependencies (`undepend`), so rework proposals freely while you discuss a plan. Approving proposals, changing approved tasks and marking tasks done belong to the human; ask for them in the conversation.

The human can also ask for changes to a proposal in the board. `status` then marks it `(changes requested: ...)`, and `show ID` has the request in its notes. Revise the proposal with `edit ID`, passing `-m` with what you changed; that note answers the request, and the mark goes.

### YOLO

The human can put a task under **YOLO** in the board. That covers the task and everything it depends on, directly or indirectly; the CLI marks covered tasks `(YOLO)`. For covered tasks:

- turning YOLO on approves the chain's proposals, so `ready` lists them and `start` accepts them;
- `finish` moves the tasks the YOLO task depends on straight to done, with your summary as a note, and the tasks waiting on them unblock;
- the YOLO task itself goes to review when you finish it, so the human checks the result of the whole chain;
- when YOLO tasks follow one another in a chain, only the last one goes to review;
- YOLO ends once its task is in review or done.

Only the human can turn YOLO on or off. A proposal you link into a YOLO chain with `depend` stays a proposal: the YOLO task waits for it, and you start it only after the human approves it in the board, so ask them to. Linking an approved task into the chain puts it under YOLO, so it is done without review; do that only for work the human clearly asked for.

## Session protocol

1. **Read the board:** `status`. Tell the user what waits for review, what is ready, what is blocked and what is proposed, and repeat any question still marked `(waiting for the human: ...)`. Revise proposals marked `(changes requested: ...)` as the human asked.
2. **Pick a task:** if the user named one, `show ID`. Otherwise `ready`; when several tasks are ready, ask which one to take.
3. **Start it:** `start ID -m "Plan: ..."`. Read the description and every note. A note starting with "Sent back:" is review feedback you must address.
4. **Work.** Record findings with `note ID "..."`. Propose any extra work you discover with `propose`, linking it with `--deps`.
5. **Finish:** `finish ID -m "..."` with a summary the human can accept or send back from: what changed for them, how you verified it, and what they should check. Tell the user the task is ready for review, then stop. Start the next task only when asked. Under YOLO, the tasks the YOLO task depends on are done instead of waiting for review, and you may go on with the next ready `(YOLO)` task without asking. The YOLO task itself goes to review; stop when no `(YOLO)` task is ready and summarize what you did.

To stop halfway, run `pause ID -m "Where I left off and why"`.

When you need the human's answer before going on, run `ask ID "the question"`, ask the same question in the conversation, and stop. The board then shows the task waiting for them, with a notification. When they reply, record the answer with `note ID "..."`; the next note ends the wait.

## Planning

When asked to plan, get to know the work first: what is already there (code, documents, the board itself) and what the user wants from the board. Tasks can be large milestones the user checks now and then, or small steps they follow closely; size them to how the user wants to work, and ask when it is unclear. Propose the plan in one batch:

```bash
python .agents/skills/kanvas/kanvas.py batch <<'EOF'
{
  "groups": ["Training", "Flight tests"],
  "tasks": [
    {"title": "Train the first policy", "group": "Training", "desc": "Flies random obstacle courses in the simulator through every gate, in wind up to 8 m/s"},
    {"title": "First autonomous flight", "group": "Flight tests", "deps": ["Train the first policy"]}
  ]
}
EOF
```

`deps` takes task IDs or the titles of earlier tasks in the same batch. A batch is all or nothing. Everything arrives as proposals; ask the human to approve them in the board.

Give each task a `desc` that says what it achieves and what finished means, since the human approves the proposal from it and later reviews the work against it. Groups are areas of work; a group's ID prefix starts every task ID in it (`RS-01`). A new task takes the highest number in its group plus one, so the ID of a rejected or deleted task can come back on the next one: check the title before acting on an old ID.

## Writing for the human

The human decides from what you write on the board: whether to approve a proposal, accept finished work or send it back, and how to answer your question. They read it between other work, without your context. Before you write a description, summary or note, put yourself in their place: what do they need to understand the task and decide, and how fast can they get it?

- Open with what matters for the decision. A summary starts with what now works or changed for them; a proposal, with what it would achieve.
- Make what they should check or try, and anything they must decide, easy to find.
- Leave out what does not help them decide: how you got there, dead ends, every file you touched, numbers that only show diligence. The code and its history keep those.
- Name things as they see them in the product before you name the code behind them, and explain terms they may not know.
- Fit the length and shape to the task. A small fix takes a line or two; a change with risks, trade-offs or choices for them takes the room to set those out clearly. Use lists for items they will go through one by one.
- When unsure, write less: they can ask, and a later `note` can add detail.

## Commands

| Command | Purpose |
|---------|---------|
| `status` | Overview: doing, review, ready, blocked, proposed, problems |
| `ready` | Tasks you can start now |
| `show ID` | Details, dependencies, notes |
| `list [STATE or GROUP]` | Tasks by group; filter by state (`ready`, `blocked`, ...) or group |
| `blocked` | Blocked tasks and what they wait on |
| `start ID [-m NOTE]` | todo -> doing |
| `finish ID -m SUMMARY` | doing -> review, or done for tasks a YOLO task depends on (the summary is required) |
| `pause ID [-m NOTE]` | doing -> todo |
| `note ID TEXT` | Append a note |
| `ask ID QUESTION` | Show the human that you wait for their reply in the conversation; the next note ends the wait |
| `propose GROUP TITLE [DESC] [--deps ID ...]` | New proposal; use `-` as GROUP for none |
| `add-group NAME [--prefix XX]` | New group |
| `batch` | Many groups and proposals from JSON on stdin |
| `depend ID ON [ON ...]` | ID waits for the others (cycles are rejected) |
| `edit ID [--title T] [--group G] [--desc D] [-m NOTE]` | Revise a proposal; `-m` answers a request for changes; `--stdin` or `--file` for a long description |
| `withdraw ID [ID ...]` | Delete proposals |
| `undepend ID ON [ON ...]` | Remove dependencies from a proposal |
| `check` | Validate the board |

Notes and proposals record your name, so the human can tell who did what. Sign with the name of your tool: `claude-code`, `cursor`, `codex`, `gemini`, `copilot`, `opencode`, or another short name for it. Pass `--as cursor` to each command, or set `KANVAS_AGENT=cursor` once if your shell keeps environment variables between commands. When several agents of one tool share the board, add what each one does, as in `cursor-docs`. Unsigned notes show as `agent`.

## Long or non-ASCII text

Commands that take text also accept `--stdin` and `--file PATH`, which read UTF-8 on every platform. They give the plan for `start`, the summary for `finish`, the note for `pause` and `note`, the question for `ask`, and the description for `propose` and `edit`; the `-m` note of `edit` is an argument only. Use a heredoc for multi-line summaries:

```bash
python .agents/skills/kanvas/kanvas.py finish TR-02 --stdin <<'EOF'
The policy flies the course on its own: 92 of 100 random courses through every gate, 3 crashes, 5 too slow.
- To see it: `make replay` plays ten flights, the three crashes among them
- All three crashes happen in gusts above 7 m/s: try it on the real drone in calm weather, or train on stronger wind first?
EOF
```

In PowerShell, write the text to a file and pass `--file summary.md`.

## When a command fails

The CLI exits with a non-zero code and an `Error:` line explaining the rule, for example "DV-04 is blocked by unfinished DV-02". Follow the message, or ask the human. Editing the file to get around a rule breaks the human's trust in the board.
