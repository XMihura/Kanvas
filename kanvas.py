#!/usr/bin/env python3
"""Kanvas: a task board shared by humans and AI agents.

The board is a single JSON file (kanvas.json). Agents change it only through
this CLI, which enforces the workflow rules. Humans plan and review in the
browser:

    python kanvas.py serve

Python 3.8+, standard library only. Run `python kanvas.py -h` for commands.
"""

import argparse
import base64
import errno
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse

VERSION = "0.4.0"
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
TASK_KEYS = ("id", "title", "state", "group", "deps", "yolo", "at", "by", "desc", "notes")
REQUIRED_TASK_KEYS = ("id", "title", "state")
GROUP_KEYS = ("id", "name")
NOTE_KEYS = ("at", "by", "edited", "text")
REQUIRED_NOTE_KEYS = ("at", "by", "text")

# A lock older than STALE_LOCK_AGE was left by a crashed process. Writers wait longer than that, so they break it
# and go on; a lock is only held while the board is read and written.
LOCK_TIMEOUT = 10.0
STALE_LOCK_AGE = 5.0
MAX_UPLOAD = 20 * 1024 * 1024
# Images live next to the board in FILES_DIR, each named by a hash of its bytes, and Markdown refers to them by that
# relative path. The same image pasted twice is one file, and a file never changes once written.
FILES_DIR = "kanvas-files"
MAX_IMAGE = 10 * 1024 * 1024
IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
FILE_REF_RE = re.compile(re.escape(FILES_DIR) + r"/([0-9a-f]{16})\.(png|jpg|gif|webp)\b")


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
# Images
# ---------------------------------------------------------------------------


