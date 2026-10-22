#!/usr/bin/env python3
"""Kanvas: a task board shared by humans and AI agents.

The board is a single JSON file (kanvas.json). Agents change it only through
this CLI, which enforces the workflow rules. Humans plan and review in the
browser:

    python kanvas.py serve

Python 3.8+, standard library only. Run `python kanvas.py -h` for commands.
"""

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import socketserver
import sys
import threading
import time
import webbrowser
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

VERSION = "0.4.0rc1"
FORMAT = 1
INSTALLED = __name__ == "kanvas_board"
BOARD_FILE = "kanvas.json"
HERE = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(HERE, "kanvas.html")
SNAPSHOT_TAG = '<script id="kanvas-snapshot" type="application/json"></script>'

STATES = ("proposed", "todo", "doing", "review", "done")
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")
PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}$")
UNGROUPED_PREFIX = "G"
TASK_KEYS = ("id", "title", "state", "group", "deps", "yolo", "desc", "notes")
REQUIRED_TASK_KEYS = ("id", "title", "state")
GROUP_KEYS = ("id", "name")
NOTE_KEYS = ("at", "by", "edited", "text")
REQUIRED_NOTE_KEYS = ("at", "by", "text")

LOCK_TIMEOUT = 5.0
STALE_LOCK_AGE = 10.0
MAX_UPLOAD = 20 * 1024 * 1024


class KanvasError(Exception):
    pass


# ---------------------------------------------------------------------------
# Board file: discovery, parsing, canonical serialization, safe writes
# ---------------------------------------------------------------------------


def find_board(start=None):
    """Return the nearest kanvas.json in `start` or any parent directory."""
    d = os.path.abspath(start or os.getcwd())
    while True:
        candidate = os.path.join(d, BOARD_FILE)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def new_board(title=""):
    return {"kanvas": FORMAT, "title": title, "groups": [], "tasks": []}


def read_text_file(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8-sig")


def parse_board(text):
    try:
        board = json.loads(text)
    except json.JSONDecodeError as e:
        raise KanvasError(f"Board is not valid JSON: {e}")
    if not isinstance(board, dict):
        raise KanvasError("Board must be a JSON object")
    if isinstance(board.get("kanvas"), int) and board["kanvas"] > FORMAT:
        upgrade = "upgrade with `uv tool upgrade kanvas-board`" if INSTALLED else "upgrade kanvas.py"
        raise KanvasError(f"Board was written by a newer version of Kanvas; {upgrade}")
    # Defaults follow JavaScript's truthiness and String(), as normalize() in kanvas.html does.
    board["title"] = _js_text(board.get("title"))
    for key in ("groups", "tasks"):
        board.setdefault(key, [])
        if not isinstance(board[key], list) or not all(isinstance(x, dict) for x in board[key]):
            raise KanvasError(f"Board '{key}' must be a list of objects")
    for g in board["groups"]:
        if not isinstance(g.get("id"), str):
            raise KanvasError("Every group needs a string 'id'")
        g["name"] = _js_str(g["name"]) if _js_truthy(g.get("name")) else g["id"]
    for t in board["tasks"]:
        if not isinstance(t.get("id"), str):
            raise KanvasError("Every task needs a string 'id'")
        t["title"] = _js_text(t.get("title"))
        t["state"] = t["state"] if _js_truthy(t.get("state")) else "todo"
        t["group"] = t["group"] if _js_truthy(t.get("group")) else None
        t["desc"] = _js_text(t.get("desc"))
        deps = t.get("deps")
        t["deps"] = [_js_str(d) for d in deps] if isinstance(deps, list) else []
        if _js_truthy(t.get("yolo")):
            t["yolo"] = True
        else:
            t.pop("yolo", None)
        notes = t.get("notes")
        t["notes"] = [n for n in notes if isinstance(n, dict)] if isinstance(notes, list) else []
    return board


def _js_truthy(v):
    if v is None or v is False or v == "":
        return False
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v == v and v != 0
    return True


def _js_str(v):
    """String(v) in JavaScript, for the values JSON can hold."""
    if isinstance(v, str):
        return v
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        text = _js_number(v)
        if text is None:
            return "NaN" if v != v else "Infinity" if v > 0 else "-Infinity"
        return text
    if isinstance(v, list):
        return ",".join("" if x is None else _js_str(x) for x in v)
    return "[object Object]"


def _js_text(v):
    """String(v || "") in JavaScript."""
    return _js_str(v) if _js_truthy(v) else ""


def _js_number(x):
    """A number as JavaScript prints it (shortest round trip), or None if it is not finite."""
    if isinstance(x, int) and abs(x) <= 2 ** 53:
        return str(x)
    try:
        x = float(x)
    except OverflowError:
        return None
    if x != x or x in (float("inf"), float("-inf")):
        return None
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    mantissa, _, exp = repr(abs(x)).partition("e")
    whole, _, frac = mantissa.partition(".")
    digits = (whole + frac).lstrip("0")
    point = len(whole) + int(exp or 0) - (len(whole + frac) - len(digits))
    digits = digits.rstrip("0")
    if len(digits) <= point <= 21:
        return sign + digits + "0" * (point - len(digits))
    if 0 < point <= 21:
        return sign + digits[:point] + "." + digits[point:]
    if -6 < point <= 0:
        return sign + "0." + "0" * -point + digits
    e = point - 1
    return f"{sign}{digits[0]}{'.' + digits[1:] if len(digits) > 1 else ''}e{'+' if e > 0 else '-'}{abs(e)}"


def load_board(path):
    if not os.path.isfile(path):
        raise KanvasError(f"Board not found: {path}")
    return parse_board(read_text_file(path))


def _ordered(obj, keys, keep=()):
    out = {}
    for k in keys:
        v = obj.get(k)
        if k in keep or v not in (None, "", []):
            out[k] = v if v is not None else ""
    for k, v in obj.items():
        if k not in keys:
            out[k] = v
    return out


def canonical(board):
    """Stable key order and no empty optional fields, so diffs stay small."""
    out = {"kanvas": FORMAT, "title": board.get("title") or ""}
    out["groups"] = [_ordered(g, GROUP_KEYS, GROUP_KEYS) for g in board.get("groups", [])]
    tasks = []
    for t in board.get("tasks", []):
        t = _ordered(t, TASK_KEYS, REQUIRED_TASK_KEYS)
        if "notes" in t:
            t["notes"] = [_ordered(n, NOTE_KEYS, REQUIRED_NOTE_KEYS) for n in t["notes"]]
        tasks.append(t)
    out["tasks"] = tasks
    for k, v in board.items():
        if k not in out:
            out[k] = v
    return out


# kanvas.html applies the same transformation, so files written from the
# browser and from the CLI are byte-identical.
_STR_ARRAY = re.compile(r'\[\n\s*("(?:[^"\\\n]|\\.)*"(?:,\n\s*"(?:[^"\\\n]|\\.)*")*)\n\s*\]')


_INDEX_KEY = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _json(v, indent=""):
    """JSON.stringify(v, null, 2): integer-like keys first, numbers and lone surrogates as JavaScript writes them."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return _js_number(v) or "null"
    if isinstance(v, str):
        return _LONE_SURROGATE.sub(lambda m: "\\u%04x" % ord(m.group()), json.dumps(v, ensure_ascii=False))
    inner = indent + "  "
    if isinstance(v, list):
        items = [inner + _json(x, inner) for x in v]
    else:
        index = sorted((k for k in v if _INDEX_KEY.match(k) and int(k) < 2 ** 32 - 1), key=int)
        first = set(index)
        items = [f"{inner}{_json(k)}: {_json(v[k], inner)}" for k in index + [k for k in v if k not in first]]
    if not items:
        return "[]" if isinstance(v, list) else "{}"
    open_, close = "[]" if isinstance(v, list) else "{}"
    return f"{open_}\n" + ",\n".join(items) + f"\n{indent}{close}"


def dumps(board):
    text = _json(canonical(board))
    text = _STR_ARRAY.sub(lambda m: "[" + re.sub(r",\n\s*", ", ", m.group(1)) + "]", text)
    return text + "\n"


@contextmanager
def locked(path):
    """Cross-process lock so agents, the server and humans never lose writes."""
    lock = path + ".lock"
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except (FileExistsError, PermissionError) as e:
            # Windows also raises PermissionError while another process deletes the lock, so retry until the deadline.
            try:
                if time.time() - os.path.getmtime(lock) > STALE_LOCK_AGE:
                    os.remove(lock)
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                if isinstance(e, PermissionError) and not os.path.exists(lock):
                    raise KanvasError(f"Cannot create the lock file {lock}: permission denied. "
                                      "Check that you can write to the board's folder.")
                raise KanvasError(
                    f"Board is busy (lock file {lock}). "
                    "Delete that file if no other kanvas process is running."
                )
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def write_text_atomic(path, text):
    tmp = f"{path}.{os.getpid()}-{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    try:
        for _ in range(40):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # Windows refuses to replace a file another process is reading.
                time.sleep(0.05)
        os.replace(tmp, path)
    except OSError:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def mutate(path, fn):
    """Load, apply `fn(board)`, and save atomically under the lock.

    Nothing is written when `fn` raises, so failed commands leave the board
    untouched.
    """
    with locked(path):
        board = load_board(path)
        result = fn(board)
        write_text_atomic(path, dumps(board))
    return result


def etag_of(data):
    return '"' + hashlib.sha1(data).hexdigest()[:20] + '"'


# ---------------------------------------------------------------------------
# Board model
# ---------------------------------------------------------------------------


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def task_map(board):
    return {t["id"].upper(): t for t in board["tasks"]}


def find_task(board, ref):
    task = task_map(board).get(str(ref).strip().upper())
    if not task:
        raise KanvasError(f"Task '{ref}' not found")
    return task


def find_group(board, ref):
    key = str(ref).strip().lower()
    for g in board["groups"]:
        if g["id"].lower() == key or g["name"].lower() == key:
            return g
    known = ", ".join(f"{g['name']} [{g['id']}]" for g in board["groups"]) or "none yet"
    raise KanvasError(f"Group '{ref}' not found (groups: {known}). Create it with add-group.")


def group_label(board, gid):
    for g in board["groups"]:
        if g["id"] == gid:
            return f"{g['name']} [{g['id']}]"
    return "Ungrouped" if not gid else f"{gid} (unknown group)"


def unmet_deps(task, tasks):
    """Dependency IDs that are not done yet. Missing tasks count as unmet."""
    out = []
    for dep in task.get("deps", []):
        d = tasks.get(dep.upper())
        if d is None or d.get("state") != "done":
            out.append(dep)
    return out


def yolo_active(task):
    """A YOLO mark counts until its task goes to review: the chain is finished then."""
    return bool(task.get("yolo")) and task.get("state") not in ("review", "done")


def yolo_cover(board):
    """Map task ID (upper case) -> ID of the YOLO task covering it.

    A human sets `yolo` on a task; until that task goes to review, it covers
    itself and everything it transitively depends on. Covered proposals count
    as approved, and finishing a covered task marks it done without review.
    Only the last YOLO task of a chain goes to review: a YOLO task that another
    one depends on is covered by it like the rest of the chain.
    """
    tasks = task_map(board)
    active = [t for t in board["tasks"] if yolo_active(t)]
    reach = [(t, _upstream(tasks, t)) for t in active]
    inside = set().union(*(up for _, up in reach))
    cover = {}
    for t, up in reach:
        if t["id"].upper() in inside:
            continue
        cover.setdefault(t["id"].upper(), t["id"])
        for k in up:
            cover.setdefault(k, t["id"])
    return cover


def _upstream(tasks, task):
    """Upper-case IDs of everything `task` transitively depends on."""
    seen, stack = set(), [x.upper() for x in task.get("deps", [])]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        d = tasks.get(cur)
        if d:
            stack.extend(x.upper() for x in d.get("deps", []))
    return seen


def view_state(task, tasks, cover=None):
    """Stored state, with 'blocked' derived for to-do tasks waiting on deps.

    Pass `cover` (from yolo_cover) to treat proposals under YOLO as approved.
    """
    state = task.get("state", "todo")
    if state == "proposed" and cover and task["id"].upper() in cover:
        state = "todo"
    if state == "todo" and unmet_deps(task, tasks):
        return "blocked"
    return state


def depends_on(board, task_id, target_id):
    """True if `task_id` is or transitively depends on `target_id`."""
    tasks = task_map(board)
    target = target_id.upper()
    seen, stack = set(), [task_id.upper()]
    while stack:
        cur = stack.pop()
        if cur == target:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        t = tasks.get(cur)
        if t:
            stack.extend(d.upper() for d in t.get("deps", []))
    return False


def next_id(board, prefix):
    prefix = prefix.upper()
    highest = 0
    for t in board["tasks"]:
        head, _, num = t["id"].rpartition("-")
        if head.upper() == prefix and num.isdigit():
            highest = max(highest, int(num))
    return f"{prefix}-{highest + 1:02d}"


def suggest_prefix(name, taken):
    """Short, readable ID prefix for a group: Research -> RS, Report Writing -> RW."""
    taken = {p.upper() for p in taken}
    words = [w.upper() for w in re.findall(r"[A-Za-z0-9]+", name)]
    candidates = []
    if len(words) >= 2:
        candidates.append("".join(w[0] for w in words[:3]))
    if words:
        w = words[0]
        candidates += [w[0] + c for c in w[1:] if c not in "AEIOU"]
        candidates += [w[:2], w[:3]]
    for c in candidates:
        if PREFIX_RE.match(c) and c not in taken:
            return c
    letter = next((ch for w in words for ch in w if ch.isalpha()), UNGROUPED_PREFIX)
    n = 2
    while f"{letter}{n}" in taken:
        n += 1
    return f"{letter}{n}"


def add_group(board, name, prefix=None):
    """Create a group, or return the existing one with that name."""
    name = " ".join(str(name or "").split())
    if not name:
        raise KanvasError("Group name is empty")
    for g in board["groups"]:
        if g["name"].lower() == name.lower():
            return g, False
    taken = [g["id"] for g in board["groups"]]
    if prefix:
        prefix = prefix.strip().upper()
        if not PREFIX_RE.match(prefix):
            raise KanvasError(f"Invalid prefix '{prefix}': use 1-6 letters or digits, starting with a letter")
        if prefix in taken:
            raise KanvasError(f"Prefix '{prefix}' is already used by another group")
    else:
        prefix = suggest_prefix(name, taken)
    group = {"id": prefix, "name": name}
    board["groups"].append(group)
    return group, True


def add_task(board, title, group=None, desc="", deps=(), state="proposed"):
    title = " ".join(str(title or "").split())
    if not title:
        raise KanvasError("Task title is empty")
    tasks = task_map(board)
    dep_ids = []
    for ref in deps:
        d = tasks.get(str(ref).strip().upper())
        if d is None:
            raise KanvasError(f"Dependency '{ref}' not found")
        if d["id"] not in dep_ids:
            dep_ids.append(d["id"])
    task = {
        "id": next_id(board, group["id"] if group else UNGROUPED_PREFIX),
        "title": title,
        "state": state,
        "group": group["id"] if group else None,
        "deps": dep_ids,
        "desc": (desc or "").strip(),
        "notes": [],
    }
    board["tasks"].append(task)
    return task


def add_note(task, text, by):
    text = (text or "").strip()
    if not text:
        raise KanvasError("Note text is empty")
    task.setdefault("notes", []).append({"at": now_iso(), "by": by, "text": text})


def find_cycles(board):
    """Each dependency cycle once, as a list of IDs (A depends on B depends on ...)."""
    tasks = task_map(board)
    white, gray, black = 0, 1, 2
    color = dict.fromkeys(tasks, white)
    cycles, seen = [], set()

    def deps(key):
        return iter([d.upper() for d in tasks[key].get("deps", [])])

    for root in tasks:
        if color[root] != white:
            continue
        color[root] = gray
        path, stack = [root], [(root, deps(root))]
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                color[node] = black
                stack.pop()
                path.pop()
            elif nxt not in tasks:
                continue
            elif color[nxt] == gray:
                cycle = path[path.index(nxt):]
                if len(cycle) > 1 and frozenset(cycle) not in seen:
                    seen.add(frozenset(cycle))
                    cycles.append([tasks[k]["id"] for k in cycle])
            elif color[nxt] == white:
                color[nxt] = gray
                path.append(nxt)
                stack.append((nxt, deps(nxt)))
    return cycles


def structural_errors(board):
    """Problems that make the board unusable for the CLI."""
    errors, seen = [], set()
    group_ids = set()
    for g in board["groups"]:
        if not PREFIX_RE.match(g["id"]):
            errors.append(f"Group '{g['name']}' has an invalid id '{g['id']}'")
        elif g["id"] in group_ids:
            errors.append(f"Duplicate group id {g['id']}")
        group_ids.add(g["id"])
    for t in board["tasks"]:
        tid = t["id"]
        if not ID_RE.match(tid):
            errors.append(f"Invalid task id '{tid}' (expected PREFIX-NN, e.g. DV-03)")
        elif tid.upper() in seen:
            errors.append(f"Duplicate task id {tid}")
        seen.add(tid.upper())
        if t.get("state") not in STATES:
            errors.append(f"{tid}: unknown state '{t.get('state')}' (use one of {', '.join(STATES)})")
    return errors


def problems(board):
    """Return (errors, warnings) describing everything wrong with the board."""
    errors = structural_errors(board)
    warnings = []
    tasks = task_map(board)
    group_ids = {g["id"] for g in board["groups"]}
    for t in board["tasks"]:
        tid = t["id"]
        if t.get("group") and t["group"] not in group_ids:
            warnings.append(f"{tid}: unknown group '{t['group']}'")
        for dep in t.get("deps", []):
            if dep.upper() == tid.upper():
                errors.append(f"{tid} depends on itself")
            elif dep.upper() not in tasks:
                errors.append(f"{tid} depends on missing task {dep}")
        if t.get("state") in ("doing", "review", "done"):
            pending = [d for d in unmet_deps(t, tasks) if d.upper() in tasks]
            if pending:
                warnings.append(f"{tid} is {t['state']} but depends on unfinished {', '.join(pending)}")
    for cycle in find_cycles(board):
        errors.append("Circular dependency: " + " -> ".join(cycle + [cycle[0]]))
    return errors, warnings


def chain_depths(deps):
    """Longest dependency chain below each node of {node: [dep, ...]} (cycle-safe)."""
    depth = {}
    for root in deps:
        stack, on_path = [(root, False)], set()
        while stack:
            key, expanded = stack.pop()
            if expanded:
                on_path.discard(key)
                depth[key] = max([depth[d] + 1 for d in deps[key] if d in depth] + [0])
            elif key not in depth and key not in on_path:
                on_path.add(key)
                stack.append((key, True))
                stack.extend((d, False) for d in deps[key] if d in deps and d not in depth)
    return depth


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def board_path(args, must_exist=True):
    path = getattr(args, "board", None) or os.environ.get("KANVAS_BOARD") or find_board()
    if not path:
        raise KanvasError(
            f"No {BOARD_FILE} found here or in any parent directory. "
            f"Create one with `{command_line('init')}`, or pass --board PATH."
        )
    path = os.path.abspath(path)
    if must_exist and not os.path.isfile(path):
        raise KanvasError(f"Board not found: {path}")
    return path


def actor(args):
    return getattr(args, "as_name", None) or os.environ.get("KANVAS_AGENT") or "agent"


def read_input(value, use_stdin=False, file_path=None, what="text"):
    """Text from an argument, stdin, or a file.

    stdin and files are decoded as UTF-8 on every platform, and they avoid
    shells that mangle multi-line arguments (notably on Windows).
    """
    if sum([value is not None, bool(use_stdin), bool(file_path)]) > 1:
        raise KanvasError(f"Give the {what} only once: as an argument, with --stdin, or with --file")
    if use_stdin:
        text = sys.stdin.buffer.read().decode("utf-8-sig")
    elif file_path:
        try:
            text = read_text_file(file_path)
        except OSError as e:
            raise KanvasError(f"Cannot read {file_path}: {e}")
    else:
        text = value or ""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def rel(path):
    try:
        r = os.path.relpath(path)
    except ValueError:
        return path
    return path if r.startswith("..") else r


def id_width(tasks):
    return max([len(t["id"]) for t in tasks] + [5])


def yolo_mark(task, cover):
    return "  (YOLO)" if cover and task["state"] not in ("review", "done") and task["id"].upper() in cover else ""


def print_rows(rows, tasks_by_id, indent="  ", show_state=True, cover=None):
    w = id_width(rows)
    for t in rows:
        state = f"{view_state(t, tasks_by_id, cover):<10}" if show_state else ""
        print(f"{indent}{t['id']:<{w}}  {state}{t['title']}{yolo_mark(t, cover)}")


def print_task(board, task):
    tasks = task_map(board)
    cover = yolo_cover(board)
    print(f"{task['id']}  {task['title']}")
    print(f"State:  {view_state(task, tasks, cover)}")
    print(f"Group:  {group_label(board, task.get('group'))}")
    via = cover.get(task["id"].upper())
    if via and task["state"] not in ("review", "done"):
        if via == task["id"]:
            print("YOLO:   set on this task; you may start it without approval, and finishing sends it to review")
        else:
            print(f"YOLO:   through {via}; you may start it without approval, and finishing marks it done (no review)")
    if task.get("deps"):
        print("Depends on:")
        for dep in task["deps"]:
            d = tasks.get(dep.upper())
            if d is None:
                print(f"  [?] {dep}  (missing)")
            else:
                mark = "x" if d["state"] == "done" else " "
                print(f"  [{mark}] {d['id']}  {d['title']}  ({view_state(d, tasks, cover)}){yolo_mark(d, cover)}")
    blocks = [t for t in board["tasks"] if task["id"].upper() in (d.upper() for d in t.get("deps", []))]
    if blocks:
        print("Blocks:")
        for t in blocks:
            print(f"  {t['id']}  {t['title']}  ({view_state(t, tasks, cover)}){yolo_mark(t, cover)}")
    print("\nDescription:")
    print(_indent(task.get("desc") or "(none)"))
    if task.get("notes"):
        print("\nNotes:")
        for n in task["notes"]:
            edited = f", edited {_when(n['edited'])}" if n.get("edited") else ""
            print(f"  {_when(n.get('at', ''))}  {n.get('by', '?')}{edited}:")
            print(_indent(n.get("text", ""), "    "))


def _when(iso):
    return str(iso).replace("T", " ").replace("Z", " UTC")


def _indent(text, prefix="  "):
    return "\n".join(prefix + line if line else line for line in text.split("\n"))


STATE_HINTS = {
    "proposed": "it is a proposal; a human must approve it first",
    "doing": "it is already in progress",
    "review": "it is waiting for human review",
    "done": "it is done",
    "blocked": "it is blocked",
}


def kanvas_on_path():
    """True if the `kanvas` command is there to type after this run.

    uvx and `uv run` (which set UV) put the running environment on PATH only while it runs.
    """
    found = shutil.which("kanvas")
    if not found or not os.environ.get("UV"):
        return bool(found)
    inside = os.path.normcase(os.path.join(os.path.abspath(sys.prefix), ""))
    return not os.path.normcase(os.path.abspath(found)).startswith(inside)


def command_line(rest=""):
    """The command to type to run Kanvas the way it was started, followed by `rest`."""
    if INSTALLED:
        head = "kanvas" if kanvas_on_path() else "uvx kanvas-board"
    else:
        script = sys.argv[0] or "kanvas.py"
        head = f'python "{script}"' if " " in script else f"python {script}"
    return f"{head} {rest}" if rest else head


def project_command(rest=""):
    """What to type inside a project after `init`: the installed command, or the project's own copy."""
    head = "kanvas" if INSTALLED and kanvas_on_path() else "python .agents/skills/kanvas/kanvas.py"
    return f"{head} {rest}" if rest else head


def _version_key(version):
    """Sortable form of a release number: 0.4.0a1 < 0.4.0b1 < 0.4.0rc1 < 0.4.0 < 0.4.1."""
    m = re.fullmatch(r"(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?", version.strip())
    if not m:
        return None
    numbers = tuple(int(part) for part in m.group(1).split("."))
    numbers += (0,) * (3 - len(numbers))
    stage = ("a", "b", "rc").index(m.group(2)) if m.group(2) else 3
    return numbers, stage, int(m.group(3) or 0)


def agent_copy_hint(board_path):
    """A line saying the project's agent copy differs from the installed command, or None."""
    if not INSTALLED:
        return None
    copy = os.path.join(os.path.dirname(board_path), ".agents", "skills", "kanvas", "kanvas.py")
    try:
        m = re.search(r'^VERSION = "([^"]+)"', read_text_file(copy), re.M)
    except (OSError, ValueError):
        return None
    theirs, ours = _version_key(m.group(1)) if m else None, _version_key(VERSION)
    if theirs is None or ours is None or theirs == ours:
        return None
    if theirs < ours:
        return f"This project's agent copy is {m.group(1)}; run `{command_line('init')}` to update it to {VERSION}."
    return (f"This project's agent copy is {m.group(1)}, newer than your kanvas {VERSION}; "
            "update it with `uv tool upgrade kanvas-board`.")


# ---------------------------------------------------------------------------
# Read-only commands
# ---------------------------------------------------------------------------


def cmd_status(args):
    path = board_path(args)
    board = load_board(path)
    tasks = task_map(board)
    cover = yolo_cover(board)
    buckets = {k: [] for k in ("doing", "review", "todo", "blocked", "proposed", "done")}
    for t in board["tasks"]:
        buckets.setdefault(view_state(t, tasks, cover), []).append(t)
    total = len(board["tasks"])
    print(f"{board.get('title') or 'Kanvas'}  ({rel(path)})")
    if not total:
        print("No tasks yet. Propose some with `propose` or `batch`.")
    else:
        names = {"todo": "ready"}
        counts = ", ".join(f"{len(v)} {names.get(k, k)}" for k, v in buckets.items() if v)
        print(f"{total} tasks: {counts}")
    if board["groups"]:
        parts = []
        for g in board["groups"]:
            members = [t for t in board["tasks"] if t.get("group") == g["id"]]
            done = sum(1 for t in members if t["state"] == "done")
            parts.append(f"{g['name']} [{g['id']}] {done}/{len(members)}")
        print("Groups: " + ", ".join(parts))
    yolo = [t["id"] for t in board["tasks"] if cover.get(t["id"].upper()) == t["id"]]
    if yolo:
        names = ", ".join(yolo)
        print(f"YOLO on {names}: tasks marked (YOLO) need no approval and finishing them marks them done; "
              f"{names} {'itself goes' if len(yolo) == 1 else 'themselves go'} to review.")
    sections = (
        ("doing", "Doing"),
        ("review", "Waiting for human review"),
        ("todo", "Ready to start"),
        ("blocked", "Blocked"),
        ("proposed", "Proposed, awaiting human approval"),
    )
    for key, label in sections:
        rows = buckets[key]
        if not rows:
            continue
        print(f"\n{label} ({len(rows)})")
        w = id_width(rows)
        for t in rows:
            line = f"  {t['id']:<{w}}  {t['title']}"
            if key == "blocked":
                line += f"  (waiting on {', '.join(unmet_deps(t, tasks))})"
            line += yolo_mark(t, cover)
            print(line)
    errors, warnings = problems(board)
    if errors or warnings:
        print("\nProblems")
        for e in errors:
            print(f"  error: {e}")
        for w in warnings:
            print(f"  warning: {w}")
    if hint := agent_copy_hint(path):
        print(f"\n{hint}")


def cmd_list(args):
    board = load_board(board_path(args))
    tasks = task_map(board)
    cover = yolo_cover(board)
    rows = board["tasks"]
    groups = list(board["groups"])
    flt = (args.filter or "").strip().lower()
    if flt:
        state = "todo" if flt == "ready" else flt
        if state in STATES or state == "blocked":
            rows = [t for t in rows if view_state(t, tasks, cover) == state]
        else:
            g = find_group(board, args.filter)
            groups = [g]
            rows = [t for t in rows if t.get("group") == g["id"]]
    if not rows:
        print("No matching tasks.")
        return
    known = {g["id"] for g in board["groups"]}
    sections = [(f"{g['name']} [{g['id']}]", [t for t in rows if t.get("group") == g["id"]]) for g in groups]
    sections.append(("Ungrouped", [t for t in rows if t.get("group") not in known]))
    first = True
    for label, members in sections:
        if not members:
            continue
        if not first:
            print()
        first = False
        print(label)
        print_rows(members, tasks, cover=cover)


def cmd_show(args):
    board = load_board(board_path(args))
    print_task(board, find_task(board, args.id))


def cmd_ready(args):
    board = load_board(board_path(args))
    tasks = task_map(board)
    cover = yolo_cover(board)
    ready = [t for t in board["tasks"] if view_state(t, tasks, cover) == "todo"]
    if ready:
        print_rows(ready, tasks, indent="", show_state=False, cover=cover)
        return
    states = Counter(view_state(t, tasks, cover) for t in board["tasks"])
    hints = [f"{states[k]} {k}" for k in ("doing", "review", "blocked", "proposed") if states[k]]
    print("No tasks ready to start." + (f" ({', '.join(hints)})" if hints else ""))
    if states["proposed"]:
        print("Ask the human to approve proposals in the board.")
    elif states["review"]:
        print("Ask the human to review finished tasks.")


def cmd_blocked(args):
    board = load_board(board_path(args))
    tasks = task_map(board)
    cover = yolo_cover(board)
    blocked = [t for t in board["tasks"] if view_state(t, tasks, cover) == "blocked"]
    if not blocked:
        print("No blocked tasks.")
        return
    for t in blocked:
        print(f"{t['id']}  {t['title']}")
        for dep in unmet_deps(t, tasks):
            d = tasks.get(dep.upper())
            print(f"  waiting on {dep}  " + (f"{d['title']}  ({view_state(d, tasks, cover)})" if d else "(missing)"))


def cmd_check(args):
    path = board_path(args)
    board = load_board(path)
    errors, warnings = problems(board)
    for e in errors:
        print(f"error: {e}")
    for w in warnings:
        print(f"warning: {w}")
    if not errors:
        print(f"OK: {len(board['tasks'])} tasks in {len(board['groups'])} groups ({rel(path)})")
    return 1 if errors else 0


# ---------------------------------------------------------------------------
# Agent write commands
# ---------------------------------------------------------------------------


def cmd_start(args):
    path = board_path(args)
    note = read_input(args.message, what="note") if args.message else ""

    def op(board):
        tasks = task_map(board)
        t = find_task(board, args.id)
        vs = view_state(t, tasks, yolo_cover(board))
        if vs == "blocked":
            raise KanvasError(f"{t['id']} is blocked by unfinished {', '.join(unmet_deps(t, tasks))}")
        if vs != "todo":
            raise KanvasError(f"{t['id']} cannot be started: {STATE_HINTS[vs]}")
        was = t["state"]
        t["state"] = "doing"
        if note:
            add_note(t, note, actor(args))
        return board, t, was

    board, t, was = mutate(path, op)
    print(f"Started {t['id']} ({was} -> doing{', approved by YOLO' if was == 'proposed' else ''})\n")
    print_task(board, t)


def cmd_finish(args):
    path = board_path(args)
    summary = read_input(args.message, args.stdin, args.file, "summary")
    if not summary:
        raise KanvasError(
            "finish needs a summary of what you did, e.g. "
            f'`{command_line("finish " + args.id)} -m "Added X; tests pass"` (or pipe it with --stdin)'
        )

    def op(board):
        t = find_task(board, args.id)
        if t["state"] != "doing":
            raise KanvasError(f"{t['id']} is {view_state(t, task_map(board))}; only tasks in progress can be finished")
        add_note(t, summary, actor(args))
        via = yolo_cover(board).get(t["id"].upper())
        t["state"] = "done" if via and via.upper() != t["id"].upper() else "review"
        return t, via

    t, via = mutate(path, op)
    if t["state"] == "done":
        print(f"Finished {t['id']} (doing -> done). YOLO covers it through {via}, so it skips review.")
    elif via:
        print(f"Finished {t['id']} (doing -> review). YOLO is set on it, so a human reviews it; its YOLO chain is complete.")
    else:
        print(f"Finished {t['id']} (doing -> review). A human will review it.")


def cmd_pause(args):
    path = board_path(args)
    note = read_input(args.message, args.stdin, args.file, "note")

    def op(board):
        t = find_task(board, args.id)
        if t["state"] != "doing":
            raise KanvasError(f"{t['id']} is {view_state(t, task_map(board))}; only tasks in progress can be paused")
        t["state"] = "todo"
        if note:
            add_note(t, note, actor(args))
        return t

    t = mutate(path, op)
    print(f"Paused {t['id']} (doing -> todo)")


def cmd_note(args):
    path = board_path(args)
    text = read_input(args.text, args.stdin, args.file, "note")
    if not text:
        raise KanvasError("Note text is empty")

    def op(board):
        t = find_task(board, args.id)
        if t["state"] == "done":
            raise KanvasError(f"{t['id']} is done; propose a follow-up task instead")
        add_note(t, text, actor(args))
        return t

    t = mutate(path, op)
    count = len(t["notes"])
    print(f"Added a note to {t['id']} ({count} note{'' if count == 1 else 's'})")


def cmd_propose(args):
    path = board_path(args)
    desc = read_input(args.desc, args.stdin, args.file, "description")

    def op(board):
        group = None if args.group in ("-", "") else find_group(board, args.group)
        return add_task(board, args.title, group, desc, args.deps or [])

    t = mutate(path, op)
    deps = f", depends on {', '.join(t['deps'])}" if t["deps"] else ""
    print(f"Proposed {t['id']}: {t['title']}{deps}. A human must approve it before work starts.")


def cmd_add_group(args):
    path = board_path(args)
    g, created = mutate(path, lambda board: add_group(board, args.name, args.prefix))
    if created:
        print(f"Created group {g['name']} (task IDs {g['id']}-01, {g['id']}-02, ...)")
    else:
        print(f"Group {g['name']} [{g['id']}] already exists")


def resolve_ref(board, ref, batch_titles):
    ref = str(ref).strip()
    t = task_map(board).get(ref.upper())
    if t:
        return t["id"]
    if ref.lower() in batch_titles:
        return batch_titles[ref.lower()]
    matches = [t for t in board["tasks"] if t["title"].lower() == ref.lower()]
    if len(matches) == 1:
        return matches[0]["id"]
    if len(matches) > 1:
        raise KanvasError(f"Dependency '{ref}' matches several tasks; use an ID ({', '.join(t['id'] for t in matches)})")
    raise KanvasError(f"Dependency '{ref}' not found (use a task ID, or the title of an earlier task in this batch)")


def cmd_batch(args):
    path = board_path(args)
    if not args.file and sys.stdin.isatty():
        raise KanvasError("Pipe the batch JSON into stdin, or pass --file PATH")
    raw = read_input(None, use_stdin=not args.file, file_path=args.file, what="batch")
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as e:
        raise KanvasError(f"Batch is not valid JSON: {e}")
    if isinstance(spec, list):
        spec = {"tasks": spec}
    if not isinstance(spec, dict):
        raise KanvasError('Batch must be a JSON object: {"groups": [...], "tasks": [...]}')

    def op(board):
        new_groups, new_tasks, titles = [], [], {}
        for g in spec.get("groups", []):
            name, prefix = (g, None) if isinstance(g, str) else (g.get("name"), g.get("prefix"))
            group, created = add_group(board, name, prefix)
            if created:
                new_groups.append(group)
        for i, item in enumerate(spec.get("tasks", []), 1):
            if not isinstance(item, dict) or not str(item.get("title", "")).strip():
                raise KanvasError(f"Task #{i} in the batch has no title")
            group = find_group(board, item["group"]) if item.get("group") else None
            refs = item.get("deps", item.get("depends_on")) or []
            if isinstance(refs, str):
                refs = [refs]
            try:
                deps = [resolve_ref(board, r, titles) for r in refs]
            except KanvasError as e:
                raise KanvasError(f"Task '{item['title']}': {e}")
            desc = item.get("desc", item.get("description", ""))
            t = add_task(board, item["title"], group, desc, deps)
            titles[t["title"].lower()] = t["id"]
            new_tasks.append(t)
        return new_groups, new_tasks

    new_groups, new_tasks = mutate(path, op)
    for g in new_groups:
        print(f"+ group {g['name']} [{g['id']}]")
    for t in new_tasks:
        deps = f"  (after {', '.join(t['deps'])})" if t["deps"] else ""
        print(f"+ {t['id']}  {t['title']}{deps}")
    print(f"Proposed {len(new_tasks)} tasks. A human must approve them before work starts.")


def cmd_depend(args):
    path = board_path(args)

    def op(board):
        t = find_task(board, args.id)
        if t["state"] == "done":
            raise KanvasError(f"{t['id']} is done; dependencies can no longer change")
        added = []
        for ref in args.on:
            d = find_task(board, ref)
            if d["id"] in t["deps"]:
                continue
            if depends_on(board, d["id"], t["id"]):
                raise KanvasError(f"{t['id']} cannot depend on {d['id']}: that would create a circular dependency")
            t["deps"].append(d["id"])
            added.append(d["id"])
        return t, added, view_state(t, task_map(board))

    t, added, vs = mutate(path, op)
    if not added:
        print(f"{t['id']} already depends on {', '.join(args.on)}")
        return
    print(f"{t['id']} now depends on {', '.join(added)}" + (" (blocked until they are done)" if vs == "blocked" else ""))


def find_proposal(board, ref, cover):
    """A task agents may still change: proposed, and not approved by YOLO."""
    t = find_task(board, ref)
    if t["state"] != "proposed":
        raise KanvasError(f"{t['id']} is approved ({t['state']}); only a human can change it now, in the board")
    if t["id"].upper() in cover:
        raise KanvasError(f"{t['id']} is under YOLO, which counts as approval; only a human can change it now")
    return t


def cmd_edit(args):
    path = board_path(args)
    has_desc = args.new_desc is not None or args.stdin or bool(args.file)
    desc = read_input(args.new_desc, args.stdin, args.file, "description") if has_desc else None
    note = read_input(args.message, what="note") if args.message else ""
    if args.title is None and args.group is None and not has_desc:
        raise KanvasError("Nothing to change: give --title, --group, or a description (--desc, --stdin or --file)")

    def op(board):
        t = find_proposal(board, args.id, yolo_cover(board))
        changed = []
        if args.title is not None:
            title = " ".join(args.title.split())
            if not title:
                raise KanvasError("Task title is empty")
            if title != t["title"]:
                t["title"] = title
                changed.append("title")
        if args.group is not None:
            gid = None if args.group in ("-", "") else find_group(board, args.group)["id"]
            if gid != t.get("group"):
                t["group"] = gid
                changed.append("group")
        if desc is not None and desc != t.get("desc", ""):
            t["desc"] = desc
            changed.append("description")
        if note:
            add_note(t, note, actor(args))
        return t, changed

    t, changed = mutate(path, op)
    what = " and ".join([", ".join(changed[:-1]), changed[-1]] if len(changed) > 1 else changed) or "nothing (it already read that way)"
    print(f"Revised {t['id']}: {what}{', with a note' if note else ''}")


def cmd_withdraw(args):
    path = board_path(args)

    def op(board):
        cover = yolo_cover(board)
        gone = []
        for ref in args.ids:
            t = find_proposal(board, ref, cover)
            if t["id"] not in gone:
                gone.append(t["id"])
        keys = {g.upper() for g in gone}
        for t in board["tasks"]:
            if t["id"].upper() in keys:
                continue
            waiting = [d for d in t.get("deps", []) if d.upper() in keys]
            if waiting and t["state"] != "proposed":
                raise KanvasError(f"{t['id']} is approved and depends on {', '.join(waiting)}; "
                                  "ask the human to remove that dependency first")
        rewired = []
        for t in board["tasks"]:
            kept = [d for d in t.get("deps", []) if d.upper() not in keys]
            if len(kept) != len(t.get("deps", [])):
                t["deps"] = kept
                rewired.append(t["id"])
        board["tasks"] = [t for t in board["tasks"] if t["id"].upper() not in keys]
        rewired = [r for r in rewired if r.upper() not in keys]
        return gone, rewired

    gone, rewired = mutate(path, op)
    print(f"Withdrew {', '.join(gone)}" + (f"; {', '.join(rewired)} no longer depend on them" if rewired else ""))


def cmd_undepend(args):
    path = board_path(args)

    def op(board):
        t = find_proposal(board, args.id, yolo_cover(board))
        keys = {str(r).strip().upper() for r in args.on}
        removed = [d for d in t["deps"] if d.upper() in keys]
        missing = [r for r in args.on if str(r).strip().upper() not in {d.upper() for d in t["deps"]}]
        if missing:
            raise KanvasError(f"{t['id']} does not depend on {', '.join(missing)}")
        t["deps"] = [d for d in t["deps"] if d.upper() not in keys]
        return t, removed

    t, removed = mutate(path, op)
    print(f"{t['id']} no longer depends on {', '.join(removed)}")


# ---------------------------------------------------------------------------
# Setup, migration and sharing
# ---------------------------------------------------------------------------


AGENTS_LINE = "Plan and track work on the Kanvas board; follow .agents/skills/kanvas/SKILL.md."


def find_git_top(start=None):
    """Return the top folder of the git repository around `start`, or None."""
    d = os.path.abspath(start or os.getcwd())
    while True:
        # A .git file marks worktrees and submodules.
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def project_folder(start=None):
    """Return the folder to set up when none is given, and the reason when it is not `start` itself.

    An existing board wins, so `init` never creates a second board above it.
    """
    here = os.path.abspath(start or os.getcwd())
    board = find_board(here)
    if board:
        found, reason = os.path.dirname(board), f"the folder of the existing {BOARD_FILE}"
    else:
        found, reason = find_git_top(here), "the top folder of this git repository"
    if not found or found == here:
        return here, None
    return found, reason


def points_to_skill(folder):
    """True if an AGENTS.md or CLAUDE.md in `folder` already sends agents to the Kanvas skill."""
    for name in os.listdir(folder):
        if name.lower() in ("agents.md", "claude.md"):
            try:
                with open(os.path.join(folder, name), "rb") as f:
                    if b".agents/skills/kanvas/SKILL.md" in f.read():
                        return True
            except OSError:
                pass
    return False


def cmd_init(args):
    if args.dir is None:
        init_project(*project_folder())
    else:
        init_project(os.path.abspath(args.dir))


def init_project(target, reason=None, next_steps=True):
    """Set up Kanvas in `target`, print what was done, and return the board's path."""
    if not os.path.isdir(target):
        raise KanvasError(f"Directory does not exist: {target}")
    skill_md = next(
        (p for p in (os.path.join(HERE, "skill", "SKILL.md"), os.path.join(HERE, "SKILL.md")) if os.path.isfile(p)),
        None,
    )
    if not skill_md or not os.path.isfile(HTML_FILE):
        raise KanvasError("kanvas.html and SKILL.md must sit next to kanvas.py (or in skill/)")

    # The skill folders go first: agent sandboxes such as Codex's keep .agents/ read-only,
    # and a refused write should leave the project as it was.
    report = []
    skill_dir = os.path.join(target, ".agents", "skills", "kanvas")
    # Claude Code reads skills only from .claude/skills; the other agents read .agents/skills.
    # Older versions also copied the skill into .cursor and .gemini, and those copies stay current.
    mirrors = [".claude"] + [
        d for d in (".cursor", ".gemini") if os.path.isfile(os.path.join(target, d, "skills", "kanvas", "SKILL.md"))
    ]
    try:
        os.makedirs(skill_dir, exist_ok=True)
        for src, name in ((os.path.abspath(__file__), "kanvas.py"), (HTML_FILE, "kanvas.html"), (skill_md, "SKILL.md")):
            dst = os.path.join(skill_dir, name)
            if os.path.abspath(src) != os.path.abspath(dst):
                shutil.copyfile(src, dst)
        report.append("installed .agents/skills/kanvas/ (kanvas.py, kanvas.html, SKILL.md)")
        for base in mirrors:
            dst_dir = os.path.join(target, base, "skills", "kanvas")
            os.makedirs(dst_dir, exist_ok=True)
            shutil.copyfile(skill_md, os.path.join(dst_dir, "SKILL.md"))
            report.append(f"installed {base}/skills/kanvas/SKILL.md")
    except OSError as e:
        raise KanvasError(
            f"Could not write {os.path.relpath(e.filename or skill_dir, target)}: {e.strerror or e}. "
            "Agent sandboxes often keep .agents/ read-only; run "
            f"`{command_line('init')}` outside the sandbox, or ask the user to run it."
        ) from None

    board_file = os.path.join(target, BOARD_FILE)
    if os.path.exists(board_file):
        report.insert(0, f"kept      {BOARD_FILE}")
    else:
        write_text_atomic(board_file, dumps(new_board(os.path.basename(target))))
        report.insert(0, f"created   {BOARD_FILE}")

    if not any(f.lower() == "agents.md" for f in os.listdir(target)):
        with open(os.path.join(target, "AGENTS.md"), "x", encoding="utf-8", newline="\n") as f:
            f.write(f"# Agent instructions\n\n{AGENTS_LINE}\n")
        report.append("created   AGENTS.md")

    print(f"Kanvas {VERSION} in {target}" + (f", {reason}" if reason else "") + "\n")
    for line in report:
        print(f"  {line}")
    legacy = sorted(f for f in os.listdir(target) if f.endswith(".canvas"))
    if legacy and not load_board(board_file)["tasks"]:
        migrate = project_command(f'import "{legacy[0]}"')
        print(f"\nFound {', '.join(legacy)}. Migrate a Kanvas 0.x Obsidian board with:")
        print(f"  {migrate}")
    if next_steps:
        print("\nOpen the board:")
        print(f"  {project_command('serve')}")
    if not points_to_skill(target):
        print("\nSo agents know about the board in every session, add this line to AGENTS.md or CLAUDE.md:")
        print(f"  {AGENTS_LINE}")
    print("\nNo existing file was modified.")
    return board_file


CANVAS_STATES = {"6": "proposed", "1": "todo", "0": "todo", "2": "doing", "5": "review", "4": "done"}
CANVAS_TASK_RE = re.compile(r"^##\s+([A-Za-z][A-Za-z0-9]*)-(\d+)\s+(.*)$")
CANVAS_SKIP_IDS = {"legend", "canvas-errors", "canvas-warnings", "canvas-lint", "canvas-status"}


def import_canvas(canvas, title=""):
    """Convert an Obsidian Canvas board from Kanvas 0.x into a Kanvas board."""
    nodes = canvas.get("nodes", [])
    group_nodes = [n for n in nodes if n.get("type") == "group"]

    def container(n):
        cx = n.get("x", 0) + n.get("width", 0) / 2
        cy = n.get("y", 0) + n.get("height", 0) / 2
        best, area = None, float("inf")
        for g in group_nodes:
            gx, gy, gw, gh = g.get("x", 0), g.get("y", 0), g.get("width", 0), g.get("height", 0)
            if gx <= cx <= gx + gw and gy <= cy <= gy + gh and gw * gh < area:
                best, area = g, gw * gh
        return best

    items = {}
    for n in nodes:
        if n.get("type") != "text" or n.get("id") in CANVAS_SKIP_IDS:
            continue
        text = (n.get("text") or "").replace("\r\n", "\n")
        first, _, rest = text.partition("\n")
        m = CANVAS_TASK_RE.match(first.strip())
        if not m and (not first.startswith("## ") or n.get("color", "") in ("", "0")):
            continue
        g = container(n)
        items[n["id"]] = {
            "node": n["id"],
            "canvas_group": g["id"] if g else None,
            "id": f"{m.group(1).upper()}-{int(m.group(2)):02d}" if m else None,
            "prefix": m.group(1).upper() if m else None,
            "title": (m.group(3) if m else first.lstrip("#")).strip(),
            "desc": rest.strip(),
            "state": CANVAS_STATES.get(str(n.get("color", "1")), "todo"),
            "deps": [],
        }
    for e in canvas.get("edges", []):
        src, dst = e.get("fromNode"), e.get("toNode")
        if src in items and dst in items and src != dst and src not in items[dst]["deps"]:
            items[dst]["deps"].append(src)
    depth = chain_depths({k: it["deps"] for k, it in items.items()})

    def group_rank(g):
        member_depths = [depth[k] for k, it in items.items() if it["canvas_group"] == g["id"]]
        return (min(member_depths) if member_depths else float("inf"), g.get("x", 0), g.get("y", 0))

    # Upstream groups first, so the board reads left to right.
    board = new_board(title)
    groups = {}
    for gnode in sorted(group_nodes, key=group_rank):
        counts = Counter(it["prefix"] for it in items.values() if it["canvas_group"] == gnode["id"] and it["prefix"])
        prefix = counts.most_common(1)[0][0] if counts else None
        if prefix and (not PREFIX_RE.match(prefix) or any(g["id"] == prefix for g in board["groups"])):
            prefix = None
        groups[gnode["id"]], _ = add_group(board, gnode.get("label") or "Group", prefix)
    rank = {g["id"]: i for i, g in enumerate(board["groups"])}

    def group_id(it):
        g = groups.get(it["canvas_group"])
        return g["id"] if g else None

    def sort_key(it):
        head, _, num = (it["id"] or "~-0").rpartition("-")
        return (rank.get(group_id(it), len(rank)), depth[it["node"]], head, int(num))

    ordered = sorted(items.values(), key=sort_key)
    used = set()
    for it in ordered:
        if it["id"] in used:
            it["id"] = None
        elif it["id"]:
            used.add(it["id"])
    for it in ordered:
        if not it["id"]:
            prefix = group_id(it) or UNGROUPED_PREFIX
            taken = [int(u.rpartition("-")[2]) for u in used if u.rpartition("-")[0] == prefix]
            it["id"] = f"{prefix}-{max(taken + [0]) + 1:02d}"
            used.add(it["id"])
    ids = {it["node"]: it["id"] for it in ordered}
    for it in ordered:
        board["tasks"].append({
            "id": it["id"], "title": it["title"] or it["id"], "state": it["state"],
            "group": group_id(it), "deps": [ids[d] for d in it["deps"]], "desc": it["desc"], "notes": [],
        })
    return board


def cmd_import(args):
    try:
        canvas = json.loads(read_text_file(args.canvas))
    except (OSError, json.JSONDecodeError) as e:
        raise KanvasError(f"Cannot read {args.canvas}: {e}")
    out = os.path.abspath(args.output or os.path.join(os.path.dirname(os.path.abspath(args.canvas)), BOARD_FILE))
    title = os.path.splitext(os.path.basename(args.canvas))[0]
    board = import_canvas(canvas, title)
    if os.path.exists(out) and not args.force:
        existing = load_board(out)
        if existing["tasks"] or existing["groups"]:
            raise KanvasError(f"{rel(out)} already has tasks; pass --force to replace it")
    write_text_atomic(out, dumps(board))
    tasks = task_map(board)
    states = Counter(view_state(t, tasks) for t in board["tasks"])
    summary = ", ".join(f"{v} {k}" for k, v in states.items())
    print(f"Imported {len(board['tasks'])} tasks in {len(board['groups'])} groups into {rel(out)} ({summary})")
    print(f"{rel(args.canvas)} was left untouched; delete it once you are happy with the result.")


def cmd_export(args):
    path = board_path(args)
    board = load_board(path)
    html = read_text_file(HTML_FILE)
    if SNAPSHOT_TAG not in html:
        raise KanvasError("kanvas.html has no snapshot slot")
    data = json.dumps(canonical(board), ensure_ascii=False).replace("<", "\\u003c")
    tag = SNAPSHOT_TAG.replace('json">', f'json" data-at="{now_iso()}">{data}', 1)
    out = os.path.abspath(args.output)
    write_text_atomic(out, html.replace(SNAPSHOT_TAG, tag, 1))
    print(f"Wrote {rel(out)}: a read-only snapshot of {len(board['tasks'])} tasks. Open or share it as is.")


# ---------------------------------------------------------------------------
# Local server for the browser board
# ---------------------------------------------------------------------------


class BoardServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets two servers share a port.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, path, check_host=True, quiet=False):
        super().__init__(address, BoardHandler)
        self.board_path = path
        self.check_host = check_host
        self.quiet = quiet

    def server_bind(self):
        # HTTPServer.server_bind looks up the host's full name with socket.getfqdn,
        # which can take half a minute on macOS. Nothing here uses that name.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class BoardHandler(BaseHTTPRequestHandler):
    server_version = "Kanvas/" + VERSION

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _error(self, code, message):
        self._send(code, json.dumps({"error": message}).encode("utf-8"))

    def _host_allowed(self):
        # Rejecting foreign Host headers blocks DNS-rebinding attacks from web pages.
        if not self.server.check_host:
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        name = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
        return name in ("localhost", "127.0.0.1", "[::1]")

    def _read_board_bytes(self):
        with open(self.server.board_path, "rb") as f:
            return f.read()

    def do_GET(self):
        if not self._host_allowed():
            return self._error(403, "Host not allowed")
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            try:
                with open(HTML_FILE, "rb") as f:
                    body = f.read()
            except OSError:
                return self._error(500, "kanvas.html is missing next to kanvas.py")
            return self._send(200, body, "text/html; charset=utf-8")
        if route == "/api/board":
            try:
                data = self._read_board_bytes()
            except OSError:
                return self._error(404, "Board file is missing")
            tag = etag_of(data)
            if self.headers.get("If-None-Match") == tag:
                return self._send(304, headers={"ETag": tag})
            return self._send(200, data, headers={"ETag": tag})
        return self._error(404, "Not found")

    def do_PUT(self):
        if not self._host_allowed():
            return self._error(403, "Host not allowed")
        if urlparse(self.path).path != "/api/board":
            return self._error(404, "Not found")
        # Requiring JSON forces a CORS preflight, so other sites cannot write.
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._error(415, "Expected application/json")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            return self._error(413, "Board too large")
        try:
            board = parse_board(self.rfile.read(length).decode("utf-8"))
        except (KanvasError, UnicodeDecodeError) as e:
            return self._error(400, str(e))
        errors = structural_errors(board)
        if errors:
            return self._error(400, "; ".join(errors))
        data = dumps(board).encode("utf-8")
        path = self.server.board_path
        try:
            with locked(path):
                current = self._read_board_bytes()
                current_tag = etag_of(current)
                expected = self.headers.get("If-Match")
                if expected and expected != current_tag:
                    return self._send(409, current, headers={"ETag": current_tag})
                if data != current:
                    write_text_atomic(path, data.decode("utf-8"))
                    if not self.server.quiet:
                        print(f"[{time.strftime('%H:%M:%S')}] saved from the browser", flush=True)
        except (KanvasError, OSError) as e:
            return self._error(503, str(e))
        self._send(200, data, headers={"ETag": etag_of(data)})