def image_type(data):
    """The extension of a PNG, JPEG, GIF or WebP image, from its first bytes; None for anything else.
    SVG is left out on purpose: it can carry scripts."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def files_dir(board_path):
    return os.path.join(os.path.dirname(os.path.abspath(board_path)), FILES_DIR)


def store_image(board_path, data):
    """Keeps an image next to the board and returns the path Markdown uses for it, as in kanvas-files/0123abcd.png."""
    ext = image_type(data)
    if ext is None:
        raise KanvasError("Only PNG, JPEG, GIF and WebP images can be attached")
    if len(data) > MAX_IMAGE:
        raise KanvasError(f"The image is larger than {MAX_IMAGE // (1024 * 1024)} MB")
    name = f"{hashlib.sha256(data).hexdigest()[:16]}.{ext}"
    folder = files_dir(board_path)
    target = os.path.join(folder, name)
    if not os.path.isfile(target):
        os.makedirs(folder, exist_ok=True)
        tmp = f"{target}.{os.getpid()}-{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, target)
    return f"{FILES_DIR}/{name}"


def image_path(board_path, ref):
    """The file on disk for a path such as kanvas-files/0123abcd.png, or None when the path is not one."""
    m = FILE_REF_RE.fullmatch(ref)
    return os.path.join(files_dir(board_path), f"{m.group(1)}.{m.group(2)}") if m else None


def image_refs(board):
    """The images that descriptions and notes refer to, in order of first use."""
    refs = {}
    for t in board["tasks"]:
        for text in [t.get("desc") or ""] + [n.get("text") or "" for n in t.get("notes", [])]:
            for m in FILE_REF_RE.finditer(str(text)):
                refs.setdefault(m.group(0), t["id"])
    return refs


def image_problems(board_path, board):
    """Warnings about images referred to but missing, and images in the folder that nothing refers to."""
    refs, warnings = image_refs(board), []
    for ref, tid in refs.items():
        if not os.path.isfile(image_path(board_path, ref)):
            warnings.append(f"{tid}: image {ref} is missing")
    try:
        names = sorted(os.listdir(files_dir(board_path)))
    except OSError:
        names = []
    for name in names:
        ref = f"{FILES_DIR}/{name}"
        if FILE_REF_RE.fullmatch(ref) and ref not in refs:
            warnings.append(f"{ref} is not used by any task; delete it if you no longer need it")
    return warnings


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
    itself and everything it transitively depends on, and finishing a covered
    task marks it done without review. Setting YOLO approves the chain's
    proposals (yoloOn in kanvas.html); a proposal linked into the chain later
    stays a proposal until a human approves it.
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


def view_state(task, tasks):
    """Stored state, with 'blocked' derived for to-do tasks waiting on deps."""
    state = task.get("state", "todo")
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
        if head.upper() == prefix and num.isascii() and num.isdigit():
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


def add_task(board, title, group=None, desc="", deps=(), state="proposed", by=None):
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
        "at": now_iso() if by else None,
        "by": by,
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


QUESTION = "Question:"


def open_question(task):
    """The note in which the agent working on `task` asked the human something in its chat, while it waits.

    The next note, from anyone, ends the wait, and so does the task leaving doing (mirrored in kanvas.html).
    """
    notes = task.get("notes") or []
    last = notes[-1] if task.get("state") == "doing" and notes else None
    if last and last.get("by") != "human" and str(last.get("text", "")).startswith(QUESTION):
        return last
    return None


def question_text(note):
    return str(note.get("text", ""))[len(QUESTION):].strip()


CHANGES = "Changes requested:"


def change_request(task):
    """The note in which the human asked for changes to a proposal, until an agent answers it.

    A note from an agent after it answers it, and so does the task leaving proposed (mirrored in kanvas.html).
    """
    if task.get("state") != "proposed":
        return None
    for note in reversed(task.get("notes") or []):
        if note.get("by") != "human":
            return None
        if str(note.get("text", "")).startswith(CHANGES):
            return note
    return None


def change_text(note):
    return str(note.get("text", ""))[len(CHANGES):].strip()


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
    return "  (YOLO)" if cover and task["state"] in ("todo", "doing") and task["id"].upper() in cover else ""


def _gist(text):
    first = text.split("\n", 1)[0]
    return first[:57].rstrip() + "..." if len(first) > 60 else first


def waiting_mark(task):
    q = open_question(task)
    return f"  (waiting for the human: {_gist(question_text(q))})" if q else ""


def changes_mark(task, cover):
    r = change_request(task) if task["id"].upper() not in cover else None
    return f"  (changes requested: {_gist(change_text(r))})" if r else ""


def print_rows(rows, tasks_by_id, indent="  ", show_state=True, cover=None):
    w = id_width(rows)
    for t in rows:
        state = f"{view_state(t, tasks_by_id):<10}" if show_state else ""
        print(f"{indent}{t['id']:<{w}}  {state}{t['title']}{yolo_mark(t, cover)}")


def print_task(board, task):
    tasks = task_map(board)
    cover = yolo_cover(board)
    print(f"{task['id']}  {task['title']}")
    print(f"State:  {view_state(task, tasks)}")
    if q := open_question(task):
        print(f"Waits:  for the human to reply to the question {q.get('by') or 'agent'} asked; the next note ends the wait")
    if changes_mark(task, cover):
        print(f"Revise: the human asked for changes (see the notes); change the proposal with "
              f"`{command_line('edit ' + task['id'])}` and say what you changed with -m, which answers the request")
    print(f"Group:  {group_label(board, task.get('group'))}")
    if task.get("by"):
        print(f"By:     {task['by']}" + (f", {task['at']}" if task.get("at") else ""))
    via = cover.get(task["id"].upper())
    if via and task["state"] == "proposed":
        print(f"YOLO:   joins the chain of {via} once a human approves it")
    elif via and task["state"] not in ("review", "done"):
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
                print(f"  [{mark}] {d['id']}  {d['title']}  ({view_state(d, tasks)}){yolo_mark(d, cover)}")
    blocks = [t for t in board["tasks"] if task["id"].upper() in (d.upper() for d in t.get("deps", []))]
    if blocks:
        print("Blocks:")
        for t in blocks:
            print(f"  {t['id']}  {t['title']}  ({view_state(t, tasks)}){yolo_mark(t, cover)}")
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
        buckets.setdefault(view_state(t, tasks), []).append(t)
    total = len(board["tasks"])
    print(f"{board.get('title') or board_name(path)}  ({rel(path)})")
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
            line += yolo_mark(t, cover) + waiting_mark(t) + changes_mark(t, cover)
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
            rows = [t for t in rows if view_state(t, tasks) == state]
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
    ready = [t for t in board["tasks"] if view_state(t, tasks) == "todo"]
    if ready:
        print_rows(ready, tasks, indent="", show_state=False, cover=cover)
        return
    states = Counter(view_state(t, tasks) for t in board["tasks"])
    hints = [f"{states[k]} {k}" for k in ("doing", "review", "blocked", "proposed") if states[k]]
    print("No tasks ready to start." + (f" ({', '.join(hints)})" if hints else ""))
    if states["proposed"]:
        print("Ask the human to approve proposals in the board.")
    elif states["review"]:
        print("Ask the human to review finished tasks.")


def cmd_blocked(args):
    board = load_board(board_path(args))
    tasks = task_map(board)
    blocked = [t for t in board["tasks"] if view_state(t, tasks) == "blocked"]
    if not blocked:
        print("No blocked tasks.")
        return
    for t in blocked:
        print(f"{t['id']}  {t['title']}")
        for dep in unmet_deps(t, tasks):
            d = tasks.get(dep.upper())
            print(f"  waiting on {dep}  " + (f"{d['title']}  ({view_state(d, tasks)})" if d else "(missing)"))


def cmd_check(args):
    path = board_path(args)
    board = load_board(path)
    errors, warnings = problems(board)
    warnings += image_problems(path, board)
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
    note = read_input(args.message, args.stdin, args.file, "note")

    def op(board):
        tasks = task_map(board)
        t = find_task(board, args.id)
        vs = view_state(t, tasks)
        if vs == "blocked":
            raise KanvasError(f"{t['id']} is blocked by unfinished {', '.join(unmet_deps(t, tasks))}")
        if vs != "todo":
            raise KanvasError(f"{t['id']} cannot be started: {STATE_HINTS[vs]}")
        t["state"] = "doing"
        if note:
            add_note(t, note, actor(args))
        return board, t

    board, t = mutate(path, op)
    print(f"Started {t['id']} (todo -> doing)\n")
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
        waited, requested = open_question(t), change_request(t)
        add_note(t, text, actor(args))
        return t, waited, requested

    t, waited, requested = mutate(path, op)
    count = len(t["notes"])
    print(f"Added a note to {t['id']} ({count} note{'' if count == 1 else 's'})")
    if waited and not open_question(t):
        print(f"{t['id']} no longer waits for the human.")
    if requested and not change_request(t):
        print(f"This note answers the human's request for changes on {t['id']}.")


def cmd_attach(args):
    path = board_path(args)
    text = read_input(args.message, args.stdin, args.file, "note")
    images = []
    for name in args.images:
        try:
            with open(name, "rb") as f:
                data = f.read(MAX_IMAGE + 1)
        except OSError as e:
            raise KanvasError(f"Cannot read {name}: {e}")
        try:
            ref = store_image(path, data)
        except KanvasError as e:
            raise KanvasError(f"{name}: {e}")
        alt = os.path.splitext(os.path.basename(name))[0].replace("[", "(").replace("]", ")")
        images.append(f"![{alt}]({ref})")
    note = "\n\n".join(([text] if text else []) + images)

    def op(board):
        t = find_task(board, args.id)
        if t["state"] == "done":
            raise KanvasError(f"{t['id']} is done; propose a follow-up task instead")
        waited, requested = open_question(t), change_request(t)
        add_note(t, note, actor(args))
        return t, waited, requested

    t, waited, requested = mutate(path, op)
    count = len(images)
    print(f"Attached {count} image{'' if count == 1 else 's'} to {t['id']} in a note. To show "
          f"{'it' if count == 1 else 'them'} in a description too, use: {' '.join(images)}")
    if waited and not open_question(t):
        print(f"{t['id']} no longer waits for the human.")
    if requested and not change_request(t):
        print(f"This note answers the human's request for changes on {t['id']}.")


def cmd_ask(args):
    path = board_path(args)
    text = read_input(args.text, args.stdin, args.file, "question")
    if text.startswith(QUESTION):
        text = text[len(QUESTION):].strip()
    if not text:
        raise KanvasError("The question is empty")

    def op(board):
        t = find_task(board, args.id)
        if t["state"] != "doing":
            raise KanvasError(f"{t['id']} is {view_state(t, task_map(board))}; ask on a task you are working on, "
                              "or only in the conversation")
        add_note(t, f"{QUESTION} {text}", actor(args))
        return t

    t = mutate(path, op)
    print(f"{t['id']} now shows the human that you wait for their reply. Ask the same question in the conversation "
          f"and stop. When they reply, record the answer with `{command_line('note ' + t['id'])}`, which ends the wait.")


def cmd_propose(args):
    path = board_path(args)
    desc = read_input(args.desc, args.stdin, args.file, "description")

    def op(board):
        group = None if args.group in ("-", "") else find_group(board, args.group)
        return add_task(board, args.title, group, desc, args.deps or [], by=actor(args))

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


def batch_items(spec):
    """The groups of a batch as (name, prefix) and its tasks as dicts, after checking the shape of every field."""
    if isinstance(spec, list):
        spec = {"tasks": spec}
    if not isinstance(spec, dict):
        raise KanvasError('Batch must be a JSON object: {"groups": [...], "tasks": [...]}')
    groups, items = spec.get("groups", []), spec.get("tasks", [])
    if not isinstance(groups, list):
        raise KanvasError('Batch "groups" must be a list of group names or {"name": ..., "prefix": ...} objects')
    if not isinstance(items, list):
        raise KanvasError('Batch "tasks" must be a list of task objects, such as {"title": ..., "group": ...}')
    if not groups and not items:
        raise KanvasError('The batch has no groups or tasks: give {"groups": [...], "tasks": [...]}')
    out_groups, out_tasks = [], []
    for i, g in enumerate(groups, 1):
        if isinstance(g, str):
            g = {"name": g}
        if not isinstance(g, dict):
            raise KanvasError(f'Group #{i} in the batch must be a name or a {{"name": ..., "prefix": ...}} object')
        name, prefix = g.get("name"), g.get("prefix")
        if not isinstance(name, str) or not name.strip():
            raise KanvasError(f'Group #{i} in the batch needs a "name" (a string)')
        if prefix is not None and not isinstance(prefix, str):
            raise KanvasError(f'Group \'{name}\': "prefix" must be a string, such as "QA"')
        out_groups.append((name, prefix))
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise KanvasError(f'Task #{i} in the batch must be an object, such as {{"title": ..., "group": ...}}')
        title = item.get("title")
        if title is not None and not isinstance(title, str):
            raise KanvasError(f'Task #{i} in the batch: "title" must be a string')
        if not (title or "").strip():
            raise KanvasError(f"Task #{i} in the batch has no title")
        desc_key = "description" if "description" in item and "desc" not in item else "desc"
        deps_key = "depends_on" if "depends_on" in item and "deps" not in item else "deps"
        desc, group, deps = item.get(desc_key), item.get("group"), item.get(deps_key)
        if desc is not None and not isinstance(desc, str):
            raise KanvasError(f"Task '{title}': \"{desc_key}\" must be a string")
        if group is not None and not isinstance(group, str):
            raise KanvasError(f"Task '{title}': \"group\" must be a string, the name or prefix of a group")
        if isinstance(deps, str):
            deps = [deps]
        if deps is not None and not (isinstance(deps, list) and all(isinstance(r, str) for r in deps)):
            raise KanvasError(f"Task '{title}': \"{deps_key}\" must be a task ID or title, or a list of them")
        out_tasks.append({"title": title, "group": group or "", "desc": desc or "", "deps": deps or []})
    return out_groups, out_tasks


def cmd_batch(args):
    path = board_path(args)
    if not args.file and sys.stdin.isatty():
        raise KanvasError("Pipe the batch JSON into stdin, or pass --file PATH")
    raw = read_input(None, use_stdin=not args.file, file_path=args.file, what="batch")
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as e:
        raise KanvasError(f"Batch is not valid JSON: {e}")
    groups, items = batch_items(spec)

    def op(board):
        new_groups, new_tasks, titles = [], [], {}
        for name, prefix in groups:
            group, created = add_group(board, name, prefix)
            if created:
                new_groups.append(group)
        for item in items:
            try:
                group = find_group(board, item["group"]) if item["group"] else None
                deps = [resolve_ref(board, r, titles) for r in item["deps"]]
            except KanvasError as e:
                raise KanvasError(f"Task '{item['title']}': {e}")
            t = add_task(board, item["title"], group, item["desc"], deps, by=actor(args))
            titles[t["title"].lower()] = t["id"]
            new_tasks.append(t)
        return new_groups, new_tasks

    new_groups, new_tasks = mutate(path, op)
    for g in new_groups:
        print(f"+ group {g['name']} [{g['id']}]")
    for t in new_tasks:
        deps = f"  (after {', '.join(t['deps'])})" if t["deps"] else ""
        print(f"+ {t['id']}  {t['title']}{deps}")
    one = len(new_tasks) == 1
    print(f"Proposed {len(new_tasks)} task{'' if one else 's'}. A human must approve {'it' if one else 'them'} before work starts.")


def cmd_depend(args):
    path = board_path(args)

    def op(board):
        t = find_task(board, args.id)
        if t["state"] == "done":
            raise KanvasError(f"{t['id']} is done; dependencies can no longer change")
        before = yolo_cover(board)
        added = []
        for ref in args.on:
            d = find_task(board, ref)
            if d["id"] in t["deps"]:
                continue
            if depends_on(board, d["id"], t["id"]):
                raise KanvasError(f"{t['id']} cannot depend on {d['id']}: that would create a circular dependency")
            t["deps"].append(d["id"])
            added.append(d["id"])
        cover = yolo_cover(board)
        joined = [x["id"] for x in board["tasks"]
                  if x["state"] == "proposed" and x["id"].upper() in cover and x["id"].upper() not in before]
        return t, added, view_state(t, task_map(board)), joined

    t, added, vs, joined = mutate(path, op)
    if not added:
        print(f"{t['id']} already depends on {', '.join(args.on)}")
        return
    print(f"{t['id']} now depends on {', '.join(added)}" + (" (blocked until they are done)" if vs == "blocked" else ""))
    if joined:
        print(f"YOLO does not approve proposals linked into its chain: ask the human to approve {', '.join(joined)} in the board.")


def find_proposal(board, ref):
    """A task agents may still change: one that is still proposed."""
    t = find_task(board, ref)
    if t["state"] != "proposed":
        raise KanvasError(f"{t['id']} is approved ({t['state']}); only a human can change it now, in the board")
    return t


def cmd_edit(args):
    path = board_path(args)
    has_desc = args.new_desc is not None or args.stdin or bool(args.file)
    desc = read_input(args.new_desc, args.stdin, args.file, "description") if has_desc else None
    note = read_input(args.message, what="note") if args.message else ""
    if args.title is None and args.group is None and not has_desc:
        raise KanvasError("Nothing to change: give --title, --group, or a description (--desc, --stdin or --file)")

    def op(board):
        t = find_proposal(board, args.id)
        requested = change_request(t)
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
        return t, changed, requested

    t, changed, requested = mutate(path, op)
    what = " and ".join([", ".join(changed[:-1]), changed[-1]] if len(changed) > 1 else changed) or "nothing (it already read that way)"
    print(f"Revised {t['id']}: {what}{', with a note' if note else ''}")
    if requested and change_request(t):
        print(f"The human's request for changes on {t['id']} stays open until you say what you changed: "
              f"`{command_line('note ' + t['id'])} \"...\"`, or -m with your next edit.")
    elif requested:
        print(f"This answers the human's request for changes on {t['id']}.")


def cmd_withdraw(args):
    path = board_path(args)

    def op(board):
        gone = []
        for ref in args.ids:
            t = find_proposal(board, ref)
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
        t = find_proposal(board, args.id)
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
    tag = SNAPSHOT_TAG.replace('json">', f'json" data-at="{now_iso()}" data-name="{escape(board_name(path))}">{data}', 1)
    # The images go inside the snapshot, so it stays one file to share.
    images = {}
    for ref in image_refs(board):
        try:
            with open(image_path(path, ref), "rb") as f:
                images[ref] = f"data:{IMAGE_TYPES[ref.rsplit('.', 1)[1]]};base64,{base64.b64encode(f.read()).decode('ascii')}"
        except OSError:
            pass
    if images:
        tag += f'<script id="kanvas-files" type="application/json">{json.dumps(images)}</script>'
    out = os.path.abspath(args.output)
    write_text_atomic(out, html.replace(SNAPSHOT_TAG, tag, 1))
    extra = f" and {len(images)} image{'' if len(images) == 1 else 's'}" if images else ""
    print(f"Wrote {rel(out)}: a read-only snapshot of {len(board['tasks'])} tasks{extra}. Open or share it as is.")


# ---------------------------------------------------------------------------
# QR codes, so a phone can open a board's address
# ---------------------------------------------------------------------------

# Error correction level M, by version: codewords per block, and blocks.
QR_ECC_PER_BLOCK = (None, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26,
                    26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28)
QR_BLOCKS = (None, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16,
             17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49)


def _gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_remainder(data, degree):
    divisor, root = [0] * (degree - 1) + [1], 1
    for _ in range(degree):
        for j in range(degree):
            divisor[j] = _gf_mul(divisor[j], root)
            if j + 1 < degree:
                divisor[j] ^= divisor[j + 1]
        root = _gf_mul(root, 2)
    rem = [0] * degree
    for b in data:
        factor = b ^ rem.pop(0)
        rem.append(0)
        for i, coef in enumerate(divisor):
            rem[i] ^= _gf_mul(coef, factor)
    return rem


def _raw_modules(ver):
    n = (16 * ver + 128) * ver + 64
    if ver >= 2:
        align = ver // 7 + 2
        n -= (25 * align - 10) * align - 55
        if ver >= 7:
            n -= 36
    return n


def qr_matrix(text):
    """The QR code of `text` (byte mode, error correction M) as rows of booleans, True for dark."""
    data = text.encode("utf-8")
    for ver in range(1, 41):
        capacity = _raw_modules(ver) // 8 - QR_ECC_PER_BLOCK[ver] * QR_BLOCKS[ver]
        count_bits = 8 if ver < 10 else 16
        if 4 + count_bits + 8 * len(data) <= capacity * 8:
            break
    else:
        raise KanvasError("Too long for a QR code")
    bits = [0, 1, 0, 0] + [(len(data) >> i) & 1 for i in reversed(range(count_bits))]
    bits += [(b >> i) & 1 for b in data for i in reversed(range(8))]
    bits += [0] * min(4, capacity * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    words += [(0xEC, 0x11)[i % 2] for i in range(capacity - len(words))]

    # Split into blocks, add error correction, and interleave.
    nblocks, ecc = QR_BLOCKS[ver], QR_ECC_PER_BLOCK[ver]
    raw = _raw_modules(ver) // 8
    short, short_len = nblocks - raw % nblocks, raw // nblocks
    blocks, k = [], 0
    for i in range(nblocks):
        part = words[k:k + short_len - ecc + (0 if i < short else 1)]
        k += len(part)
        blocks.append(part + ([0] if i < short else []) + _rs_remainder(part, ecc))
    stream = [blk[i] for i in range(len(blocks[0])) for j, blk in enumerate(blocks)
              if i != short_len - ecc or j >= short]

    size = ver * 4 + 17
    dark = [[False] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def put(x, y, on):
        dark[y][x], fixed[y][x] = on, True

    for i in range(size):
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    put(x, y, max(abs(dx), abs(dy)) not in (2, 4))
    if ver > 1:
        align = ver // 7 + 2
        step = (ver * 8 + align * 3 + 5) // (align * 4 - 4) * 2
        pos = [6] + sorted(size - 7 - i * step for i in range(align - 1))
        for ax in pos:
            for ay in pos:
                if (ax, ay) in ((6, 6), (6, pos[-1]), (pos[-1], 6)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        put(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
    if ver >= 7:
        rem = ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        vbits = ver << 12 | rem
        for i in range(18):
            a, b, on = size - 11 + i % 3, i // 3, (vbits >> i) & 1 == 1
            put(a, b, on)
            put(b, a, on)

    def put_format(mask):
        rem = mask  # level M is 0
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        f = (mask << 10 | rem) ^ 0x5412
        bit = lambda i: (f >> i) & 1 == 1
        for i in range(6):
            put(8, i, bit(i))
        put(8, 7, bit(6))
        put(8, 8, bit(7))
        put(7, 8, bit(8))
        for i in range(9, 15):
            put(14 - i, 8, bit(i))
        for i in range(8):
            put(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            put(8, size - 15 + i, bit(i))
        put(8, size - 8, True)

    put_format(0)
    i, right = 0, size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                y = size - 1 - vert if (right + 1) & 2 == 0 else vert
                if not fixed[y][x] and i < len(stream) * 8:
                    dark[y][x] = (stream[i >> 3] >> (7 - (i & 7))) & 1 == 1
                    i += 1
        right -= 2

    masks = (lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
             lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
             lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
             lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0)

    def masked(m):
        put_format(m)
        return [[dark[y][x] ^ (not fixed[y][x] and masks[m](x, y)) for x in range(size)] for y in range(size)]

    def penalty(g):
        score = 0
        for line in g + [list(col) for col in zip(*g)]:
            run = 1
            for a, b in zip(line, line[1:] + [None]):
                if a == b:
                    run += 1
                else:
                    score += run - 2 if run >= 5 else 0
                    run = 1
        score += 3 * sum(g[y][x] == g[y][x + 1] == g[y + 1][x] == g[y + 1][x + 1]
                         for y in range(size - 1) for x in range(size - 1))
        share = sum(map(sum, g)) * 20 // (size * size)
        return score + 10 * abs(share - 10)

    best = min(range(8), key=lambda m: penalty(masked(m)))
    return masked(best)


def qr_svg(text):
    m = qr_matrix(text)
    n = len(m) + 8
    path = "".join(f"M{x + 4} {y + 4}h1v1h-1z" for y, row in enumerate(m) for x, on in enumerate(row) if on)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
            f'<rect width="{n}" height="{n}" fill="#fff"/><path d="{path}" fill="#000"/></svg>')


def qr_text(text):
    """The QR code of `text` for a terminal, two rows per line, dark on light whatever the terminal's colours."""
    m = qr_matrix(text)
    n = len(m)
    rows = [[False] * (n + 4)] * 2 + [[False] * 2 + r + [False] * 2 for r in m] + [[False] * (n + 4)] * 3
    chars = {(False, False): " ", (True, False): "\u2580", (False, True): "\u2584", (True, True): "\u2588"}
    return "\n".join("\x1b[30;107m" + "".join(chars[a, b] for a, b in zip(rows[y], rows[y + 1])) + "\x1b[0m"
                     for y in range(0, len(rows) - 1, 2))


# ---------------------------------------------------------------------------
# Local server for the browser board
# ---------------------------------------------------------------------------


def board_name(path):
    """The name a board goes by in the URL: its project folder's, made safe for a path."""
    folder = os.path.basename(os.path.dirname(os.path.abspath(path)))
    return re.sub(r"[^\w.-]+", "-", folder).strip("-.") or "board"


def same_file(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def page(title, body):
    return ("<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{escape(title)}</title><style>:root {{ color-scheme: light dark; font: 15px/1.5 system-ui, sans-serif; }}"
            " body { max-width: 640px; margin: 56px auto; padding: 0 20px; } h1 { font-size: 22px; }"
            " a.board { display: block; margin: 8px 0; padding: 10px 14px; border: 1px solid #8885; border-radius: 9px; color: inherit; text-decoration: none; }"
            " a.board:hover { border-color: #6366f1; } small { display: block; opacity: .6; } code { font-size: 13px; }</style>"
            f"<body>{body}</body></html>").encode("utf-8")


LOCAL_NAMES = frozenset({"localhost", "127.0.0.1", "[::1]"})
DEFAULT_PORT = 8765


def state_file():
    """Where Kanvas remembers the open boards between runs: the user's own application state folder."""
    base = os.environ.get("KANVAS_STATE_DIR")
    if not base:
        if os.name == "nt":
            base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local"), "Kanvas")
        elif sys.platform == "darwin":
            base = os.path.expanduser("~/Library/Application Support/Kanvas")
        else:
            base = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "kanvas")
    return os.path.join(base, "boards.json")