def start_server(path, host="127.0.0.1", port=8765, quiet=False):
    check_host = host in ("127.0.0.1", "localhost")
    last_error = None
    for p in range(port, port + 20):
        try:
            return BoardServer((host, p), path, check_host, quiet)
        except OSError as e:
            busy = (errno.EADDRINUSE, errno.EACCES, getattr(errno, "WSAEADDRINUSE", -1), getattr(errno, "WSAEACCES", -1))
            if e.errno not in busy:
                raise
            last_error = e
    raise KanvasError(f"No free port between {port} and {port + 19}: {last_error}")


def offer_board():
    """Ask the person at the terminal to set up Kanvas; return the new board's path, or None."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    target, reason = project_folder()
    try:
        answer = input(f"No {BOARD_FILE} here. Create one in {target}? [Y/n] ")
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if answer.strip().lower() not in ("", "y", "yes"):
        return None
    print()
    path = init_project(target, reason, next_steps=False)
    print()
    return path


def cmd_serve(args):
    try:
        path = board_path(args)
    except KanvasError:
        if getattr(args, "board", None) or os.environ.get("KANVAS_BOARD"):
            raise
        path = offer_board()
        if not path:
            raise
    if not os.path.isfile(HTML_FILE):
        raise KanvasError("kanvas.html is missing next to kanvas.py")
    server = start_server(path, args.host, args.port)
    host, port = server.server_address[:2]
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/"
    print(f"Kanvas board: {path}")
    if hint := agent_copy_hint(path):
        print(hint)
    print(f"Open {url}  (Ctrl+C to stop)")
    if not server.check_host:
        print("Warning: listening beyond localhost; anyone who can reach this port can edit the board.")
    if not args.no_open:
        threading.Timer(0.4, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

EPILOG = """\
workflow:  proposed -> todo -> doing -> review -> done
  Agents propose, start, pause, finish, note and add dependencies, and may
  edit, withdraw or rewire proposals until a human approves them.
  Humans approve proposals and mark work done in the board (`serve`).
  A todo task with unfinished dependencies shows as blocked.
  YOLO on a task: it and everything it depends on need no approval;
  finishing them marks them done, and the YOLO task itself goes to review.
  When YOLO tasks follow one another in a chain, only the last one goes to
  review. YOLO ends once its task is in review.