class BoardServer(ThreadingHTTPServer):
    """Serves every open board, each at /<name>/."""

    daemon_threads = True
    # On Windows SO_REUSEADDR lets two servers share a port.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, path=None, hosts=LOCAL_NAMES, quiet=False, share=None, remember=None):
        """`hosts` are the names allowed in the Host header, or None for any. A server made with
        `share` serves the boards of that one and cannot open boards itself. With `remember`, a
        file path, the open boards and whether phones may connect are kept there for the next start."""
        # TCPServer.__init__ calls server_close when the port is busy, before the rest is set up.
        self.phone_listener = None
        super().__init__(address, BoardHandler)
        self.hosts = hosts
        self.quiet = quiet
        self.remember = remember
        self.main = share or self
        self.opens_boards = share is None
        self.phone = []  # base URLs of the Tailscale listener, by name and by address
        self.phone_lock = threading.Lock()
        self.boards = share.boards if share else {}  # name -> path, in the order the boards were opened
        self.boards_lock = share.boards_lock if share else threading.Lock()
        if path:
            self.add_board(path)

    @property
    def check_host(self):
        return self.hosts is not None

    def open_to_phone(self):
        """Serve the boards on this computer's Tailscale address too, on the same port; return the
        base URLs a phone can open."""
        with self.phone_lock:
            if self.phone:
                return self.phone
            ip, name, magic = tailscale_self()
            names = tailscale_names(ip, name)
            port = self.server_address[1]
            try:
                listener = BoardServer((ip, port), hosts=frozenset(names), quiet=self.quiet, share=self)
            except OSError as e:
                raise KanvasError(f"Could not listen on the Tailscale address {ip}:{port}: {e}")
            threading.Thread(target=listener.serve_forever, daemon=True).start()
            self.phone_listener = listener
            # `tailscale serve` passes these names on to this computer's own address.
            if self.hosts is not None:
                self.hosts = self.hosts | names
            self.phone_names = names
            self.phone = [f"http://{h}:{port}/" for h in ([name] if magic else []) + [ip]]
        self.save_state()
        return self.phone

    def open_to_phone_soon(self, tries=20, wait=15):
        """Open to phones in the background, retrying while Tailscale is still starting, as after a login."""
        def run():
            for i in range(tries):
                try:
                    urls = self.open_to_phone()
                except KanvasError as e:
                    if i == tries - 1:
                        print(f"Could not open Kanvas to your phone: {e}", file=sys.stderr, flush=True)
                    time.sleep(wait)
                    continue
                if not self.quiet:
                    print(f"[{time.strftime('%H:%M:%S')}] opened to phones on Tailscale: {'  or  '.join(urls)}", flush=True)
                return
        threading.Thread(target=run, daemon=True).start()

    def close_to_phone(self):
        with self.phone_lock:
            self._stop_phone()
            if self.hosts is not None:
                self.hosts = self.hosts - getattr(self, "phone_names", set()) | LOCAL_NAMES
            self.phone = []
        self.save_state()

    def _stop_phone(self):
        if self.phone_listener:
            self.phone_listener.shutdown()
            self.phone_listener.server_close()
            self.phone_listener = None

    def server_close(self):
        self._stop_phone()
        super().server_close()

    def server_bind(self):
        # HTTPServer.server_bind looks up the host's full name with socket.getfqdn,
        # which can take half a minute on macOS. Nothing here uses that name.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def add_board(self, path, save=True):
        """Open the board at `path` and return its name. A board that is already open keeps its name."""
        path = os.path.abspath(path)
        with self.boards_lock:
            for name, known in self.boards.items():
                if same_file(known, path):
                    return name
            base = name = board_name(path)
            n = 2
            while name in self.boards or name == "api":
                name, n = f"{base}-{n}", n + 1
            self.boards[name] = path
        if save:
            self.save_state()
        return name

    def save_state(self):
        if not self.remember:
            return
        with self.boards_lock:
            state = {"boards": list(self.boards.values()), "phone": bool(self.phone)}
        try:
            os.makedirs(os.path.dirname(self.remember), exist_ok=True)
            write_text_atomic(self.remember, json.dumps(state, indent=2, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"Could not remember the open boards in {self.remember}: {e}", file=sys.stderr)

    def restore(self):
        """Open the boards remembered from the last run that are still boards; return whether phones could connect then."""
        try:
            state = json.loads(read_text_file(self.remember))
        except (OSError, ValueError):
            return False
        boards = state.get("boards") if isinstance(state, dict) else None
        for path in boards if isinstance(boards, list) else []:
            try:
                if isinstance(path, str) and "tasks" in json.loads(read_text_file(path)):
                    load_board(path)
                    self.add_board(path, save=False)
            except (KanvasError, OSError, ValueError):
                continue
        self.save_state()
        return isinstance(state, dict) and state.get("phone") is True

    def board_path(self, name):
        with self.boards_lock:
            return self.boards.get(name)

    def board_list(self):
        with self.boards_lock:
            boards = list(self.boards.items())
        out = []
        for name, path in boards:
            try:
                title = load_board(path)["title"] or name
            except (KanvasError, OSError):
                title = name
            out.append({"name": name, "title": title, "path": path})
        return out


class BoardHandler(BaseHTTPRequestHandler):
    server_version = "Kanvas/" + VERSION

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", headers=None):
        self._skip_body()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        headers = dict(headers or {})
        self.send_header("Cache-Control", headers.pop("Cache-Control", "no-store"))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _error(self, code, message):
        self._send(code, json.dumps({"error": message}).encode("utf-8"))

    def _skip_body(self):
        """Reads a request body that the answer leaves unread. Closing the connection with the body unread makes
        Windows reset it, and the client can lose the answer."""
        if getattr(self, "_body_read", False):
            return
        self._body_read = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if 0 < length <= MAX_UPLOAD:
            self.rfile.read(length)

    def _redirect(self, location):
        self._send(302, headers={"Location": location})

    def _host_name(self):
        host = (self.headers.get("Host") or "").strip().lower()
        name = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
        return name.rstrip(".")

    def _host_allowed(self):
        # Rejecting foreign Host headers blocks DNS-rebinding attacks from web pages.
        return not self.server.check_host or self._host_name() in self.server.hosts

    def _from_this_computer(self):
        """Whether the request comes from this computer by one of its local names, with no proxy between.
        Phones, other devices and `tailscale serve`, which passes requests on by this computer's Tailscale
        name and adds X-Forwarded headers, do not."""
        try:
            loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False
        forwarded = any(self.headers.get(h) for h in ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host"))
        return loopback and not forwarded and self._host_name() in LOCAL_NAMES

    def _may_manage(self):
        """Whether the request may open boards and open or close Kanvas to phones, which stay with
        the computer: only the main server does that, and only for this computer."""
        return self.server.opens_boards and self._from_this_computer()

    def _board_list(self):
        """The open boards, with where each one lives for this computer only."""
        boards = self.server.board_list()
        if self._from_this_computer():
            return boards
        return [{"name": b["name"], "title": b["title"]} for b in boards]

    def _route(self):
        """The request path as a list of parts: "/a/b/" gives ["a", "b", ""]."""
        return unquote(urlparse(self.path).path).split("/")[1:]

    def _read_json(self):
        """The request body, or None after answering with an error."""
        # Requiring JSON forces a CORS preflight, so other sites cannot write.
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._error(415, "Expected application/json")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            return self._error(400, "Expected the length of the body in bytes in Content-Length")
        if length > MAX_UPLOAD:
            return self._error(413, "Too large")
        self._body_read = True
        try:
            return self.rfile.read(length).decode("utf-8")
        except UnicodeDecodeError:
            return self._error(400, "Expected UTF-8")

    def do_GET(self):
        if not self._host_allowed():
            return self._error(403, "Host not allowed")
        parts = self._route()
        if parts in ([""], ["index.html"]):
            boards = self._board_list()
            if len(boards) == 1:
                return self._redirect(f"/{quote(boards[0]['name'])}/")
            links = "".join(f'<a class="board" href="/{quote(b["name"])}/">{escape(b["title"])}'
                            + (f'<small>{escape(b["path"])}</small>' if "path" in b else "") + "</a>" for b in boards)
            body = (f"<h1>Kanvas</h1>{links}" if boards else "<h1>Kanvas</h1><p>No boards are open. Run <code>kanvas serve</code> in a project to open its board here.</p>")
            if boards:
                body += "<p><small>Run <code>kanvas serve</code> in a project to open its board here.</small></p>"
            return self._send(200, page("Kanvas", body), "text/html; charset=utf-8")
        if parts == ["api", "boards"]:
            info = {"kanvas": VERSION, "boards": self._board_list()}
            return self._send(200, json.dumps(info, ensure_ascii=False).encode("utf-8"))
        name, rest = parts[0], parts[1:]
        path = self.server.board_path(name)
        if path is None:
            if rest[:1] == ["api"]:
                return self._error(404, f"No board called {name} is open here; run `kanvas serve` in its project")
            body = (f"<h1>No board called {escape(name)} is open</h1><p>Run <code>kanvas serve</code> in its project, then reload this page."
                    ' If it was open here before, its project folder may have been moved or renamed.</p><p><a href="/">Open boards</a></p>')
            return self._send(404, page("Kanvas", body), "text/html; charset=utf-8")
        if not rest:
            return self._redirect(f"/{quote(name)}/")
        if rest == ["api", "phone"]:
            return self._phone(name)
        if rest in ([""], ["index.html"]):
            try:
                with open(HTML_FILE, "rb") as f:
                    body = f.read()
            except OSError:
                return self._error(500, "kanvas.html is missing next to kanvas.py")
            return self._send(200, body, "text/html; charset=utf-8")
        if rest == ["api", "board"]:
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                return self._error(404, "Board file is missing")
            tag = etag_of(data)
            if self.headers.get("If-None-Match") == tag:
                return self._send(304, headers={"ETag": tag})
            return self._send(200, data, headers={"ETag": tag})
        if len(rest) == 2 and rest[0] == FILES_DIR:
            return self._image(path, f"{FILES_DIR}/{rest[1]}")
        return self._error(404, "Not found")

    def _image(self, board, ref):
        """An image of the board. Its name is a hash of its bytes, so it never changes and browsers keep it."""
        target = image_path(board, ref)
        try:
            with open(target, "rb") as f:
                data = f.read()
        except (OSError, TypeError):
            return self._error(404, "Not found")
        self._send(200, data, IMAGE_TYPES[ref.rsplit(".", 1)[1]], headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        })

    def _upload_image(self, board):
        """Keeps an image sent as the raw request body. An image type is not one a form can send, so the browser asks
        before another site sends one, and Kanvas does not allow it."""
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() not in IMAGE_TYPES.values():
            return self._error(415, "Expected a PNG, JPEG, GIF or WebP image")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0:
            return self._error(400, "Expected the length of the body in bytes in Content-Length")
        if length > MAX_IMAGE:
            return self._error(413, f"Images can be {MAX_IMAGE // (1024 * 1024)} MB at most")
        self._body_read = True
        data = self.rfile.read(length)
        try:
            ref = store_image(board, data)
        except KanvasError as e:
            return self._error(400, str(e))
        except OSError as e:
            return self._error(500, f"Cannot save the image: {e}")
        self._send(200, json.dumps({"path": ref}).encode("utf-8"))

    def _phone(self, name):
        """The board's addresses for phones on Tailscale, with a QR code of the first; none while closed to phones.
        `manage` says whether this request may open or close Kanvas to phones."""
        urls = [f"{base}{quote(name)}/" for base in self.server.main.phone]
        info = {"urls": urls, "qr": qr_svg(urls[0]) if urls else None, "manage": self._may_manage()}
        self._send(200, json.dumps(info).encode("utf-8"))

    def do_POST(self):
        """Opens another project's board here, for `kanvas serve` in that project, or opens the boards to phones."""
        if not self._host_allowed():
            return self._error(403, "Host not allowed")
        parts = self._route()
        if parts[1:] == ["api", "files"] and self.server.board_path(parts[0]) is not None:
            return self._upload_image(self.server.board_path(parts[0]))
        if parts[1:] == ["api", "phone"] and self.server.board_path(parts[0]) is not None:
            if not self._may_manage():
                return self._error(403, "Only the computer running Kanvas can open it to phones")
            if self._read_json() is None:
                return
            try:
                fresh = not self.server.phone
                urls = self.server.open_to_phone()
            except KanvasError as e:
                return self._error(503, str(e))
            if fresh and not self.server.quiet:
                print(f"[{time.strftime('%H:%M:%S')}] opened to phones on Tailscale: {'  or  '.join(urls)}", flush=True)
            return self._phone(parts[0])
        if parts != ["api", "boards"]:
            return self._error(404, "Not found")
        if not self._may_manage():
            return self._error(403, "Boards open only from the computer running Kanvas")
        text = self._read_json()
        if text is None:
            return
        try:
            path = json.loads(text).get("path")
            if not isinstance(path, str) or not os.path.isabs(path):
                raise KanvasError("Expected the board's absolute path")
            # A board always has its task list, so another JSON file is not taken for one.
            if "tasks" not in json.loads(read_text_file(path)):
                raise KanvasError(f"{path} is not a Kanvas board")
            load_board(path)
        except (KanvasError, ValueError, AttributeError, OSError) as e:
            return self._error(400, str(e))
        name = self.server.add_board(path)
        if not self.server.quiet:
            print(f"[{time.strftime('%H:%M:%S')}] opened {name}: {path}", flush=True)
        info = {"name": name, "url": f"/{quote(name)}/"}
        if self.server.phone:
            info["phone"] = [f"{base}{quote(name)}/" for base in self.server.phone]
        self._send(200, json.dumps(info, ensure_ascii=False).encode("utf-8"))

    def do_DELETE(self):
        """Closes Kanvas to phones again."""
        if not self._host_allowed():
            return self._error(403, "Host not allowed")
        parts = self._route()
        if parts[1:] != ["api", "phone"] or self.server.board_path(parts[0]) is None:
            return self._error(404, "Not found")
        if not self._may_manage():
            return self._error(403, "Only the computer running Kanvas can close it to phones")
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._error(415, "Expected application/json")
        if self.server.phone:
            self.server.close_to_phone()
            if not self.server.quiet:
                print(f"[{time.strftime('%H:%M:%S')}] closed to phones", flush=True)
        self._phone(parts[0])

    def do_PUT(self):
        if not self._host_allowed():
            return self._error(403, "Host not allowed")
        parts = self._route()
        path = self.server.board_path(parts[0]) if parts[1:] == ["api", "board"] else None
        if path is None:
            return self._error(404, "Not found")
        text = self._read_json()
        if text is None:
            return
        expected = self.headers.get("If-Match")
        if not expected:
            return self._error(428, "Expected If-Match with the ETag of the board this changes; load the board first")
        try:
            board = parse_board(text)
            # parse_board fills in a missing task list, which would empty the board.
            if "tasks" not in json.loads(text):
                raise KanvasError("Expected a board with its task list")
        except KanvasError as e:
            return self._error(400, str(e))
        errors = structural_errors(board)
        if errors:
            return self._error(400, "; ".join(errors))
        data = dumps(board).encode("utf-8")
        try:
            with locked(path):
                with open(path, "rb") as f:
                    current = f.read()
                current_tag = etag_of(current)
                if expected != current_tag:
                    return self._send(409, current, headers={"ETag": current_tag})
                if data != current:
                    write_text_atomic(path, data.decode("utf-8"))
                    if not self.server.quiet:
                        print(f"[{time.strftime('%H:%M:%S')}] saved {parts[0]} from the browser", flush=True)
        except (KanvasError, OSError) as e:
            # The reason names the board's folder or its lock file.
            return self._error(503, str(e) if self._from_this_computer() else "The computer running Kanvas could not save the board")
        self._send(200, data, headers={"ETag": etag_of(data)})


def start_server(path, host="127.0.0.1", port=DEFAULT_PORT, quiet=False, remember=None):
    """Serve the board at `path` (or none) on the first free port from `port` on. With `remember`,
    the boards open last time come back first, and `phone_wanted` says whether phones could connect."""
    hosts = LOCAL_NAMES if host in ("127.0.0.1", "localhost") else None
    last_error = None
    for p in range(port, port + 20):
        try:
            server = BoardServer((host, p), None, hosts, quiet, remember=remember)
            server.phone_wanted = server.restore() if remember else False
            if path:
                server.add_board(path)
            return server
        except OSError as e:
            busy = (errno.EADDRINUSE, errno.EACCES, getattr(errno, "WSAEADDRINUSE", -1), getattr(errno, "WSAEACCES", -1))
            if e.errno not in busy:
                raise
            last_error = e
    raise KanvasError(f"No free port between {port} and {port + 19}: {last_error}")


# Proxy settings must not send requests for this computer elsewhere.
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def port_free(host, port):
    with socket.socket() as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def find_running(port, host="127.0.0.1"):
    """Return (url, version) of a Kanvas already serving boards on one of the ports `serve` tries, or None."""
    for p in range(port, port + 20):
        # A Kanvas may hold a later port after the ones below it were freed. Only taken ports are
        # probed, since connecting to a closed port takes seconds on Windows.
        if port_free(host, p):
            continue
        try:
            with LOCAL.open(f"http://{host}:{p}/api/boards", timeout=1) as r:
                info = json.loads(r.read().decode("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(info, dict) and isinstance(info.get("kanvas"), str):
            return f"http://{host}:{p}/", info["kanvas"]
    return None


def join_running(url, path):
    """Open the board at `path` in the Kanvas running at `url`; return the board's URL there,
    and its URLs for phones on Tailscale (empty without Tailscale)."""
    body = json.dumps({"path": os.path.abspath(path)}).encode("utf-8")
    req = urllib.request.Request(url + "api/boards", body, {"Content-Type": "application/json"}, method="POST")
    try:
        with LOCAL.open(req, timeout=5) as r:
            info = json.loads(r.read().decode("utf-8"))
            return url + info["url"].lstrip("/"), info.get("phone") or []
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read().decode("utf-8"))["error"]
        except (ValueError, KeyError, OSError):
            message = str(e)
        raise KanvasError(f"The Kanvas running at {url} did not open the board: {message}")
    except (OSError, ValueError, KeyError) as e:
        raise KanvasError(f"The Kanvas running at {url} did not answer: {e}")


TAILSCALE_PATHS = (
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Tailscale", "tailscale.exe"),
)


def tailscale_self():
    """This computer on Tailscale: (IPv4 address, DNS name or "", whether MagicDNS gives names)."""
    exe = shutil.which("tailscale") or next((p for p in TAILSCALE_PATHS if os.path.isfile(p)), None)
    if not exe:
        raise KanvasError("Tailscale is not installed here. Get it from https://tailscale.com/download, "
                          "sign in on this computer and on your phone, then run this again.")
    try:
        proc = subprocess.run([exe, "status", "--json"], capture_output=True, timeout=15)
        status = json.loads(proc.stdout.decode("utf-8", "replace"))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise KanvasError("Tailscale did not say this computer's address. Is it running? Try `tailscale status`.")
    if status.get("BackendState") != "Running":
        raise KanvasError(f"Tailscale is not connected ({status.get('BackendState') or 'unknown state'}). "
                          "Connect it (`tailscale up`, or the Tailscale app), then run this again.")
    me = status.get("Self") or {}
    ips = [ip for ip in me.get("TailscaleIPs") or [] if "." in ip]
    if not ips:
        raise KanvasError("Tailscale gave this computer no IPv4 address; check `tailscale status`.")
    name = (me.get("DNSName") or "").rstrip(".").lower()
    magic = bool((status.get("CurrentTailnet") or {}).get("MagicDNSEnabled")) and bool(name)
    return ips[0], name, magic


def tailscale_names(ip, name):
    """What a phone may type to reach this computer: its address, full name and short name."""
    return {n for n in (ip, name, name.split(".")[0]) if n}


def phone_line(urls):
    return f"On your phone (Tailscale): {urls[0]}" + (f"  or  {urls[1]}" if len(urls) > 1 else "")


def print_phone(urls):
    print(phone_line(urls))
    if sys.stdout.isatty():
        print("Scan it with your phone's camera:")
        print(qr_text(urls[0]))
    print("Anyone on your Tailscale network can open and edit your boards.")


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
    if args.saved:
        path = None
    else:
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
    local = args.host in ("127.0.0.1", "localhost")
    if args.tailscale and not local:
        raise KanvasError("Use --tailscale or --host, not both: --tailscale serves on this computer and on its Tailscale address.")
    # One Kanvas serves the boards of every project; a second `serve` hands its board to it and returns.
    running = find_running(args.port, args.host) if args.port and local else None
    if running and not path:
        print(f"Kanvas already runs at {running[0]}")
        return
    if running:
        url, version = running
        board_url, phone_urls = join_running(url, path)
        print(f"Kanvas board: {path}")
        if hint := agent_copy_hint(path):
            print(hint)
        print(f"Opened in the Kanvas already running: {board_url}")
        if phone_urls:
            print_phone(phone_urls)
        elif args.tailscale:
            print("That Kanvas is not open to your phone. Stop it (Ctrl+C in its terminal) and run `kanvas serve --tailscale` there.")
        if version != VERSION:
            print(f"That Kanvas is {version} and this command is {VERSION}; to switch, stop it (Ctrl+C in its terminal) and run `kanvas serve` again.")
        if not args.no_open:
            webbrowser.open(board_url)
        return
    server = start_server(path, args.host, args.port, remember=state_file() if local else None)
    if args.tailscale:
        try:
            server.open_to_phone()
        except KanvasError:
            server.server_close()
            raise
    elif server.phone_wanted:
        server.open_to_phone_soon()
    host, port = server.server_address[:2]
    base_url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/"
    if path:
        name = quote(server.add_board(path))
        board_url = f"{base_url}{name}/"
        print(f"Kanvas board: {path}")
        if hint := agent_copy_hint(path):
            print(hint)
    else:
        name, board_url = "", base_url
    others = [b["name"] for b in server.board_list() if b["name"] != unquote(name)]
    print(f"Open {board_url}  (Ctrl+C to stop)")
    if others:
        print(f"Also open from last time: {', '.join(others)}")
    if server.phone:
        print_phone([f"{base}{name}/" if name else base for base in server.phone])
    print("Boards of other projects open here too: run `kanvas serve` in them.")
    if not server.check_host:
        print("Warning: listening beyond localhost; anyone who can reach this port can edit the board.")
    if not args.no_open:
        threading.Timer(0.4, webbrowser.open, (board_url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


# ---------------------------------------------------------------------------
# Background service: Kanvas starts when you log in
# ---------------------------------------------------------------------------

SERVICE_NAME = "kanvas"  # systemd unit, launchd label and Windows task


def service_command():
    """The command a login service runs: this Kanvas, serving the boards open last time."""
    if INSTALLED and f"{os.sep}archive-v" in sys.prefix:
        raise KanvasError("This kanvas runs from uvx's temporary cache, which a service cannot rely on. "
                          "Install it with `uv tool install kanvas-board`, then run `kanvas service install`.")
    args = ["serve", "--saved", "--no-open"]
    if os.name == "nt":
        windowless = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        return [windowless if os.path.isfile(windowless) else sys.executable, os.path.abspath(__file__)] + args
    installed = shutil.which("kanvas") if INSTALLED else None
    return ([installed] if installed else [sys.executable, os.path.abspath(__file__)]) + args


def run_quiet(cmd):
    """Run a system command; return (exit code, its output)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return proc.returncode, (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()


def systemd_unit(cmd):
    quoted = " ".join('"' + a.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"' for a in cmd)
    return ("[Unit]\nDescription=Kanvas task boards\n\n"
            f"[Service]\nExecStart={quoted}\nEnvironment=PYTHONUNBUFFERED=1\nRestart=on-failure\nRestartSec=10\n\n"
            "[Install]\nWantedBy=default.target\n")


def launchd_plist(cmd):
    import plistlib
    log = os.path.expanduser("~/Library/Logs/Kanvas.log")
    return plistlib.dumps({"Label": SERVICE_NAME, "ProgramArguments": cmd, "RunAtLoad": True,
                           "KeepAlive": {"SuccessfulExit": False}, "StandardOutPath": log, "StandardErrorPath": log})


def windows_task(cmd):
    from xml.sax.saxutils import escape as xml
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    args = subprocess.list2cmdline(cmd[1:])
    return ('<?xml version="1.0" encoding="UTF-16"?>\n'
            '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
            "<RegistrationInfo><Description>Kanvas task boards</Description></RegistrationInfo>"
            f"<Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{xml(user)}</UserId></LogonTrigger></Triggers>"
            '<Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>'
            "<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
            "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT0S</ExecutionTimeLimit>"
            "<RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure></Settings>"
            f'<Actions Context="Author"><Exec><Command>{xml(cmd[0])}</Command><Arguments>{xml(args)}</Arguments></Exec></Actions></Task>')


def service_paths():
    if os.name == "nt":
        return None
    if sys.platform == "darwin":
        return os.path.expanduser(f"~/Library/LaunchAgents/{SERVICE_NAME}.plist")
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(config, "systemd", "user", f"{SERVICE_NAME}.service")


def service_install(tailscale, wait=10):
    cmd = service_command()
    if tailscale:
        # The service opens to phones once Tailscale is up, as it would after opening from the board.
        state = state_file()
        try:
            current = json.loads(read_text_file(state))
        except (OSError, ValueError):
            current = {}
        current = current if isinstance(current, dict) else {}
        os.makedirs(os.path.dirname(state), exist_ok=True)
        write_text_atomic(state, json.dumps(dict(current, phone=True), indent=2, ensure_ascii=False) + "\n")
    path = service_paths()
    if os.name == "nt":
        xml_path = os.path.join(os.path.dirname(state_file()), "kanvas-task.xml")
        os.makedirs(os.path.dirname(xml_path), exist_ok=True)
        with open(xml_path, "w", encoding="utf-16") as f:
            f.write(windows_task(cmd))
        stop = ["schtasks", "/End", "/TN", SERVICE_NAME]
        steps = [["schtasks", "/Create", "/TN", SERVICE_NAME, "/XML", xml_path, "/F"], ["schtasks", "/Run", "/TN", SERVICE_NAME]]
        where = "a Task Scheduler task that runs when you log in"
    elif sys.platform == "darwin":
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(launchd_plist(cmd))
        domain = f"gui/{os.getuid()}"
        stop = ["launchctl", "bootout", f"{domain}/{SERVICE_NAME}"]
        steps = [["launchctl", "bootstrap", domain, path]]
        where = path
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_text_atomic(path, systemd_unit(cmd))
        stop = ["systemctl", "--user", "stop", f"{SERVICE_NAME}.service"]
        steps = [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", f"{SERVICE_NAME}.service"]]
        where = path
    # Once the service is stopped, a Kanvas that still answers was started with `kanvas serve`.
    # The service's own `serve --saved` then finds it and exits, and nothing restarts a clean exit.
    run_quiet(stop)
    by_hand = find_running(DEFAULT_PORT)
    for step in steps:
        code, out = run_quiet(step)
        if code:
            raise KanvasError(f"`{' '.join(step)}` failed: {out or f'exit code {code}'}")
    print(f"Kanvas now starts when you log in ({where}).")
    print("It serves the boards you open with `kanvas serve`, and remembers them across restarts.")
    if tailscale:
        print("It also opens them to your phone over Tailscale, as soon as Tailscale is connected.")
    if by_hand:
        again = "kanvas service install" + (" --tailscale" if tailscale else "")
        print(f"Kanvas already runs at {by_hand[0]}, started with `kanvas serve`; the service leaves your boards to it and stops.")
        print("Once that Kanvas stops, nothing serves them until you log in again. To hand them to the service now, "
              f"stop it (Ctrl+C in its terminal) and run `{again}` again.")
    else:
        deadline = time.monotonic() + wait
        running = find_running(DEFAULT_PORT)
        while not running and time.monotonic() < deadline:
            time.sleep(0.25)
            running = find_running(DEFAULT_PORT)
        print(f"Open {running[0]}" if running else "It has not answered yet; `kanvas service status` says when it runs.")
    if os.name != "nt" and sys.platform != "darwin":
        print(f"To start it at boot, before you log in: loginctl enable-linger {os.environ.get('USER') or '$USER'}")


def service_uninstall():
    path = service_paths()
    if os.name == "nt":
        run_quiet(["schtasks", "/End", "/TN", SERVICE_NAME])
        code, out = run_quiet(["schtasks", "/Delete", "/TN", SERVICE_NAME, "/F"])
        removed = code == 0
    elif sys.platform == "darwin":
        run_quiet(["launchctl", "bootout", f"gui/{os.getuid()}/{SERVICE_NAME}"])
        removed = os.path.exists(path)
        if removed:
            os.remove(path)
    else:
        run_quiet(["systemctl", "--user", "disable", "--now", f"{SERVICE_NAME}.service"])
        removed = os.path.exists(path)
        if removed:
            os.remove(path)
        run_quiet(["systemctl", "--user", "daemon-reload"])
    print("Kanvas no longer starts when you log in." if removed else "Kanvas was not set to start when you log in.")


def service_status():
    path = service_paths()
    if os.name == "nt":
        installed = run_quiet(["schtasks", "/Query", "/TN", SERVICE_NAME])[0] == 0
    else:
        installed = os.path.exists(path)
    print("Starts when you log in: " + ("yes" if installed else "no (`kanvas service install` sets that up)"))
    running = find_running(DEFAULT_PORT)
    print(f"Running: yes, at {running[0]}" if running else "Running: no")
    try:
        state = json.loads(read_text_file(state_file()))
    except (OSError, ValueError):
        state = {}
    boards = state.get("boards") or [] if isinstance(state, dict) else []
    print(f"Remembered boards ({state_file()}):" if boards else "Remembered boards: none yet")
    for b in boards:
        print(f"  {b}")


def cmd_service(args):
    {"install": lambda: service_install(args.tailscale), "uninstall": service_uninstall, "status": service_status}[args.action]()


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

EPILOG = """\
workflow:  proposed -> todo -> doing -> review -> done
  Agents propose, start, pause, finish, note and add dependencies, and may
  edit, withdraw or rewire proposals until a human approves them.
  Humans approve proposals and mark work done in the board (`serve`).
  A todo task with unfinished dependencies shows as blocked.
  An agent that needs the human's reply in the conversation runs `ask`
  first, so the board shows the task waiting for them until the next note.
  YOLO on a task: turning it on approves it and everything it depends on;
  finishing them marks them done, and the YOLO task itself goes to review.
  A proposal linked into the chain later still needs a human's approval.
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
                        help="name recorded on notes and proposals (default: $KANVAS_AGENT or 'agent')")

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
    text_sources(p, "note")
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
    p = add("attach", cmd_attach, "attach images (PNG, JPEG, GIF, WebP) to a task in a note, e.g. screenshots")
    p.add_argument("id")
    p.add_argument("images", nargs="+", metavar="IMAGE")
    p.add_argument("-m", "--message", help="optional text for the note, above the images")
    text_sources(p, "note text")
    p = add("ask", cmd_ask, "show the human that you wait for their reply to a question in the conversation; "
                            "the next note ends the wait")
    p.add_argument("id")
    p.add_argument("text", nargs="?", help="the question, as you ask it in the conversation")
    text_sources(p, "question")
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
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"port to serve on (default: %(default)s), or the next free one up to 19 higher ({DEFAULT_PORT + 19} by default); "
                        "when a Kanvas already runs on any of these ports, serve opens the board there and returns")
    p.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 for containers or remote machines")
    p.add_argument("--tailscale", action="store_true", help="also open the boards to your phone and other devices on your Tailscale network")
    p.add_argument("--saved", action="store_true", help="serve only the boards open last time, without a project of its own (for `kanvas service`)")
    p.add_argument("--no-open", action="store_true", help="do not open a browser")
    p = add("service", cmd_service, "start Kanvas when you log in, so your boards are always open")
    p.add_argument("action", choices=["install", "uninstall", "status"])
    p.add_argument("--tailscale", action="store_true", help="also open the boards to your phone over Tailscale")
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