examples:
  %(prog)s status
  %(prog)s start DV-02
  %(prog)s finish DV-02 -m "Implemented the parser; 14 tests pass"
  %(prog)s propose Development "Add retries" "Retry failed uploads" --deps DV-02
"""


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-b", "--board", default=argparse.SUPPRESS, metavar="PATH",
                        help=f"board file (default: nearest {BOARD_FILE})")
    common.add_argument("--as", dest="as_name", default=argparse.SUPPRESS, metavar="NAME",
                        help="name recorded on notes (default: $KANVAS_AGENT or 'agent')")

    prog = "kanvas" if INSTALLED else os.path.basename(sys.argv[0]) or "kanvas.py"
    parser = argparse.ArgumentParser(
        prog=prog, parents=[common], epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Kanvas: a task board shared by humans and AI agents.",
    )
    parser.add_argument("--version", action="version", version=f"Kanvas {VERSION}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name, func, help_text, **kw):
        p = sub.add_parser(name, parents=[common], help=help_text, description=help_text, **kw)
        p.set_defaults(func=func)
        return p

    def text_sources(p, what):
        p.add_argument("--stdin", action="store_true", help=f"read the {what} from stdin (UTF-8)")
        p.add_argument("--file", metavar="PATH", help=f"read the {what} from a file")

    add("status", cmd_status, "board overview (default command)")
    p = add("list", cmd_list, "list tasks, optionally filtered by state or group")
    p.add_argument("filter", nargs="?", help="proposed, todo/ready, blocked, doing, review, done, or a group")
    p = add("show", cmd_show, "task details, dependencies and notes")
    p.add_argument("id")
    add("ready", cmd_ready, "tasks that can be started now")
    add("blocked", cmd_blocked, "blocked tasks and what they wait on")
    add("check", cmd_check, "validate the board (exit code 1 on errors)")

    p = add("start", cmd_start, "start a ready task (todo -> doing)")
    p.add_argument("id")
    p.add_argument("-m", "--message", help="optional note, e.g. your plan")
    p = add("finish", cmd_finish, "hand a task over for review (doing -> review; done when a YOLO task depends on it); "
                                  "needs a summary")
    p.add_argument("id")
    p.add_argument("-m", "--message", help="summary of what you did")
    text_sources(p, "summary")
    p = add("pause", cmd_pause, "stop working on a task (doing -> todo)")
    p.add_argument("id")
    p.add_argument("-m", "--message", help="optional note on why")
    text_sources(p, "note")
    p = add("note", cmd_note, "append a note to a task (keeps the description intact)")
    p.add_argument("id")
    p.add_argument("text", nargs="?")
    text_sources(p, "note")
    p = add("propose", cmd_propose, "propose a new task (a human approves it)")
    p.add_argument("group", help="group name or prefix, or - for none")
    p.add_argument("title")
    p.add_argument("desc", nargs="?")
    p.add_argument("--deps", "--depends-on", nargs="+", metavar="ID", help="tasks this one depends on")
    text_sources(p, "description")
    p = add("add-group", cmd_add_group, "create a group of tasks")
    p.add_argument("name")
    p.add_argument("--prefix", help="task ID prefix (default: derived from the name)")
    p = add("batch", cmd_batch, "propose many groups and tasks from JSON (stdin or --file)")
    p.add_argument("--file", metavar="PATH", help="read the JSON from a file")
    p = add("depend", cmd_depend, "make a task depend on others: depend DV-03 DV-01 DV-02")
    p.add_argument("id")
    p.add_argument("on", nargs="+", metavar="ON")
    p = add("edit", cmd_edit, "revise a proposal before it is approved")
    p.add_argument("id")
    p.add_argument("--title")
    p.add_argument("--group", help="group name or prefix, or - for none")
    p.add_argument("--desc", dest="new_desc", metavar="DESC", help="new description")
    p.add_argument("-m", "--message", help="optional note on why")
    text_sources(p, "description")
    p = add("withdraw", cmd_withdraw, "delete proposals that are not approved yet")
    p.add_argument("ids", nargs="+", metavar="ID")
    p = add("undepend", cmd_undepend, "remove dependencies from a proposal: undepend DV-03 DV-01")
    p.add_argument("id")
    p.add_argument("on", nargs="+", metavar="ON")

    p = add("serve", cmd_serve, "open the interactive board in your browser")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 for containers or remote machines")
    p.add_argument("--no-open", action="store_true", help="do not open a browser")
    p = add("init", cmd_init, "set up Kanvas in a project (never overwrites files)")
    p.add_argument("dir", nargs="?", help=f"project folder (default: the folder of the nearest {BOARD_FILE}, "
                                          "else the top folder of the git repository, else this one)")
    p = add("import", cmd_import, "convert an Obsidian .canvas board from Kanvas 0.x")
    p.add_argument("canvas")
    p.add_argument("-o", "--output", help=f"output file (default: {BOARD_FILE} next to the canvas)")
    p.add_argument("--force", action="store_true", help="replace an existing board that has tasks")
    p = add("export", cmd_export, "write a read-only HTML snapshot to share")
    p.add_argument("output", nargs="?", default="kanvas-snapshot.html")
    return parser


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    # argparse cannot fill an optional positional that follows an option,
    # as in `note DV-01 --as claude "text"`.
    for name in ("text", "desc"):
        if extra and getattr(args, name, False) is None and not extra[0].startswith("-"):
            setattr(args, name, extra.pop(0))
    if extra:
        parser.error("unrecognized arguments: " + " ".join(extra))
    func = getattr(args, "func", cmd_status)
    try:
        return func(args) or 0
    except KanvasError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
