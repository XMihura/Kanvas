"""Tests for kanvas.py. Run with: python -m unittest discover -s tests -v"""

import argparse
import atexit
import base64
import contextlib
import http.client
import io
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import kanvas  # noqa: E402

# The CLI processes the tests start inherit the environment; they must never reach a real board.
os.environ.pop("KANVAS_BOARD", None)
# Nor the boards a real Kanvas remembers between runs.
STATE_DIR = tempfile.mkdtemp(prefix="kanvas-state-")
os.environ["KANVAS_STATE_DIR"] = STATE_DIR
atexit.register(shutil.rmtree, STATE_DIR, True)

SCRIPT = os.path.join(ROOT, "kanvas.py")


def write_board(path, board):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(kanvas.dumps(board))


def task(tid, state="todo", group=None, deps=(), **extra):
    t = {"id": tid, "title": f"Task {tid}", "state": state, "group": group, "deps": list(deps)}
    t.update(extra)
    return t


# Hand-edited tasks with a field of the wrong type, and the error each one gives.
WRONG_TYPES = [
    (task("G-01", ["todo"]), "Task G-01: 'state' must be a string"),
    (task("G-01", group=["A"]), "Task G-01: 'group' must be a string or null"),
    (task("G-01", notes=[{"at": "x", "by": "h", "text": 5}]), "Task G-01: note 'text' must be a string"),
]


def run_html_js(test, code, stdin, sections=()):
    """Runs `code` after the pure part of kanvas.html under Node, with `stdin` parsed as JSON in `input`.

    `sections` adds more of the page, each as a (start, end) pair of markers.
    """
    node = shutil.which("node")
    if not node:
        test.skipTest("node is not installed")
    with open(os.path.join(ROOT, "kanvas.html"), encoding="utf-8") as f:
        html = f.read()
    parts = [html[html.index("const STATES ="):html.index("// App state")]]
    for start, end in sections:
        i = html.index(start)
        parts.append(html[i:html.index(end, i)])
    js = ("\n".join(parts)
          + "\nconst input = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n" + code + "\n")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(js)
    test.addCleanup(os.remove, f.name)
    proc = subprocess.run([node, f.name], input=stdin.encode("utf-8"), capture_output=True, timeout=120)
    test.assertEqual(proc.returncode, 0, proc.stderr.decode())
    return proc.stdout.decode("utf-8")


class TempBoard(unittest.TestCase):
    """A temporary directory with a kanvas.json, and a helper to run the CLI in it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kanvas-test-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "kanvas.json")
        write_board(self.path, self.initial_board())

    def initial_board(self):
        return {
            "kanvas": 1,
            "title": "Test",
            "groups": [{"id": "DV", "name": "Development"}],
            "tasks": [
                task("DV-01", "done", "DV"),
                task("DV-02", "todo", "DV", ["DV-01"]),
                task("DV-03", "todo", "DV", ["DV-02"]),
                task("DV-04", "proposed", "DV"),
            ],
        }

    def run_cli(self, *args, stdin=None, cwd=None, env=None, check=True):
        full_env = dict(os.environ, KANVAS_AGENT="")
        full_env.pop("KANVAS_BOARD", None)
        full_env.update(env or {})
        proc = subprocess.run(
            [sys.executable, SCRIPT, *args],
            cwd=cwd or self.dir,
            input=stdin if stdin is not None else b"",
            capture_output=True,
            env=full_env,
        )
        out, err = proc.stdout.decode("utf-8"), proc.stderr.decode("utf-8")
        if check and proc.returncode != 0:
            self.fail(f"kanvas {' '.join(args)} failed ({proc.returncode}):\n{out}{err}")
        return proc.returncode, out, err

    def fails(self, *args, **kw):
        code, out, err = self.run_cli(*args, check=False, **kw)
        self.assertNotEqual(code, 0, f"expected kanvas {' '.join(args)} to fail:\n{out}")
        return err

    def board(self):
        return kanvas.load_board(self.path)

    def get(self, tid):
        return kanvas.find_task(self.board(), tid)

    def raw(self):
        with open(self.path, "rb") as f:
            return f.read()


class FormatTests(unittest.TestCase):
    def sample(self):
        with open(os.path.join(ROOT, "examples", "sample.json"), encoding="utf-8") as f:
            return f.read()

    def test_dumps_is_stable(self):
        text = self.sample()
        self.assertEqual(kanvas.dumps(kanvas.parse_board(text)), text)

    def test_the_example_boards_are_valid_and_canonical(self):
        for name in sorted(os.listdir(os.path.join(ROOT, "examples"))):
            with self.subTest(name), open(os.path.join(ROOT, "examples", name), encoding="utf-8") as f:
                text = f.read()
                board = kanvas.parse_board(text)
                self.assertEqual(kanvas.dumps(board), text)
                self.assertEqual(kanvas.problems(board), ([], []))

    def test_short_string_arrays_stay_on_one_line(self):
        board = kanvas.new_board("t")
        board["tasks"] = [task("A-01"), task("A-02", deps=["A-01"]), task("A-03", deps=["A-01", "A-02"])]
        text = kanvas.dumps(board)
        self.assertIn('"deps": ["A-01", "A-02"]', text)
        self.assertEqual(json.loads(text)["tasks"][2]["deps"], ["A-01", "A-02"])

    def test_empty_optional_fields_are_omitted_and_unknown_keys_kept(self):
        board = kanvas.parse_board(json.dumps({
            "kanvas": 1, "groups": [], "custom": {"keep": True},
            "tasks": [{"id": "G-01", "title": "x", "state": "todo", "estimate": 3}],
        }))
        out = json.loads(kanvas.dumps(board))
        self.assertEqual(out["tasks"][0], {"id": "G-01", "title": "x", "state": "todo", "estimate": 3})
        self.assertEqual(out["custom"], {"keep": True})
        self.assertEqual(list(out)[:4], ["kanvas", "title", "groups", "tasks"])

    def test_newer_format_is_rejected(self):
        with self.assertRaises(kanvas.KanvasError):
            kanvas.parse_board('{"kanvas": 99, "groups": [], "tasks": []}')

    def test_fields_of_the_wrong_type_are_rejected(self):
        for bad, message in WRONG_TYPES:
            with self.subTest(message=message):
                with self.assertRaises(kanvas.KanvasError) as cm:
                    kanvas.parse_board(json.dumps(dict(kanvas.new_board(), tasks=[bad])))
                self.assertEqual(str(cm.exception), message)

    def assert_writers_agree(self, text):
        out = run_html_js(self, "process.stdout.write(serialize(normalize(input)));", text)
        self.assertEqual(out, kanvas.dumps(kanvas.parse_board(text)))

    def test_html_serializer_matches_python(self):
        """kanvas.html must write byte-identical files, or every browser save would reformat the board."""
        board = kanvas.parse_board(self.sample())
        board["tasks"][0]["desc"] = 'Quotes " and \\ backslash, tab\t, unicode é 日本 ✓, </script>'
        board["tasks"][1]["deps"] = []
        board["tasks"][1]["extra"] = {"nested": [1, 2.5, None, True]}
        board["tasks"][2]["yolo"] = True
        board["tasks"][3]["yolo"] = False
        board["tasks"][3]["notes"] = [
            {"text": "Edited", "edited": "2026-10-21T10:16:00Z", "by": "human", "at": "2026-10-19T08:00:00Z"},
            {"at": "2026-10-19T09:00:00Z", "by": "codex", "text": "Not edited", "edited": ""},
        ]
        board["unknown"] = ["kept"]
        self.assert_writers_agree(json.dumps(board, ensure_ascii=False))

    def test_writers_agree_on_unusual_values(self):
        """Numbers, numeric keys, keys that are special in JavaScript, missing values and lone surrogates."""
        self.assert_writers_agree(json.dumps({
            "kanvas": 1, "title": 5, "10": "ten", "2": "two", "b": True, "01": "kept in place",
            "constructor": {"__proto__": 1, "toString": None},
            "groups": [{"id": "G", "name": ""}],
            "tasks": [
                {"id": "G-01", "title": None, "state": "", "group": "", "desc": 0, "deps": [], "yolo": 1,
                 "7": "seven", "constructor": "kept", "__proto__": {"x": 1},
                 "numbers": [1.0, -0.0, 1e21, 1e-7, 0.1, 2 ** 53 + 1, 10 ** 30, 123.456, 5e-324]},
                {"id": "G-02", "title": "Lone \ud800 surrogate", "state": None, "deps": ["G-01"], "yolo": 0,
                 "notes": [{"at": "2026-10-21T09:00:00Z", "by": "codex", "text": "x", "4": 4}]},
            ],
        }))


class ModelTests(unittest.TestCase):
    def test_suggest_prefix(self):
        self.assertEqual(kanvas.suggest_prefix("Research", []), "RS")
        self.assertEqual(kanvas.suggest_prefix("Development", []), "DV")
        self.assertEqual(kanvas.suggest_prefix("Delivery", ["DV"]), "DL")
        self.assertEqual(kanvas.suggest_prefix("Report Writing", []), "RW")
        self.assertEqual(kanvas.suggest_prefix("Level 0 Contract", []), "L0C")

    def test_next_id_goes_past_99(self):
        board = kanvas.new_board()
        board["tasks"] = [task("DV-99")]
        self.assertEqual(kanvas.next_id(board, "DV"), "DV-100")
        board["tasks"].append(task("DV-100"))
        self.assertEqual(kanvas.next_id(board, "dv"), "DV-101")

    def test_html_next_id_matches_python(self):
        board = kanvas.new_board()
        board["tasks"] = [task(tid) for tid in ("DV-007", "dv-12", "DV-99", "DVX-150", "D-170", "DV-X-140", "DV-", "DV-1a",
                                                "-5", "DV-²", "DV-٣", "L0C-03", "g-04", "QA-1")]
        prefixes = ["DV", "dv", "DVX", "D", "L0C", "G", "QA", "NEW"]
        out = run_html_js(self, f"const b = normalize(input); process.stdout.write(JSON.stringify({json.dumps(prefixes)}.map((p) => nextId(b, p))));",
                          json.dumps(board))
        self.assertEqual(json.loads(out), [kanvas.next_id(board, p) for p in prefixes])
        self.assertEqual(json.loads(out), ["DV-100", "DV-100", "DVX-151", "D-171", "L0C-04", "G-05", "QA-02", "NEW-01"])

    def test_problems(self):
        board = kanvas.new_board()
        board["tasks"] = [
            task("A-01", deps=["A-02"]), task("A-02", deps=["A-01"]),
            task("A-03", deps=["A-09"]), task("A-04", "doing", deps=["A-03"]), task("bad id"),
        ]
        errors, warnings = kanvas.problems(board)
        self.assertTrue(any("Circular dependency" in e for e in errors), errors)
        self.assertTrue(any("missing task A-09" in e for e in errors), errors)
        self.assertTrue(any("Invalid task id" in e for e in errors), errors)
        self.assertTrue(any("A-04 is doing but depends on unfinished A-03" in w for w in warnings), warnings)


class UndoTests(unittest.TestCase):
    """Undo and redo replay a change on the latest board, which agents may have changed meanwhile."""

    NOTE = {"at": "2026-10-21T09:00:00Z", "by": "human", "text": "Same"}
    AGENT = {"at": "2026-10-21T09:05:00Z", "by": "codex", "text": "Agent note"}

    def board(self, *tasks):
        return {"kanvas": 1, "title": "", "groups": [], "tasks": [task("A-01")] + list(tasks)}

    def notes(self, *notes):
        return self.board(task("A-02", notes=list(notes)))

    def replay(self, cur, frm, to):
        code = ("const [cur, from, to] = input.map(normalize);\n"
                "const result = replay(cur, from, to);\n"
                "process.stdout.write(JSON.stringify({ ...result, board: cur }));")
        out = json.loads(run_html_js(self, code, json.dumps([cur, frm, to])))
        return out["board"], out["applied"], out["skipped"]

    def test_undoing_a_note_edit_restores_the_note_whatever_its_key_order(self):
        edited = dict(self.NOTE, text="Changed", edited="2026-10-21T09:10:00Z")
        read_back = {k: edited[k] for k in ("edited", "text", "by", "at")}
        board, _, _ = self.replay(self.notes(read_back, self.AGENT), self.notes(edited), self.notes(self.NOTE))
        self.assertEqual(board["tasks"][1]["notes"], [self.NOTE, self.AGENT])

    def test_identical_notes_are_counted(self):
        board, _, _ = self.replay(self.notes(self.NOTE, self.AGENT), self.notes(self.NOTE), self.notes(self.NOTE, self.NOTE))
        self.assertEqual(board["tasks"][1]["notes"], [self.NOTE, self.NOTE, self.AGENT])
        board, _, _ = self.replay(board, self.notes(self.NOTE, self.NOTE), self.notes(self.NOTE))
        self.assertEqual(board["tasks"][1]["notes"], [self.NOTE, self.AGENT])

    def test_deleting_a_group_and_undoing_it(self):
        board = {"kanvas": 1, "title": "", "groups": [{"id": "A", "name": "Alpha"}, {"id": "B", "name": "Beta"}],
                 "tasks": [task("A-01", group="A"), task("A-02", group="A"), task("B-01", group="B", deps=["A-01", "A-02"])]}
        code = ("const b = normalize(input), before = clone(b);\n"
                "removeGroup(b, 'A', ['a-01', 'B-01']);\n"
                "const after = clone(b);\n"
                "replay(b, after, before);\n"
                "process.stdout.write(JSON.stringify({ before, after, undone: b }));")
        out = json.loads(run_html_js(self, code, json.dumps(board)))
        after = out["after"]
        self.assertEqual(after["groups"], [{"id": "B", "name": "Beta"}])
        self.assertEqual([(t["id"], t["group"], t["deps"]) for t in after["tasks"]], [("A-02", None, []), ("B-01", "B", ["A-02"])])
        self.assertEqual(out["undone"], out["before"])

    def test_a_task_someone_changed_is_not_removed(self):
        created = self.board(task("A-02"))
        board, applied, skipped = self.replay(self.board(task("A-02", notes=[self.AGENT])), created, self.board())
        self.assertEqual([t["id"] for t in board["tasks"]], ["A-01", "A-02"])
        self.assertEqual((applied, skipped), (0, 1))
        board, applied, _ = self.replay(created, created, self.board())
        self.assertEqual([t["id"] for t in board["tasks"]], ["A-01"])
        self.assertEqual(applied, 1)

    def test_identical_notes_are_told_apart_by_rank(self):
        code = ("const t = normalize(input).tasks[1];\n"
                "process.stdout.write(JSON.stringify([noteRank(t, t.notes[0]), noteRank(t, t.notes[1]), noteRank(t, { ...t.notes[0] }),\n"
                "  noteIndex(t, t.notes[0], 1), noteIndex(t, t.notes[0], 5), noteIndex(t, t.notes[2])]));")
        out = run_html_js(self, code, json.dumps(self.notes(self.NOTE, self.NOTE, self.AGENT)))
        self.assertEqual(json.loads(out), [0, 1, 0, 1, 1, 2])

    def test_undo_labels_name_the_action(self):
        def board(*tasks, groups=("A", "B")):
            return {"kanvas": 1, "title": "", "groups": [{"id": g, "name": f"Group {g}"} for g in groups], "tasks": list(tasks)}

        def change(fn):
            before = board(task("A-01", "proposed", "A"), task("A-02", "doing", "A", ["A-01"], notes=[self.NOTE]),
                           task("A-03", "review", "A", ["A-02"]), task("A-04", "done", "A"), task("A-05", "proposed", "A"))
            after = json.loads(json.dumps(before))
            fn(after, {t["id"]: t for t in after["tasks"]})
            return [before, after]

        def drop(b, ids):
            b["tasks"] = [t for t in b["tasks"] if t["id"] not in ids]
            for t in b["tasks"]:
                t["deps"] = [d for d in t["deps"] if d not in ids]

        cases = {
            "approving A-01": lambda b, t: t["A-01"].update(state="todo"),
            "pausing A-02": lambda b, t: t["A-02"].update(state="todo"),
            "sending A-03 back": lambda b, t: t["A-03"].update(state="todo", notes=[self.NOTE]),
            "reopening A-04": lambda b, t: t["A-04"].update(state="todo"),
            "starting A-01": lambda b, t: t["A-01"].update(state="doing"),
            "marking A-02 and A-03 done": lambda b, t: [t[i].update(state="done") for i in ("A-02", "A-03")],
            "moving A-04 to Proposed": lambda b, t: t["A-04"].update(state="proposed"),
            "rejecting A-01": lambda b, t: drop(b, {"A-01"}),
            "deleting A-02": lambda b, t: drop(b, {"A-02"}),
            "rejecting A-01 and A-05": lambda b, t: drop(b, {"A-01", "A-05"}),
            "unlinking A-03 from A-02": lambda b, t: t["A-03"].update(deps=[]),
            "making A-05 wait for A-01 and A-04": lambda b, t: t["A-05"].update(deps=["A-01", "A-04"]),
            "adding A-06": lambda b, t: (b["tasks"].append(task("A-06", group="A")), t["A-05"].update(deps=["A-06"])),
            "turning on YOLO up to A-03": lambda b, t: (t["A-03"].update(yolo=True), t["A-01"].update(state="todo")),
            "deleting the Group A group": lambda b, t: (b["groups"].pop(0), [x.update(group=None) for x in b["tasks"]]),
            "adding a note to A-02": lambda b, t: t["A-02"]["notes"].append(self.AGENT),
            "deleting a note from A-02": lambda b, t: t["A-02"].update(notes=[]),
            "editing a note on A-02": lambda b, t: t["A-02"].update(notes=[dict(self.NOTE, text="New")]),
            "asking for changes to A-05": lambda b, t: t["A-05"].update(notes=[dict(self.NOTE, text="Changes requested: Split it")]),
            "renaming A-02": lambda b, t: t["A-02"].update(title="New title"),
            "moving A-02 to Group B": lambda b, t: t["A-02"].update(group="B"),
            "reordering the cards": lambda b, t: b["tasks"].reverse(),
            "renaming the Group B group": lambda b, t: b["groups"][1].update(name="Beta"),
            "renaming the board": lambda b, t: b.update(title="Board"),
        }
        pairs = [change(fn) for fn in cases.values()]
        yolo_off = change(lambda b, t: t["A-03"].update(yolo=True))[::-1]
        many = [board(*(task(f"A-0{i}", "proposed") for i in range(1, 6))), board(*(task(f"A-0{i}") for i in range(1, 6)))]
        code = "process.stdout.write(JSON.stringify(input.map(([a, z]) => describeChange(normalize(a), normalize(z)))));"
        out = run_html_js(self, code, json.dumps(pairs + [yolo_off, many]), [("const MOVE_WORDS =", "\n// Undoes or redoes")])
        self.assertEqual(json.loads(out), list(cases) + ["turning off YOLO on A-03", "approving 5 tasks"])


class FailedSaveTests(unittest.TestCase):
    """A change that is not saved hands its text back through commit()'s onFail, and the board goes back."""

    SECTIONS = [("async function commit(", "async function poll("), ("function mustFind(", "\n// ----")]

    def run_commits(self, code, board):
        setup = (
            "let board, confirmed, store, lost = null, closingBoard = false, pending = 0, writes = 0, saveQueue = Promise.resolve();\n"
            "const log = [], failed = [];\n"
            "toast = (msg) => log.push(msg);\n"
            "const setBoard = (b) => { board = b; }, render = () => {}, remember = () => ({}), forget = () => {}, setSync = () => {};\n"
            "const onFail = (e) => failed.push(e.message);\n"
            "board = normalize(input); confirmed = clone(board);\n"
        )
        return json.loads(run_html_js(self, setup + code, json.dumps(board), self.SECTIONS))

    def test_a_failed_save_calls_on_fail_and_restores_the_board(self):
        board = {"kanvas": 1, "title": "", "groups": [], "tasks": [task("A-01")]}
        out = self.run_commits(
            "store = { load: async () => clone(confirmed), save: async () => { throw new Error('server answered 500'); } };\n"
            "(async () => {\n"
            "  const refused = await commit(ops.note('A-09', 'Typed text'), { onFail });\n"
            "  const queued = await commit(ops.note('A-01', 'Typed text'), { onFail });\n"
            "  const shown = board.tasks[0].notes.length;\n"
            "  await saveQueue;\n"
            "  process.stdout.write(JSON.stringify({ refused, queued, shown, after: board.tasks[0].notes.length, failed, log }));\n"
            "})();", board)
        self.assertEqual((out["refused"], out["queued"], out["shown"], out["after"]), (False, True, 1, 0))
        self.assertEqual(out["failed"], ["A-09 no longer exists", "server answered 500"])
        self.assertEqual(out["log"], ["A-09 no longer exists", "Not saved: server answered 500"])

    def test_a_saved_change_does_not_call_on_fail(self):
        board = {"kanvas": 1, "title": "", "groups": [], "tasks": [task("A-01")]}
        out = self.run_commits(
            "store = { load: async () => clone(confirmed), save: async (fn, base) => { const b = clone(base); fn(b); return { board: b, base }; } };\n"
            "(async () => {\n"
            "  await commit(ops.note('A-01', 'Typed text'), { onFail });\n"
            "  await saveQueue;\n"
            "  process.stdout.write(JSON.stringify({ notes: confirmed.tasks[0].notes.map((n) => n.text), failed, log }));\n"
            "})();", board)
        self.assertEqual(out, {"notes": ["Typed text"], "failed": [], "log": []})

    def test_changes_wait_while_the_board_cannot_be_reached(self):
        board = {"kanvas": 1, "title": "", "groups": [], "tasks": [task("A-01")]}
        out = self.run_commits(
            "store = { save: async () => { throw new Error('tried to save'); } };\n"
            "lost = 'Kanvas stopped running or cannot be reached';\n"
            "(async () => {\n"
            "  const ok = await commit(ops.note('A-01', 'Typed text'), { onFail });\n"
            "  await saveQueue;\n"
            "  process.stdout.write(JSON.stringify({ ok, pending, notes: board.tasks[0].notes.length, failed, log }));\n"
            "})();", board)
        self.assertEqual(out, {"ok": False, "pending": 0, "notes": 0, "failed": ["Kanvas stopped running or cannot be reached"],
                               "log": ["Not saved: Kanvas stopped running or cannot be reached"]})


class LostConnectionTests(unittest.TestCase):
    """The server store tells a board it cannot reach from other errors, in plain words."""

    def test_errors_say_when_the_board_cannot_be_reached(self):
        code = (
            "const setTimeout = (fn) => { fn(); return 0; };\n"
            "let answer;\n"
            "const fetch = (url, opts) => new Promise((resolve, reject) => {\n"
            "  // setTimeout runs at once, so a request with a time limit is aborted before it starts; one without gets its late answer.\n"
            "  if (answer === 'hang' && opts.signal.aborted) reject(new DOMException('aborted', 'AbortError'));\n"
            "  else if (answer === 'hang') resolve({ ok: true, status: 200, text: async () => '{}', headers: new Map() });\n"
            "  else if (answer === 'down') reject(new TypeError('Failed to fetch'));\n"
            "  else resolve({ ok: false, status: answer, statusText: '', text: async () => '{\"error\": \"No board\"}', headers: new Map() });\n"
            "});\n"
            "(async () => {\n"
            "  const out = {};\n"
            "  for (answer of ['down', 'hang', 404, 500]) {\n"
            "    for (const step of ['load', 'poll', 'save']) {\n"
            "      try { await serverStore()[step]((b) => b, normalize({})); out[`${answer} ${step}`] = 'no error'; }\n"
            "      catch (e) { out[`${answer} ${step}`] = [e.message, !!e.lost]; }\n"
            "    }\n"
            "  }\n"
            "  process.stdout.write(JSON.stringify(out));\n"
            "})();"
        )
        out = json.loads(run_html_js(self, code, "{}", [("function serverStore()", "function fileStore(")]))
        down = ["Kanvas stopped running or cannot be reached", True]
        closed = ["this board was closed in Kanvas", True]
        self.assertEqual(out, {
            "down load": down, "down poll": down, "down save": down,
            # Only reads give up on a request that hangs: a write may have landed, and waits for its answer.
            "hang load": down, "hang poll": down, "hang save": "no error",
            "404 load": closed, "404 poll": closed, "404 save": ["No board", False],
            "500 load": ["server answered 500", False], "500 poll": ["server answered 500", False], "500 save": ["No board", False],
        })


class FileStoreTests(TempBoard):
    """In file mode, an agent's CLI write that lands while the page saves is kept, and the page's change goes on top."""

    def save_with_agent(self, writes):
        """Saves a human note on DV-03 while the CLI adds the agent notes in `writes` ({step: [text, ...]}) at that step.

        The handle works as Chrome's does: createWritable() fills a swap file, and close() moves it over the board.
        """
        code = (
            "const fs = require('fs'), { execFileSync } = require('child_process');\n"
            "const [path, python, script, writes] = input;\n"
            "let aborted = 0;\n"
            "const agent = (step) => { const text = (writes[step] || []).shift();\n"
            "  if (text) execFileSync(python, [script, 'note', 'DV-02', text, '--board', path, '--as', 'agent']); };\n"
            "const handle = {\n"
            "  name: 'kanvas.json',\n"
            "  async getFile() { const text = fs.readFileSync(path, 'utf8'), st = fs.statSync(path);\n"
            "    return { lastModified: st.mtimeMs, size: st.size, text: async () => text }; },\n"
            "  async createWritable() {\n"
            "    const swap = path + '.crswap';\n"
            "    fs.writeFileSync(swap, '');\n"
            "    return { async write(t) { fs.writeFileSync(swap, t); agent('write'); },\n"
            "             async close() { fs.renameSync(swap, path); agent('close'); },\n"
            "             async abort() { fs.unlinkSync(swap); aborted++; } };\n"
            "  },\n"
            "};\n"
            "(async () => {\n"
            "  const store = fileStore(handle), base = await store.load();\n"
            "  let saved, error = null;\n"
            "  try { saved = (await store.save(ops.note('DV-03', 'Human note'), base)).board; } catch (e) { error = e.message; }\n"
            "  const notes = (b) => b.tasks.flatMap((t) => t.notes.map((n) => `${t.id} ${n.by}: ${n.text}`));\n"
            "  const file = fs.readFileSync(path, 'utf8'), polled = await store.poll();\n"
            "  process.stdout.write(JSON.stringify({ error, aborted, saved: saved ? notes(saved) : null, file: notes(normalize(JSON.parse(file))),\n"
            "    canonical: serialize(normalize(JSON.parse(file))) === file, polled: polled && notes(polled.board),\n"
            "    swap: fs.existsSync(path + '.crswap') }));\n"
            "})();"
        )
        stdin = json.dumps([self.path, sys.executable, SCRIPT, writes])
        sections = [("function fileStore(", "function memoryStore("), ("function mustFind(", "\n// ----")]
        return json.loads(run_html_js(self, code, stdin, sections))

    def test_a_save_without_agents_writes_once(self):
        out = self.save_with_agent({})
        self.assertEqual(out, {"error": None, "aborted": 0, "saved": ["DV-03 human: Human note"], "file": ["DV-03 human: Human note"],
                               "canonical": True, "polled": None, "swap": False})

    def test_an_agent_write_during_a_save_is_kept(self):
        out = self.save_with_agent({"write": ["Agent note"]})
        both = ["DV-02 agent: Agent note", "DV-03 human: Human note"]
        self.assertEqual(out, {"error": None, "aborted": 1, "saved": both, "file": both, "canonical": True, "polled": None, "swap": False})

    def test_an_agent_write_right_after_a_save_shows_at_the_next_poll(self):
        out = self.save_with_agent({"close": ["Agent note"]})
        both = ["DV-02 agent: Agent note", "DV-03 human: Human note"]
        self.assertEqual(out, {"error": None, "aborted": 0, "saved": ["DV-03 human: Human note"], "file": both, "canonical": True,
                               "polled": both, "swap": False})

    def test_a_save_gives_up_when_the_file_keeps_changing(self):
        notes = [f"Agent note {i}" for i in range(6)]
        out = self.save_with_agent({"write": list(notes)})
        agent = [f"DV-02 agent: {n}" for n in notes]
        self.assertEqual(out, {"error": "the board kept changing; try again", "aborted": 6, "saved": None, "file": agent,
                               "canonical": True, "polled": agent, "swap": False})


class LeaveTests(unittest.TestCase):
    """Leaving the page asks first only while something would be lost."""

    def test_unsaved_work(self):
        code = (
            "let fields = [], pending = 0, uploads = 0, store, board = normalize(input), IX = indexOf(board);\n"
            "globalThis.document = { querySelector: () => null, querySelectorAll: () => fields };\n"
            "const get = (id) => IX.get(key(id)), ui = {}, drafts = { note: new Map(), back: new Map() };\n"
            "const reset = () => {\n"
            "  fields = []; pending = 0; uploads = 0; store = { kind: 'server' };\n"
            "  Object.assign(ui, { selected: 'A-01', editingDesc: false, editingNote: null });\n"
            "  Object.assign(drafts, { desc: null, noteEdit: null, title: null, boardTitle: null });\n"
            "  drafts.note.clear(); drafts.back.clear();\n"
            "};\n"
            "const cases = {\n"
            "  clean: () => {},\n"
            "  pendingSave: () => { pending = 1; },\n"
            "  imageUploading: () => { uploads = 1; },\n"
            "  memoryBoard: () => { store = { kind: 'memory', dirty: false }; },\n"
            "  memoryBoardChanged: () => { store = { kind: 'memory', dirty: true }; },\n"
            "  snapshot: () => { store = { kind: 'snapshot', readOnly: true }; drafts.note.set('A-01', 'typed'); },\n"
            "  note: () => { drafts.note.set('A-01', 'typed'); },\n"
            "  blankNote: () => { drafts.note.set('A-01', '  '); },\n"
            "  noteOnRemovedTask: () => { drafts.note.set('A-09', 'typed'); },\n"
            "  descEditorUnchanged: () => { ui.editingDesc = true; drafts.desc = 'Old '; },\n"
            "  descEditorChanged: () => { ui.editingDesc = true; drafts.desc = 'New'; },\n"
            "  noteEditorChanged: () => { ui.editingNote = { text: 'Was' }; drafts.noteEdit = 'Is'; },\n"
            "  givenBack: () => { drafts.back.set(key('A-01'), [() => {}]); },\n"
            "  givenBackRemovedTask: () => { drafts.back.set(key('A-09'), [() => {}]); },\n"
            "  dialogFilled: () => { fields = [{ value: 'A task' }]; },\n"
            "  dialogEmpty: () => { fields = [{ value: ' ' }]; },\n"
            "};\n"
            "const out = {};\n"
            "for (const [name, set] of Object.entries(cases)) { reset(); set(); out[name] = unsaved(); }\n"
            "process.stdout.write(JSON.stringify(out));"
        )
        board = {"kanvas": 1, "title": "", "groups": [], "tasks": [task("A-01", desc="Old")]}
        out = json.loads(run_html_js(self, code, json.dumps(board), [("function unsaved(", "async function boot(")]))
        asks = {name for name, value in out.items() if value}
        self.assertEqual(asks, {"pendingSave", "imageUploading", "memoryBoardChanged", "note", "descEditorChanged", "noteEditorChanged", "givenBack", "dialogFilled"})


class ChecklistTests(unittest.TestCase):
    """A checkbox ticked in a rendered description changes the item it shows and nothing else."""

    LINES = [
        "Steps, with a fake - [ ] inside a sentence",
        "- [ ] plain",
        "  - [x] nested",
        "    continued",
        "* [X] star, upper case",
        "+ [ ] plus",
        "1. [ ] numbered",
        "2) [x] numbered with a parenthesis",
        "> - [ ] quoted",
        "```",
        "- [ ] not real",
        "```",
        "- [ ]",
        "- not a task [ ]",
        "- [ ] plain",
        "| a | - [ ] in a table |",
        "|---|---|",
        "- [x] after a table",
        "  - [ ] child",
        "    - [x] grandchild",
        "1. [ ] numbered after bullets",
        ">>>>>> - [x] six quotes deep",
        ">>>>>>> - [ ] seven quotes deep, so text",
        "- [ ]\u2028a line separator after the box",
        "-\u2029[x] separators\u2028inside",
    ]
    DESC = "\n".join(LINES)
    # The line of each item in DESC, in the order the boxes are rendered.
    ITEMS = [(1, "plain", False), (2, "nested continued", True), (4, "star, upper case", True), (5, "plus", False),
             (6, "numbered", False), (7, "numbered with a parenthesis", True), (8, "quoted", False), (14, "plain", False),
             (17, "after a table", True), (18, "child", False), (19, "grandchild", True), (20, "numbered after bullets", False),
             (21, "six quotes deep", True), (23, "a line separator after the box", False), (24, "separators\u2028inside", True)]

    def run_js(self, code, desc=DESC):
        code = "const get = () => null;\n" + code
        return json.loads(run_html_js(self, code, json.dumps(desc), [("function inlineMd(", "\n// ----")]))

    def test_the_items_are_the_boxes_markdown_renders(self):
        out = self.run_js("process.stdout.write(JSON.stringify([taskItems(input).map((x) => [x.text, x.checked]),\n"
                          "  [...markdown(input).matchAll(/<input type=\"checkbox\" disabled( checked)?>/g)].map((m) => !!m[1])]));")
        self.assertEqual(out, [[[text, checked] for _, text, checked in self.ITEMS], [checked for *_, checked in self.ITEMS]])

    def test_a_quote_ten_thousand_deep_holds_no_boxes(self):
        out = self.run_js("process.stdout.write(JSON.stringify([taskItems(input), markdown(input).includes('checkbox')]));", ">" * 10000 + " - [ ] deep")
        self.assertEqual(out, [[], False])

    def test_toggling_changes_only_the_line_of_that_item(self):
        out = self.run_js("const items = taskItems(input);\n"
                          "process.stdout.write(JSON.stringify(items.map((x, i) => toggleTask(input, x.text,\n"
                          "  items.slice(0, i).filter((y) => y.text === x.text).length, !x.checked))));")
        for (line, _, checked), toggled in zip(self.ITEMS, out):
            lines = list(self.LINES)
            lines[line] = re.sub(r"\[[ xX]\]", "[ ]" if checked else "[x]", lines[line], count=1)
            self.assertEqual(toggled, "\n".join(lines))
        self.assertEqual(len(out), len(self.ITEMS))

    def test_missing_items_and_items_already_in_that_state(self):
        out = self.run_js("process.stdout.write(JSON.stringify([toggleTask(input, 'gone', 0, true), toggleTask(input, 'plain', 2, true),\n"
                          "  toggleTask(input, 'plain', 1, false) === input, toggleTask(input, 'star, upper case', 0, true) === input,\n"
                          "  toggleTask(input.replace(/\\n/g, '\\r\\n'), 'plus', 0, true)]));")
        self.assertEqual(out, [None, None, True, True, self.DESC.replace("+ [ ] plus", "+ [x] plus").replace("\n", "\r\n")])


def link(href, text):
    return f'<a href="{href}" target="_blank" rel="noopener noreferrer">{text}</a>'


def ref(text, tid="RS-02"):
    return f'<a class="tref" href="#{tid}" data-ref="{tid}">{text}</a>'


def table(head, *rows, align=()):
    def row(cells, tag):
        out = ""
        for cell, a in zip(cells, list(align) + [None] * len(cells)):
            attr = f' style="text-align: {a}"' if a else ""
            out += f"<{tag}{attr}>{cell}</{tag}>"
        return f"<tr>{out}</tr>"
    body = f"<tbody>{''.join(row(r, 'td') for r in rows)}</tbody>" if rows else ""
    return f'<div class="table"><table><thead>{row(head, "th")}</thead>{body}</table></div>'


class MarkdownTests(unittest.TestCase):
    """markdown() turns text from agents into HTML: the subset it knows, with everything else escaped."""

    def check(self, cases):
        code = ("const tasks = new Map([['RS-02', { id: 'RS-02' }]]);\n"
                "const get = (id) => tasks.get(key(id));\n"
                "process.stdout.write(JSON.stringify(input.map((src) => markdown(src))));")
        out = json.loads(run_html_js(self, code, json.dumps([src for src, _ in cases]), [("function inlineMd(", "\n// ----")]))
        for (src, expected), html in zip(cases, out):
            with self.subTest(src=src):
                self.assertEqual(html, expected)

    def test_task_ids_link_outside_link_text_only(self):
        self.check([
            ("RS-02 and rs-02, XX-99 is unknown", f"<p>{ref('RS-02')} and {ref('rs-02')}, XX-99 is unknown</p>"),
            ("[see RS-02](https://example.com)", f"<p>{link('https://example.com', 'see RS-02')}</p>"),
            ("RS-02, [see RS-02](https://example.com) and RS-02",
             f"<p>{ref('RS-02')}, {link('https://example.com', 'see RS-02')} and {ref('RS-02')}</p>"),
            ("[**RS-02** first](mailto:a@example.com)", f"<p>{link('mailto:a@example.com', '<strong>RS-02</strong> first')}</p>"),
            ("[RS-02](https://example.com/RS-02) https://example.com/RS-02",
             f"<p>{link('https://example.com/RS-02', 'RS-02')} {link('https://example.com/RS-02', 'https://example.com/RS-02')}</p>"),
            # A refused URL leaves plain text, where a task ID is a reference again.
            ("[see RS-02](#RS-02) [x](vbscript:RS-02)", f"<p>see {ref('RS-02')} x</p>"),
            ("`RS-02` *RS-02*", f"<p><code>RS-02</code> <em>{ref('RS-02')}</em></p>"),
        ])

    def test_urls_hold_balanced_parentheses(self):
        foo, nested = "https://en.wikipedia.org/wiki/Foo_(bar)", "https://example.com/a_(b_(c))_d"
        self.check([
            (f"[spec]({foo})", f"<p>{link(foo, 'spec')}</p>"),
            (f"[spec]({nested}) {nested}", f"<p>{link(nested, 'spec')} {link(nested, nested)}</p>"),
            (f"{foo}, ({foo}) and ({foo}).", f"<p>{link(foo, foo)}, ({link(foo, foo)}) and ({link(foo, foo)}).</p>"),
            ("(see [spec](https://example.com/a)) (see https://example.com/a)",
             f"<p>(see {link('https://example.com/a', 'spec')}) (see {link('https://example.com/a', 'https://example.com/a')})</p>"),
            ("[RS-02 notes](https://example.com/RS-02_(draft))", f"<p>{link('https://example.com/RS-02_(draft)', 'RS-02 notes')}</p>"),
            # Unbalanced: a link needs its closing parenthesis, and a bare URL ends before one that closes nothing.
            ("[spec](https://example.com/a_(b) and more",
             f"<p>[spec]({link('https://example.com/a_(b)', 'https://example.com/a_(b)')} and more</p>"),
            ("[spec](https://example.com/a_b)) https://example.com/a)b https://example.com/a_(b c",
             f"<p>{link('https://example.com/a_b', 'spec')}) {link('https://example.com/a', 'https://example.com/a')})b "
             f"{link('https://example.com/a_', 'https://example.com/a_')}(b c</p>"),
            ("[spec](https://example.com/a(b c)", f"<p>[spec]({link('https://example.com/a', 'https://example.com/a')}(b c)</p>"),
            # The rest is as before.
            ('Go to https://example.com/a.b?q=1&r=2. "https://example.com/x"!',
             f"<p>Go to {link('https://example.com/a.b?q=1&amp;r=2', 'https://example.com/a.b?q=1&amp;r=2')}. "
             f"&quot;{link('https://example.com/x', 'https://example.com/x')}&quot;!</p>"),
            ("[js link](javascript:alert(1)) and [ok](https://example.com)", f"<p>js link and {link('https://example.com', 'ok')}</p>"),
            ('[x](https://example.com/"onmouseover="alert(1))',
             f"<p>{link('https://example.com/&quot;onmouseover=&quot;alert(1)', 'x')}</p>"),
            ("[d](data:text/html,<script>alert(1)</script>) https://x", "<p>d https://x</p>"),
        ])

    def test_tables(self):
        one = table(["a"], ["1"])
        self.check([
            ("| Left | Center | Right | None |\n|:-----|:------:|------:|------|\n| a | b | c | d |",
             '<div class="table"><table><thead><tr><th style="text-align: left">Left</th><th style="text-align: center">Center</th>'
             '<th style="text-align: right">Right</th><th>None</th></tr></thead><tbody><tr><td style="text-align: left">a</td>'
             '<td style="text-align: center">b</td><td style="text-align: right">c</td><td>d</td></tr></tbody></table></div>'),
            # Outer pipes are optional, \| is a pipe inside a cell, code included, and cells hold inline Markdown.
            ("Name | Note\n--- | :-:\n`a\\|b` | x \\| y, **RS-02**",
             table(["Name", "Note"], ["<code>a|b</code>", f"x | y, <strong>{ref('RS-02')}</strong>"], align=[None, "center"])),
            # Missing cells are empty and extra ones dropped.
            ("| a | b |\n|---|---|\n| 1 |\n| 1 | 2 | 3 |\n|| 2 |", table(["a", "b"], ["1", ""], ["1", "2"], ["", "2"])),
            ("| a |\n|---|", table(["a"])),
            # A table may follow a paragraph line or a list, and ends at a line without a pipe or a new block.
            ("Results:\n| a |\n|---|\n| 1 |\nafter\n\n| b |\n|---|\n\n| 2 |", f"<p>Results:</p>{one}<p>after</p>{table(['b'])}<p>| 2 |</p>"),
            ("- item\n| a |\n|---|\n| 1 |\n- next", f"<ul><li>item</li></ul>{one}<ul><li>next</li></ul>"),
            ("1. one\n\n| a |\n|---|\n| 1 |\n2. two\n> | a |\n> |---|\n> | 1 |",
             f'<ol><li>one</li></ol>{one}<ol><li>two</li></ol><blockquote>{one}</blockquote>'),
            # Not tables: cell counts differ, no row of dashes, or dashes with no pipe.
            ("| a | b |\n|---|\n\na | b\nc | d\n\n| a |\n| - - |\n\nTitle\n---",
             "<p>| a | b |<br>|---|</p><p>a | b<br>c | d</p><p>| a |<br>| - - |</p><p>Title</p><hr>"),
            ("| <script>alert(1)</script> | [x](javascript:alert(1)) |\n|:-:|---|\n| <img src=x onerror=alert(1)> | ![y](https://example.com/\"onerror=\"alert(1)) |",
             table(["&lt;script&gt;alert(1)&lt;/script&gt;", "x"],
                   ["&lt;img src=x onerror=alert(1)&gt;", link("https://example.com/&quot;onerror=&quot;alert(1)", "y")], align=["center"])),
        ])

    def test_nested_lists(self):
        box, ticked = '<li class="task"><input type="checkbox" disabled>', '<li class="task"><input type="checkbox" disabled checked>'
        self.check([
            ("- a\n  - b\n    - c\n  - d\n- e", "<ul><li>a<ul><li>b<ul><li>c</li></ul></li><li>d</li></ul></li><li>e</li></ul>"),
            ("1. one\n   - sub\n   - sub two\n2. two\n   1. deep",
             "<ol><li>one<ul><li>sub</li><li>sub two</li></ul></li><li>two<ol><li>deep</li></ol></li></ol>"),
            # A switch between bullets and numbers starts a new list.
            ("- a\n- b\n1. c\n2) d\n* e", "<ul><li>a</li><li>b</li></ul><ol><li>c</li><li>d</li></ol><ul><li>e</li></ul>"),
            ("- [ ] parent\n  - [x] child\n    with more\n  - plain child, RS-02\n- [X] next",
             f"<ul>{box} parent<ul>{ticked} child with more</li><li>plain child, {ref('RS-02')}</li></ul></li>{ticked} next</li></ul>"),
            # Two columns deeper make a child; in between, an item joins the deeper list. A tab counts as four.
            ("- a\n - b\n    - c\n  - d\n\t- e", "<ul><li>a</li><li>b<ul><li>c</li><li>d</li><li>e</li></ul></li></ul>"),
            ("  - a\n- b", "<ul><li>a</li><li>b</li></ul>"),
            ("Steps:\n1. a\n2. b\n\n- c\n\n- d", "<p>Steps:</p><ol><li>a</li><li>b</li></ol><ul><li>c</li></ul><ul><li>d</li></ul>"),
            ("> - a\n>   - b", "<blockquote><ul><li>a<ul><li>b</li></ul></li></ul></blockquote>"),
            ("\n".join(" " * 2 * k + f"- {k}" for k in range(8)),
             "<ul><li>0<ul><li>1<ul><li>2<ul><li>3<ul><li>4<ul><li>5</li><li>6</li><li>7</li></ul></li></ul></li></ul></li></ul></li></ul></li></ul>"),
        ])

    def test_quotes_nest_six_deep_at_most(self):
        six = lambda html: "<blockquote>" * 6 + html + "</blockquote>" * 6
        self.check([
            ("> one\n> two **RS-02**\n>\n>no space", f"<blockquote><p>one<br>two <strong>{ref('RS-02')}</strong></p><p>no space</p></blockquote>"),
            ("before\n> # Title\n> ```\n> <b>\n> ```\n> 1. one\n> ---\nafter",
             "<p>before</p><blockquote><h3>Title</h3><pre><code>&lt;b&gt;</code></pre><ol><li>one</li></ol><hr></blockquote><p>after</p>"),
            ("> a\n>> b\n> > c\n> d", "<blockquote><p>a</p><blockquote><p>b<br>c</p></blockquote><p>d</p></blockquote>"),
            (">>>>>> six\n>>>>>> - [x] box", six('<p>six</p><ul><li class="task"><input type="checkbox" disabled checked> box</li></ul>')),
            # Deeper markers are text, in the paragraph they stand in.
            ("> > > > > > > seven\n>>>>>>>> - [ ] eight, RS-02", six(f"<p>&gt; seven<br>&gt;&gt; - [ ] eight, {ref('RS-02')}</p>")),
            (">" * 10000 + " deep", six("<p>" + "&gt;" * 9994 + " deep</p>")),
            ("> " * 10000 + "deep", six("<p>" + "&gt; " * 9994 + "deep</p>")),
        ])

    def test_images_are_plain_links(self):
        self.check([
            ("![img](https://example.com/x.png)", f"<p>{link('https://example.com/x.png', 'img')}</p>"),
            ("![](https://example.com/x_y_z.png) and [](https://example.com)",
             f"<p>{link('https://example.com/x_y_z.png', 'https://example.com/x_y_z.png')} and {link('https://example.com', 'https://example.com')}</p>"),
            ("![see RS-02](https://example.com/a_(b).png)", f"<p>{link('https://example.com/a_(b).png', 'see RS-02')}</p>"),
            ("![x](javascript:alert(1)) ![RS-02](x.png) ![](data:image/png;base64,AAAA)", f"<p>x {ref('RS-02')} data:image/png;base64,AAAA</p>"),
            ("![<img src=x onerror=alert(1)>](https://example.com/x.png)",
             f"<p>{link('https://example.com/x.png', '&lt;img src=x onerror=alert(1)&gt;')}</p>"),
        ])

    def test_the_stress_boards_hostile_description(self):
        desc = ("<script>alert(1)</script>\n\n[js link](javascript:alert(1)) and [ok](https://example.com)\n\n"
                "| a | b |\n|---|---|\n| 1 | 2 |\n\n![img](https://example.com/x.png)\n\n- [ ] first\n- [x] second\n  - nested item\n\n"
                "1. one\n2. two\n\n# Heading one\n\n> quote with RS-02 reference")
        self.check([(desc, (
            "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"
            f"<p>js link and {link('https://example.com', 'ok')}</p>"
            + table(["a", "b"], ["1", "2"])
            + f"<p>{link('https://example.com/x.png', 'img')}</p>"
            '<ul><li class="task"><input type="checkbox" disabled> first</li>'
            '<li class="task"><input type="checkbox" disabled checked> second<ul><li>nested item</li></ul></li></ul>'
            "<ol><li>one</li><li>two</li></ol><h3>Heading one</h3>"
            f"<blockquote><p>quote with {ref('RS-02')} reference</p></blockquote>"))])

    def test_html_stays_escaped(self):
        self.check([
            ("<script>alert(1)</script>", "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"),
            ("<img src=x onerror=alert(1)> **bold** `code`", "<p>&lt;img src=x onerror=alert(1)&gt; <strong>bold</strong> <code>code</code></p>"),
            ("# <i>RS-02</i>", f"<h3>&lt;i&gt;{ref('RS-02')}&lt;/i&gt;</h3>"),
            ("```\n<b>'x'</b>\n```", "<pre><code>&lt;b&gt;&#39;x&#39;&lt;/b&gt;</code></pre>"),
            ('[<b>RS-02</b>](https://example.com/"><script>)',
             f"<p>{link('https://example.com/&quot;&gt;&lt;script&gt;', '&lt;b&gt;RS-02&lt;/b&gt;')}</p>"),
            ('https://example.com/a"onmouseover="x', f"<p>{link('https://example.com/a&quot;onmouseover=&quot;x', 'https://example.com/a&quot;onmouseover=&quot;x')}</p>"),
            ("[a](javascript:x) [b](JavaScript:x) [c](data:text/html,x) [d](//example.com)", "<p>a b c d</p>"),
        ])

    def test_emphasis_and_links(self):
        e = "https://e.com"
        self.check([
            ("*a* _b_ **c** ~~d~~", "<p><em>a</em> <em>b</em> <strong>c</strong> <del>d</del></p>"),
            ("**a *b* c** and *a **b** c*", "<p><strong>a <em>b</em> c</strong> and <em>a <strong>b</strong> c</em></p>"),
            # Not inside words. Emphasis runs to the first closer after its opener, and an opener without one is text.
            ("a*b*c, snake_case_name and 2*3*4", "<p>a*b*c, snake_case_name and 2*3*4</p>"),
            ("*a *b c* and _a _b c_", "<p><em>a *b c</em> and <em>a _b c</em></p>"),
            ("x ** y* and * a* and *a *", "<p>x <em>* y</em> and * a* and <em>a </em></p>"),
            ("*a* *b _c_ _d", "<p><em>a</em> *b <em>c</em> _d</p>"),
            # A link's text runs from its "[" to the first "]" after it.
            (f"[a [b]({e}) and [x] [y]({e}) [z]", f"<p>{link(e, 'a [b')} and [x] {link(e, 'y')} [z]</p>"),
            (f"![x]({e}) !![y]({e}) [![z]({e})", f"<p>{link(e, 'x')} !{link(e, 'y')} {link(e, '![z')}</p>"),
            (f"[`a`]({e}) and [a `b]({e})` c", f"<p>{link(e, '`a`')} and {link(e, 'a `b')}` c</p>"),
            (f"`[`[b]({e}) `a` and [c]({e})`", f"<p><code>[</code>{link(e, 'b')} <code>a</code> and {link(e, 'c')}`</p>"),
            (f"[a]]]({e}) [a] ({e})", f"<p>[a]]]({link(e, e)}) [a] ({link(e, e)})</p>"),
            (f"[*a* RS-02]({e}) [a](javascript:x) [RS-02](#x)", f"<p>{link(e, '<em>a</em> RS-02')} a {ref('RS-02')}</p>"),
        ])

    def test_long_lines_of_unclosed_openers_render_quickly(self):
        # 100 KB of each, then the end of the line: [unit, end, the HTML of the end].
        cases = [[" *a", "", ""], [" _a", "", ""], ["(*a", "", ""], ["[", "", ""], ["[a", "", ""], ["![", "", ""], [" *a _a [a", "", ""],
                 ["[ *a", "", ""], ["[a", "]", "]"], [" *a", " _b_", " <em>b</em>"], [" _a", " [c](https://e.com)", f" {link('https://e.com', 'c')}"]]
        code = ("const get = () => null;\n"
                "process.stdout.write(JSON.stringify(input.map(([unit, end, endHtml]) => {\n"
                "  const head = unit.repeat(Math.ceil(100000 / unit.length)), t = process.hrtime.bigint(), html = markdown(head + end);\n"
                "  return [Number(process.hrtime.bigint() - t) / 1e6, html === `<p>${escHtml(head)}${endHtml}</p>`];\n"
                "})));")
        out = json.loads(run_html_js(self, code, json.dumps(cases), [("function inlineMd(", "\n// ----")]))
        for (unit, end, _), (ms, same) in zip(cases, out):
            with self.subTest(unit=unit, end=end):
                self.assertTrue(same)
                self.assertLess(ms, 1000)

    def test_line_separators_are_part_of_their_line(self):
        # U+2028 and U+2029 end no line, item or span. Like a no-break space, they are whitespace and stay in the text.
        L, P = "\u2028", "\u2029"
        box, ticked = '<li class="task"><input type="checkbox" disabled>', '<li class="task"><input type="checkbox" disabled checked>'
        cases = [
            (f"- a{L}b\n- [ ] c{P}d\n-{L}[x]{P}e\n- {L}\n  {L}more", f"<ul><li>a{L}b</li>{box} c{P}d</li>{ticked} e</li><li> more</li></ul>"),
            (f"1.{P}one\n   - [ ]{L}sub", f"<ol><li>one<ul>{box} sub</li></ul></li></ol>"),
            (f"# a{L}b\n#{P}c", f"<h3>a{L}b</h3><h3>c</h3>"),
            (f"*a{L}b* _c{P}d_ **e{L}f** ~~g{P}h~~ *{L}i*", f"<p><em>a{L}b</em> <em>c{P}d</em> <strong>e{L}f</strong> <del>g{P}h</del> *{L}i*</p>"),
            (f"[a{L}b](https://e.com) https://e.com/c{L}d", f"<p>{link('https://e.com', f'a{L}b')} {link('https://e.com/c', 'https://e.com/c')}{L}d</p>"),
            (f">{L}- [x] a\n> b{P}c", f"<blockquote><ul>{ticked} a</li></ul><p>b{P}c</p></blockquote>"),
            (f"| a{L} | b |\n|---|---{P}|\n| 1 | {L} |", table(["a", "b"], ["1", ""])),
            (f"```\n- {L}a\n```\na\n{L}{P}\nb{L}\n---{L}", f"<pre><code>- {L}a</code></pre><p>a</p><p>b{L}</p><hr>"),
        ]
        self.check(cases)
        code = ("const get = () => null, nbsp = (s) => s.replace(/[\\u2028\\u2029]/g, '\\u00a0');\n"
                "process.stdout.write(JSON.stringify(input.map((src) => [markdown(nbsp(src)) === nbsp(markdown(src)),\n"
                "  JSON.stringify(taskItems(nbsp(src))) === nbsp(JSON.stringify(taskItems(src)))])));")
        out = json.loads(run_html_js(self, code, json.dumps([src for src, _ in cases]), [("function inlineMd(", "\n// ----")]))
        self.assertEqual(out, [[True, True]] * len(cases))

    def test_long_texts_with_line_separators_render_quickly(self):
        # 100 KB of each, as one description: [unit, the boxes in one unit].
        cases = [["- \u2028a\n", 0], ["- [ ] a\u2028b\n", 1], ["-\u2029[x]\u2028a\n", 1], ["> \u2028- [ ] a\n", 1], ["# \u2028a\n", 0],
                 ["- a\n  \u2028b\n", 0], [" *a\u2028", 0], ["[a\u2029", 0], ["\u2028\n", 0]]
        code = ("const get = () => null;\n"
                "process.stdout.write(JSON.stringify(input.map(([unit, boxes]) => {\n"
                "  const n = Math.ceil(100000 / unit.length), src = unit.repeat(n), t = process.hrtime.bigint();\n"
                "  const html = markdown(src), items = taskItems(src), ms = Number(process.hrtime.bigint() - t) / 1e6;\n"
                "  return [ms, items.length === boxes * n && (html.match(/<input type=\"checkbox\"/g) || []).length === items.length];\n"
                "})));")
        out = json.loads(run_html_js(self, code, json.dumps(cases), [("function inlineMd(", "\n// ----")]))
        for (unit, _), (ms, found) in zip(cases, out):
            with self.subTest(unit=unit):
                self.assertTrue(found)
                self.assertLess(ms, 1000)

    def test_urls_of_many_megabytes_stay_text(self):
        # From about 8 MB a URL overflows the stack of the regex engine. The page stays up and keeps all the text.
        # [source, its text without the markup, how its HTML starts and ends]
        cases = [["see URL and *b*", "see URL and b", "<p>see ", " and <em>b</em></p>"],
                 ["[a](https://e.com) URL *b*\n\n*c*", "a URL bc", f"<p>{link('https://e.com', 'a')} ", " <em>b</em></p><p><em>c</em></p>"]]
        code = ("const get = () => null, url = 'https://e.com/' + 'a'.repeat(16 << 20);\n"
                "process.stdout.write(JSON.stringify(input.map(([src, text, head, tail]) => {\n"
                "  const html = markdown(src.replace('URL', url));\n"
                "  return html.startsWith(head) && html.endsWith(tail) && html.replace(/<[^>]*>/g, '') === text.replace('URL', url);\n"
                "})));")
        out = json.loads(run_html_js(self, code, json.dumps(cases), [("function inlineMd(", "\n// ----")]))
        self.assertEqual(out, [True, True])

    def render_images(self, srcs, store):
        """markdown() of each source, with `store` as the page's store (None for no store)."""
        code = ("const get = () => null;\n"
                f"var store = {store};\n"
                "process.stdout.write(JSON.stringify(input.map((src) => markdown(src))));")
        return json.loads(run_html_js(self, code, json.dumps(srcs), [("function inlineMd(", "\n// ----")]))

    def test_the_boards_own_images_show_where_the_store_reaches_them(self):
        img = "kanvas-files/0123456789abcdef.png"
        srcs = [f"![Login \"form\"]({img})", f"See ![]({img}) here", "![x](https://example.com/a.png)",
                "![x](kanvas-files/../kanvas.json)", "![x](kanvas-files/0123456789abcdef.svg)", f"[link]({img})"]
        served = self.render_images(srcs, "{ imageUrl: (ref) => ref }")
        self.assertEqual(served, [
            f'<p><img src="{img}" alt="Login &quot;form&quot;" loading="lazy"></p>',
            f'<p>See <img src="{img}" alt="" loading="lazy"> here</p>',
            # Other images stay links, and nothing else counts as one of the board's images.
            f"<p>{link('https://example.com/a.png', 'x')}</p>", "<p>x</p>", "<p>x</p>", "<p>link</p>",
        ])
        # Without a store that reaches the images, as for a board opened from a file, they stay text.
        for store in ("null", "{}"):
            with self.subTest(store=store):
                self.assertEqual(self.render_images(srcs[:2], store), ["<p>Login &quot;form&quot;</p>", f"<p>See {img} here</p>"])


class BoardViewTests(unittest.TestCase):
    """The board view on phones, where one column fills the screen."""

    def run_js(self, code, data):
        return json.loads(run_html_js(self, code, json.dumps(data), [("function columnInView(", "\n// The row of states")]))

    def test_the_row_of_states_marks_the_column_that_fills_most_of_the_screen(self):
        # Six 340px columns 10px apart on a 390px phone, as [left, right] on screen for a scroll position.
        spans = lambda scroll: [[10 + 350 * i - scroll, 350 + 350 * i - scroll] for i in range(6)]
        cases = [
            [spans(0), 0, 390, False],     # at rest on the first column
            [spans(150), 0, 390, False],   # swiped less than halfway
            [spans(190), 0, 390, False],   # past halfway, before the scroll settles
            [spans(1400), 0, 390, False],  # Review, with a sliver of Done
            [spans(1720), 0, 390, True],   # scrolled to the end
            # A phone held sideways: two columns fit, so the first of them is marked, until the end, which marks the last
            [spans(700), 0, 844, False],
            [spans(1266), 0, 844, True],
            # The board view hidden, and a board without columns
            [[[0, 0], [0, 0]], 0, 0, False],
            [[], 0, 390, True],
        ]
        out = self.run_js("process.stdout.write(JSON.stringify(input.map((c) => columnInView(...c))));", cases)
        self.assertEqual(out, [0, 0, 1, 4, 5, 2, 5, -1, -1])

    def test_cards_dropped_by_mouse_or_touch_move_and_are_refused_alike(self):
        """Both drop through dropCard. The page shows `page`, and the save replays the move on `latest`."""
        page = {"kanvas": 1, "title": "", "groups": [], "tasks": [
            task("P-01", "proposed"), task("P-02", "proposed"), task("T-01"), task("D-01", "doing"), task("R-01", "review")]}
        latest = json.loads(json.dumps(page))
        latest["tasks"][1]["desc"] = "Revised by an agent"
        latest["tasks"][2]["state"] = "doing"
        drops = [["P-01", "blocked"], ["P-01", "proposed"], ["D-01", "done"], ["R-01", "todo"], ["T-01", "blocked"],
                 ["T-01", "review"], ["P-02", "todo"], ["X-09", "done"]]
        code = ("let board = normalize(input.page), id = null;\n"
                "const get = (x) => board.tasks.find((t) => same(t.id, x));\n"
                "const out = [];\n"
                "const commit = (fn) => { const b = normalize(clone(input.latest));\n"
                "  try { fn(b); out.push(b.tasks.find((t) => same(t.id, id)).state); } catch (e) { out.push(e.message); } };\n"
                "for (const [x, s] of input.drops) { id = x; const n = out.length; dropCard(x, s); if (out.length === n) out.push(null); }\n"
                "process.stdout.write(JSON.stringify(out));")
        sections = [("function mustFind(", "\n// ----"), ("function dropCard(", "\n// The column that takes up most")]
        out = json.loads(run_html_js(self, code, json.dumps({"page": page, "latest": latest, "drops": drops}), sections))
        self.assertEqual(out, [
            "todo", None, "done", "todo", None,  # Blocked stands for To do, and a card dropped where it is stays
            "T-01 is in Doing now, not To do",  # an agent started it meanwhile
            "P-02 was revised since you opened it; read the new version first",
            None,
        ])


class ReviewFlowTests(unittest.TestCase):
    """Marking the open task done or sending it back in the board view opens the next task in review."""

    FILTERS = [("const get = (id) =>", "\nfunction setBoard("), ("function matches(", "\nfunction render()")]
    STATE = "let board = null, IX = new Map(), COVER = new Map();\nconst ui = { view: 'board', selected: null, search: '', states: new Set(), groups: new Set() };\n"

    @staticmethod
    def board(*tasks):
        return {"kanvas": 1, "title": "", "groups": [{"id": "BE", "name": "Backend"}, {"id": "FE", "name": "Frontend"}], "tasks": list(tasks)}

    def test_the_next_task_follows_the_board_view_order_and_filters(self):
        board = self.board(
            task("FE-01", "review", "FE"), task("BE-01", "review", "BE"), task("BE-02", "done", "BE"),
            task("FE-02", "review", "FE", title="Forecast chart"), task("BE-03", "review", "BE"), task("X-01", "review", "XX"),
            task("G-01", "review"), task("BE-04", "review", "BE", notes=[{"at": "", "by": "claude", "text": "Reads the forecast cache"}]),
            task("FE-03", "doing", "FE"))
        # The task reviewed, the filters, and what else changed by the time the change was saved
        cases = [
            ["BE-01", {}, {}],                           # the done BE-02 is skipped
            ["BE-04", {}, {}],                           # on to the next group
            ["FE-02", {}, {}],                           # an unknown group comes after the groups, with the ungrouped tasks
            ["X-01", {}, {}],
            ["G-01", {}, {}],                            # round to the first
            ["FE-02", {"groups": ["FE"]}, {}],           # round, within the group
            ["FE-02", {"groups": ["FE"]}, {"FE-01": "done"}],
            ["BE-03", {"groups": ["FE"]}, {}],           # opened from elsewhere: its place in the order still counts
            ["X-01", {"groups": [""]}, {}],              # No group holds the tasks of unknown groups
            ["G-01", {"groups": [""]}, {}],
            ["FE-02", {"search": "forecast"}, {}],       # BE-04 matches by its note
            ["BE-04", {"search": "FRONTEND"}, {}],       # and groups by their name
            ["BE-01", {"states": ["review"]}, {}],       # the reviewed task no longer shows, and keeps its place
            ["BE-01", {"states": ["doing"]}, {}],        # no Review column
            ["BE-01", {}, {"BE-03": "done"}],            # moved on by YOLO meanwhile
            ["FE-02", {}, {"FE-03": "review"}],          # finished by an agent meanwhile
            ["BE-01", {}, {"BE-03": "done", "BE-04": "done", "FE-01": "done", "FE-02": "done", "X-01": "done", "G-01": "done"}],
            ["ZZ-09", {}, {}],                           # deleted meanwhile: from the top
        ]
        code = self.STATE + (
            "const out = [];\n"
            "for (const [id, f, changes] of input.cases) {\n"
            "  board = normalize(clone(input.board));\n"
            "  for (const t of board.tasks) if (same(t.id, id)) t.state = 'done';\n"
            "  for (const [x, s] of Object.entries(changes)) board.tasks.find((t) => t.id === x).state = s;\n"
            "  IX = indexOf(board);\n"
            "  Object.assign(ui, { groups: new Set(f.groups || []), states: new Set(f.states || []), search: f.search || '' });\n"
            "  out.push(nextReview(board, id, matches)?.id ?? null);\n"
            "}\n"
            "process.stdout.write(JSON.stringify({ order: boardOrder(normalize(input.board)).map((t) => t.id), next: out }));")
        out = json.loads(run_html_js(self, code, json.dumps({"board": board, "cases": cases}), self.FILTERS))
        self.assertEqual(out["order"], ["BE-01", "BE-02", "BE-03", "BE-04", "FE-01", "FE-02", "FE-03", "X-01", "G-01"])
        self.assertEqual(out["next"], [
            "BE-03", "FE-01", "X-01", "G-01", "BE-01",
            "FE-01", None, "FE-01",
            "G-01", "X-01",
            "BE-04", "FE-01",
            "BE-03", None,
            "BE-04", "FE-03", None, "BE-01",
        ])

    def test_moving_on_waits_for_the_save_and_reads_the_board_it_wrote(self):
        board = self.board(task("FE-01", "review", "FE"), task("BE-01", "done", "BE"), task("FE-02", "review", "FE"),
                           task("BE-03", "review", "BE"), task("FE-03", "doing", "FE"), task("BE-04", "review", "BE"), task("G-01"))
        lost = "Kanvas stopped running or cannot be reached"
        cases = [
            {"id": "BE-03", "undo": True},
            {"id": "BE-03", "undo": "FE-02"},  # Undo leaves the panel alone once the human opened another task
            {"id": "BE-03", "agent": {"BE-04": "done", "FE-03": "review"}},
            {"id": "BE-03", "fail": "server answered 500"},
            {"id": "BE-03", "lost": lost},
            {"id": "BE-03", "view": "graph"},
            {"id": "BE-03", "open": "FE-01"},  # from the card menu, with another task open
            {"id": "BE-03", "open": None},     # with no task open
            {"id": "BE-03", "state": "todo", "note": "Sent back: Cap the retries"},
            {"id": "BE-03", "agent": {"BE-04": "done", "FE-01": "done", "FE-02": "done"}},
            {"id": "FE-02", "groups": ["FE"], "agent": {"FE-01": "done"}},
            {"id": "FE-02", "groups": ["FE"], "agent": {"FE-01": "done", "BE-04": "done"}},
            {"id": "BE-03", "during": {"selected": "FE-02"}},
            {"id": "BE-03", "during": {"editingDesc": True}},
        ]
        code = self.STATE + (
            "let confirmed, store, lost = null, closingBoard = false, pending = 0, writes = 0, saveQueue = Promise.resolve();\n"
            "const edits = { undo: [], redo: [] }, toasts = [];\n"
            "const setBoard = (b) => { board = b; IX = indexOf(b); }, render = () => {}, setSync = () => {};\n"
            "const remember = (before, after) => { const e = { before, after }; edits.undo.push(e); return e; };\n"
            "const forget = (e) => { edits.undo = edits.undo.filter((x) => x !== e); };\n"
            "const select = (id) => { ui.selected = id; }, shownCard = () => null;\n"
            "globalThis.document = { activeElement: null, createTextNode: (data) => ({ data }), querySelector: () => ({ contains: () => false }) };\n"
            "h = (tag, props, ...kids) => ({ kids });\n"
            "toast = (msg) => { const t = { msg, remove() { t.gone = true; } }; toasts.push(t); return t; };\n"
            "const text = (t) => (typeof t.msg === 'string' ? t.msg : t.msg[0].kids[0].data) + (t.gone ? ' (taken away)' : '');\n"
            "(async () => {\n"
            "  const out = [];\n"
            "  for (const c of input.cases) {\n"
            "    board = normalize(clone(input.board)); confirmed = clone(board); IX = indexOf(board);\n"
            "    edits.undo = []; toasts.length = 0; lost = c.lost || null;\n"
            "    Object.assign(ui, { view: c.view || 'board', selected: 'open' in c ? c.open : c.id, editingDesc: false, editingNote: null, groups: new Set(c.groups || []) });\n"
            "    const latest = normalize(clone(input.board));\n"
            "    for (const [x, s] of Object.entries(c.agent || {})) latest.tasks.find((t) => t.id === x).state = s;\n"
            "    store = { load: async () => clone(confirmed), save: async (fn) => {\n"
            "      await new Promise((r) => setTimeout(r, 5));\n"
            "      Object.assign(ui, c.during || {});\n"
            "      if (c.fail) throw new Error(c.fail);\n"
            "      const b = clone(latest); fn(b); return { board: b, base: latest };\n"
            "    } };\n"
            "    await review(get(c.id), c.state || 'done', c.note || '');\n"
            "    await saveQueue;\n"
            "    const t = get(c.id), r = { open: ui.selected, toasts: toasts.map(text), saved: [t.state, t.notes.map((n) => n.text)] };\n"
            "    if (c.undo) { if (c.undo !== true) ui.selected = c.undo; const e = edits.undo[edits.undo.length - 1]; if (e.onUndo) e.onUndo(); r.afterUndo = ui.selected; }\n"
            "    out.push(r);\n"
            "  }\n"
            "  process.stdout.write(JSON.stringify(out));\n"
            "})();")
        sections = [("async function commit(", "async function poll("), ("function undoable(", "\nfunction renderUndo("),
                    ("const MOVE_WORDS =", "\n// What a change did"), ("function mustFind(", "\n// ----"),
                    ("async function review(", "\nasync function askChanges(")] + self.FILTERS
        out = json.loads(run_html_js(self, code, json.dumps({"board": board, "cases": cases}), sections))
        done, kept = ["done", []], ["review", []]
        self.assertEqual(out, [
            {"open": "BE-04", "toasts": ["Marked BE-03 done. Next to review: BE-04."], "saved": done, "afterUndo": "BE-03"},
            {"open": "BE-04", "toasts": ["Marked BE-03 done. Next to review: BE-04."], "saved": done, "afterUndo": "FE-02"},
            # The next task comes from the board as saved, with the agents' changes
            {"open": "FE-01", "toasts": ["Marked BE-03 done. Next to review: FE-01."], "saved": done},
            # A failed save takes the toast away, and one refused at once never shows it
            {"open": "BE-03", "toasts": ["Marked BE-03 done (taken away)", "Not saved: server answered 500"], "saved": kept},
            {"open": "BE-03", "toasts": [f"Not saved: {lost}"], "saved": kept},
            {"open": "BE-03", "toasts": ["Marked BE-03 done"], "saved": done},
            {"open": "FE-01", "toasts": ["Marked BE-03 done"], "saved": done},
            {"open": None, "toasts": ["Marked BE-03 done"], "saved": done},
            {"open": "BE-04", "toasts": ["Sent BE-03 back. Next to review: BE-04."], "saved": ["todo", ["Sent back: Cap the retries"]]},
            {"open": "BE-03", "toasts": ["Marked BE-03 done. No more tasks to review."], "saved": done},
            {"open": "FE-02", "toasts": ["Marked FE-02 done. No more tasks to review in this view; 2 more are filtered out."], "saved": done},
            {"open": "FE-02", "toasts": ["Marked FE-02 done. No more tasks to review in this view; 1 more is filtered out."], "saved": done},
            # The human went on meanwhile: to another task, or to editing this one
            {"open": "FE-02", "toasts": ["Marked BE-03 done"], "saved": done},
            {"open": "BE-03", "toasts": ["Marked BE-03 done"], "saved": done},
        ])


class PanelPlaceTests(unittest.TestCase):
    """Every way of opening a task goes through select(), and the panel shows each task from its top."""

    def test_another_task_opens_at_the_top_and_the_same_one_keeps_the_readers_place(self):
        board = {"kanvas": 1, "title": "", "groups": [], "tasks": [task("A-01", "doing"), task("B-01", "review")]}
        # The panel is 300px tall, and each step reports the task on show and its scroll position, or None once closed
        steps = [
            ["open", "A-01"],                         # the panel opens at the top
            ["scroll", 900], ["render"],              # an agent's change elsewhere, or a save, keeps the place
            ["tall", 2600], ["render"],               # a note added below
            ["tall", 1000], ["render"],               # a shorter task keeps what is left of the place
            ["open", "B-01"],                         # another task starts at the top
            ["scroll", 500], ["type"], ["render"],    # no rebuild while a text field has focus
            ["leave"], ["render"],                    # and none moves it once the field lets go
            ["type"], ["open", "A-01"],               # a link or the address bar opens another task while typing
            ["scroll", 400], ["open", "A-01"],        # its own card, or a link to itself
            ["close"], ["open", "A-01"],              # opened again
            ["scroll", 300], ["close"], ["open", "B-01"],
        ]
        code = (
            "const board = normalize(input.board), get = (id) => board.tasks.find((t) => same(t.id, id));\n"
            "const ui = { selected: null, editingDesc: false, editingNote: null, panelStale: false };\n"
            "const drafts = { title: null, desc: null, descBase: null, noteEdit: null, back: new Map() };\n"
            "const phone = { matches: false }, history = { state: null, pushState() {}, replaceState() {}, back() {} }, location = { pathname: '/', search: '' };\n"
            "const tall = { 'A-01': 2000, 'B-01': 1200 }, body = { matches: () => false };\n"
            "const field = { matches: () => true, blur() { document.activeElement = body; } };\n"
            "const panel = { hidden: true, dataset: {}, height: 0, offset: 0, querySelectorAll: () => [], contains: (el) => el === panel || el === field,\n"
            "  focus() { document.activeElement = panel; }, get scrollTop() { return this.offset; }, set scrollTop(v) { this.offset = Math.max(0, Math.min(v, this.height - 300)); } };\n"
            "globalThis.document = { activeElement: body, querySelector: () => panel };\n"
            "const showPanel = (p, open) => { p.hidden = !open; }, render = () => renderPanel(), reveal = () => {};\n"
            # The browser keeps the offset through a rebuild as far as the new content allows, and shifts it as the
            # title grows to a second line.
            "const fillPanel = (p, t) => { showPanel(p, true); p.height = tall[t.id]; p.scrollTop += 24; };\n"
            "const out = [];\n"
            "for (const [what, arg] of input.steps) {\n"
            "  if (what === 'open') select(arg);\n"
            "  if (what === 'close') select(null);\n"
            "  if (what === 'render') renderPanel();\n"
            "  if (what === 'scroll') { panel.scrollTop = arg; continue; }\n"
            "  if (what === 'tall') { tall[ui.selected] = arg; continue; }\n"
            "  if (what === 'type') { document.activeElement = field; continue; }\n"
            "  if (what === 'leave') { document.activeElement = body; continue; }\n"
            "  out.push([ui.selected, panel.hidden ? null : panel.scrollTop]);\n"
            "}\n"
            "process.stdout.write(JSON.stringify(out));")
        sections = [("function select(", "\n// A note that failed to save"), ("const tabbables =", "\nfunction fillPanel(")]
        out = json.loads(run_html_js(self, code, json.dumps({"board": board, "steps": steps}), sections))
        self.assertEqual(out, [
            ["A-01", 0],
            ["A-01", 900], ["A-01", 900], ["A-01", 700],
            ["B-01", 0],
            ["B-01", 500], ["B-01", 500],
            ["A-01", 0],
            ["A-01", 400],
            [None, None], ["A-01", 0],
            [None, None], ["B-01", 0],
        ])


class ShortcutLabelTests(unittest.TestCase):
    def test_apple_keyboards_show_command_where_the_others_show_ctrl(self):
        code = ("process.stdout.write(JSON.stringify(input.map((platform) => {\n"
                "  Object.defineProperty(globalThis, 'navigator', { value: { platform }, configurable: true });\n"
                "  return [keyLabel('Ctrl+Shift+Z / Ctrl+Y'), keyLabel('Deleted 2 tasks. Ctrl+Z brings them back.')];\n"
                "})));")
        out = json.loads(run_html_js(self, code, json.dumps(["MacIntel", "iPad", "Win32", "Linux x86_64", ""])))
        mac = ["⌘+Shift+Z / ⌘+Y", "Deleted 2 tasks. ⌘+Z brings them back."]
        other = ["Ctrl+Shift+Z / Ctrl+Y", "Deleted 2 tasks. Ctrl+Z brings them back."]
        self.assertEqual(out, [mac, mac, other, other, other])

    def test_the_shortcuts_dialog_lists_every_shortcut_once_in_four_groups(self):
        code = "process.stdout.write(JSON.stringify(SHORTCUTS.map(([head, keys]) => [head, keys.map(([k]) => k)])));"
        groups = json.loads(run_html_js(self, code, "null", [("const SHORTCUTS =", "\nfunction helpDialog")]))
        self.assertEqual([head for head, _ in groups], ["Moving around", "The selected task", "Views", "Editing"])
        keys = [k for _, ks in groups for k in ks]
        self.assertEqual(len(keys), 24)
        self.assertEqual(len(set(keys)), len(keys))
        self.assertIn("Enter", groups[0][1])
        self.assertIn("Ctrl+Z", groups[3][1])


class CardLabelTests(unittest.TestCase):
    def test_a_card_is_named_by_its_id_title_state_and_who_its_foot_shows(self):
        with open(os.path.join(ROOT, "examples", "sample.json"), encoding="utf-8") as f:
            b = json.load(f)
        next(t for t in b["tasks"] if t["id"] == "FE-02")["notes"].append({"by": "codex", "at": "2026-10-20T10:00:00Z", "text": "Question: metric or imperial?"})
        code = ("const b = normalize(input), ix = indexOf(b);\n"
                "process.stdout.write(JSON.stringify(Object.fromEntries(b.tasks.map((t) => [t.id, cardLabel(t, vstateIn(ix, t), unmetIn(ix, t))]))));")
        names = json.loads(run_html_js(self, code, json.dumps(b), [("function cardLabel(", "\nfunction cardFoot(")]))
        self.assertEqual(names["BE-02"], "BE-02 Fetch and cache forecasts, Review, claude")
        self.assertEqual(names["BE-03"], "BE-03 City search endpoint, Doing, codex")
        self.assertEqual(names["FE-02"], "FE-02 Forecast cards, Waiting for you, codex")
        self.assertEqual(names["FE-03"], "FE-03 City search box, Blocked, waiting on FE-02, BE-03")
        self.assertEqual(names["FE-05"], "FE-05 Offline banner, Proposed, codex")
        self.assertEqual(names["FE-04"], "FE-04 Dark mode, To do")
        self.assertEqual(names["RS-01"], "RS-01 Compare weather APIs, Done")


class WorkflowTests(TempBoard):
    def test_status_lists_what_matters(self):
        _, out, _ = self.run_cli("status")
        self.assertIn("Ready to start (1)", out)
        self.assertIn("DV-03  Task DV-03  (waiting on DV-02)", out)
        self.assertIn("Proposed, awaiting human approval (1)", out)
        _, out, _ = self.run_cli("ready")
        self.assertEqual(out.split(), ["DV-02", "Task", "DV-02"])

    def test_status_names_an_untitled_board_as_the_boards_page_does(self):
        board = self.board()
        board["title"] = ""
        write_board(self.path, board)
        _, out, _ = self.run_cli("status")
        self.assertEqual(out.splitlines()[0], f"{kanvas.board_name(self.path)}  (kanvas.json)")

    def test_agents_cannot_start_blocked_proposed_or_running_tasks(self):
        self.assertIn("blocked by unfinished DV-02", self.fails("start", "DV-03"))
        self.assertIn("a human must approve it", self.fails("start", "DV-04"))
        self.run_cli("start", "DV-02")
        self.assertEqual(self.get("DV-02")["state"], "doing")
        self.assertIn("already in progress", self.fails("start", "DV-02"))

    def test_finish_requires_a_summary(self):
        self.run_cli("start", "DV-02")
        self.assertIn("needs a summary", self.fails("finish", "DV-02"))
        self.assertEqual(self.get("DV-02")["state"], "doing")
        self.run_cli("finish", "DV-02", "--as", "claude", "-m", "Parser done; 12 tests pass")
        t = self.get("DV-02")
        self.assertEqual(t["state"], "review")
        self.assertEqual((t["notes"][-1]["by"], t["notes"][-1]["text"]), ("claude", "Parser done; 12 tests pass"))
        self.assertIn("only tasks in progress", self.fails("finish", "DV-02", "-m", "again"))

    def test_pause_returns_the_task_with_a_note(self):
        self.run_cli("start", "DV-02", "-m", "Plan: write the parser first")
        self.run_cli("pause", "DV-02", "-m", "Waiting for an API key")
        t = self.get("DV-02")
        self.assertEqual(t["state"], "todo")
        self.assertEqual([n["text"] for n in t["notes"]], ["Plan: write the parser first", "Waiting for an API key"])

    def test_author_comes_from_as_or_environment(self):
        self.run_cli("note", "DV-02", "from env", env={"KANVAS_AGENT": "codex"})
        self.run_cli("note", "DV-02", "--as", "gemini", "after an option")
        self.run_cli("note", "DV-02", "plain")
        self.assertEqual([n["by"] for n in self.get("DV-02")["notes"]], ["codex", "gemini", "agent"])

    def test_proposals_are_signed_like_notes(self):
        self.run_cli("propose", "Development", "Signed", env={"KANVAS_AGENT": "codex"})
        self.run_cli("batch", "--as", "gemini", stdin=json.dumps({"tasks": [{"title": "From a batch"}]}).encode())
        signed = {t["title"]: t for t in self.board()["tasks"] if t["title"] in ("Signed", "From a batch")}
        self.assertEqual(signed["Signed"]["by"], "codex")
        self.assertEqual(signed["From a batch"]["by"], "gemini")
        self.assertRegex(signed["Signed"]["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.run_cli("edit", signed["Signed"]["id"], "--title", "Signed and revised", "--as", "claude")
        self.assertEqual(self.get(signed["Signed"]["id"])["by"], "codex")
        self.assertIn("By:     codex", self.run_cli("show", signed["Signed"]["id"])[1])

    def test_utf8_from_stdin(self):
        text = "Café — naïve ✓ 日本語\nsecond line: ü"
        self.run_cli("note", "DV-02", "--stdin", stdin=text.encode("utf-8"))
        self.run_cli("propose", "Development", "Übersicht ✓", "--stdin", stdin=text.encode("utf-8"))
        self.assertEqual(self.get("DV-02")["notes"][0]["text"], text)
        self.assertEqual(self.get("DV-05")["title"], "Übersicht ✓")
        self.assertEqual(self.get("DV-05")["desc"], text)
        _, out, _ = self.run_cli("show", "DV-05")
        self.assertIn("Café — naïve ✓ 日本語", out)

    def test_show_marks_edited_notes(self):
        board = kanvas.parse_board(self.raw())
        board["tasks"][1]["notes"] = [{"at": "2026-10-19T08:00:00Z", "by": "claude", "edited": "2026-10-21T10:16:00Z", "text": "Parser done"}]
        write_board(self.path, board)
        _, out, _ = self.run_cli("show", board["tasks"][1]["id"])
        self.assertIn("2026-10-19 08:00:00 UTC  claude, edited 2026-10-21 10:16:00 UTC:", out)

    def test_multiline_text_from_a_file(self):
        notes = os.path.join(self.dir, "summary.md")
        with open(notes, "wb") as f:
            f.write("\ufeffDone:\r\n- parser\r\n- tests\r\n".encode("utf-8"))
        self.run_cli("start", "DV-02")
        self.run_cli("finish", "DV-02", "--file", notes)
        self.assertEqual(self.get("DV-02")["notes"][-1]["text"], "Done:\n- parser\n- tests")

    def test_start_reads_the_plan_from_stdin_or_a_file(self):
        plan = "Plan ✓:\n- parse\n- test"
        self.run_cli("start", "DV-02", "--stdin", stdin=plan.encode("utf-8"))
        self.assertEqual(self.get("DV-02")["notes"][-1]["text"], plan)
        self.run_cli("pause", "DV-02")
        path = os.path.join(self.dir, "plan.md")
        with open(path, "wb") as f:
            f.write("\ufeffPlan:\r\n- again\r\n".encode("utf-8"))
        self.run_cli("start", "DV-02", "--file", path)
        self.assertEqual(self.get("DV-02")["notes"][-1]["text"], "Plan:\n- again")
        self.run_cli("pause", "DV-02")
        before = self.raw()
        self.assertIn("only once", self.fails("start", "DV-02", "-m", "inline", "--stdin", stdin=b"piped"))
        self.assertEqual(self.raw(), before)

    def test_text_may_be_given_only_once(self):
        self.assertIn("only once", self.fails("note", "DV-02", "inline", "--stdin", stdin=b"piped"))

    def test_done_tasks_are_frozen_for_agents(self):
        self.assertIn("propose a follow-up", self.fails("note", "DV-01", "late"))
        self.assertIn("dependencies can no longer change", self.fails("depend", "DV-01", "DV-04"))

    def test_depend_rejects_cycles_and_self(self):
        self.run_cli("depend", "DV-04", "DV-03")
        self.assertEqual(self.get("DV-04")["deps"], ["DV-03"])
        self.assertIn("circular", self.fails("depend", "DV-02", "DV-04"))
        self.assertIn("circular", self.fails("depend", "DV-04", "DV-04"))
        _, out, _ = self.run_cli("depend", "DV-04", "dv-03")
        self.assertIn("already depends", out)

    def test_failed_commands_leave_the_file_untouched(self):
        before = self.raw()
        self.fails("start", "DV-03")
        self.fails("propose", "Nope", "Title")
        self.fails("depend", "DV-02", "DV-03")
        self.assertEqual(self.raw(), before)

    def test_board_is_found_from_a_subdirectory(self):
        sub = os.path.join(self.dir, "src", "deep")
        os.makedirs(sub)
        _, out, _ = self.run_cli("ready", cwd=sub)
        self.assertIn("DV-02", out)

    def test_check_exit_code(self):
        _, out, _ = self.run_cli("check")
        self.assertTrue(out.startswith("OK: 4 tasks"))
        board = self.board()
        board["tasks"].append(task("DV-05", deps=["DV-77"]))
        write_board(self.path, board)
        self.assertNotEqual(self.run_cli("check", check=False)[0], 0)

    def test_commands_refuse_fields_of_the_wrong_type(self):
        for bad, message in WRONG_TYPES:
            write_board(self.path, dict(kanvas.new_board(), tasks=[bad]))
            for args in (["status"], ["show", "G-01"], ["check"]):
                with self.subTest(args=args, message=message):
                    code, out, err = self.run_cli(*args, check=False)
                    self.assertEqual(code, 1, out)
                    self.assertRegex(err, r"\AError: [^\n]+\n\Z")
                    self.assertIn(message, err)


class QuestionTests(TempBoard):
    def test_ask_shows_the_wait_until_the_next_note(self):
        self.run_cli("start", "DV-02", "--as", "claude")
        _, out, _ = self.run_cli("ask", "DV-02", "--as", "claude", "Question: SQLite or Postgres?")
        self.assertIn("DV-02 now shows the human that you wait for their reply", out)
        last = self.get("DV-02")["notes"][-1]
        self.assertEqual((last["by"], last["text"]), ("claude", "Question: SQLite or Postgres?"))
        self.assertIn("DV-02  Task DV-02  (waiting for the human: SQLite or Postgres?)", self.run_cli("status")[1])
        self.assertIn("Waits:  for the human to reply to the question claude asked", self.run_cli("show", "DV-02")[1])
        _, out, _ = self.run_cli("note", "DV-02", "--as", "claude", "SQLite, the human said")
        self.assertIn("DV-02 no longer waits for the human.", out)
        self.assertIsNone(kanvas.open_question(self.get("DV-02")))
        self.assertNotIn("waiting for the human", self.run_cli("status")[1])
        self.assertNotIn("no longer waits", self.run_cli("note", "DV-02", "Another note")[1])

    def test_ask_needs_a_question_on_a_task_in_progress(self):
        self.assertIn("DV-02 is todo; ask on a task you are working on", self.fails("ask", "DV-02", "Which one?"))
        self.assertIn("DV-03 is blocked", self.fails("ask", "DV-03", "Which one?"))
        self.run_cli("start", "DV-02")
        before = self.raw()
        self.assertIn("The question is empty", self.fails("ask", "DV-02", "Question:"))
        self.assertEqual(self.raw(), before)

    def test_leaving_doing_ends_the_wait(self):
        self.run_cli("start", "DV-02")
        self.run_cli("ask", "DV-02", "Which one?")
        t = self.get("DV-02")
        self.assertEqual(kanvas.open_question(t)["text"], "Question: Which one?")
        t["state"] = "review"
        self.assertIsNone(kanvas.open_question(t))
        self.run_cli("finish", "DV-02", "-m", "Done without an answer")
        self.assertIsNone(kanvas.open_question(self.get("DV-02")))

    def test_html_open_question_matches_python(self):
        q = {"at": "2026-10-26T09:00:00Z", "by": "claude", "text": "Question: Which one?"}
        later = {"at": "2026-10-26T09:05:00Z", "by": "claude", "text": "The human chose the first"}
        board = kanvas.new_board()
        board["tasks"] = [task("Q-01", "doing", notes=[q]), task("Q-02", "doing", notes=[q, later]),
                          task("Q-03", "review", notes=[q]), task("Q-04", "doing", notes=[dict(q, by="human")]),
                          task("Q-05", "doing", notes=[dict(q, text="Questions: none")]), task("Q-06", "doing"),
                          task("Q-07", "doing", notes=[later, q])]
        out = run_html_js(self, "process.stdout.write(JSON.stringify(normalize(input).tasks.map((t) => !!openQuestion(t))));", json.dumps(board))
        expected = [kanvas.open_question(t) is not None for t in board["tasks"]]
        self.assertEqual(json.loads(out), expected)
        self.assertEqual(expected, [True, False, False, False, False, False, True])


class ChangeRequestTests(TempBoard):
    REQUEST = {"at": "2026-10-26T09:00:00Z", "by": "human", "text": "Changes requested: Split it in two"}
    HUMAN = {"at": "2026-10-26T09:02:00Z", "by": "human", "text": "And keep the cache out of it"}
    AGENT = {"at": "2026-10-26T09:05:00Z", "by": "codex", "text": "Split as asked"}

    def request(self, *notes):
        board = self.board()
        kanvas.find_task(board, "DV-04")["notes"] = [self.REQUEST, *notes]
        write_board(self.path, board)

    def test_status_and_show_point_at_the_request_until_an_agent_answers(self):
        self.request()
        self.assertIn("DV-04  Task DV-04  (changes requested: Split it in two)", self.run_cli("status")[1])
        self.assertIn("Revise: the human asked for changes", self.run_cli("show", "DV-04")[1])
        _, out, _ = self.run_cli("edit", "DV-04", "--title", "The endpoint", "--as", "codex")
        self.assertIn("The human's request for changes on DV-04 stays open until you say what you changed", out)
        self.assertIn("(changes requested: Split it in two)", self.run_cli("status")[1])
        _, out, _ = self.run_cli("note", "DV-04", "--as", "codex", "Split: the caching is now DV-05")
        self.assertIn("This note answers the human's request for changes on DV-04.", out)
        self.assertIsNone(kanvas.change_request(self.get("DV-04")))
        self.assertNotIn("changes requested", self.run_cli("status")[1])
        self.assertNotIn("Revise:", self.run_cli("show", "DV-04")[1])
        self.assertNotIn("request for changes", self.run_cli("note", "DV-04", "Another note")[1])

    def test_the_human_may_add_to_the_request_and_edit_with_a_note_answers_it(self):
        self.request(self.HUMAN)
        self.assertIn("(changes requested: Split it in two)", self.run_cli("status")[1])
        _, out, _ = self.run_cli("edit", "DV-04", "--desc", "Only the endpoint", "-m", "Split as asked", "--as", "codex")
        self.assertIn("This answers the human's request for changes on DV-04.", out)
        self.assertIsNone(kanvas.change_request(self.get("DV-04")))

    def test_a_proposal_under_yolo_is_not_sent_back_for_revision(self):
        self.request()
        board = self.board()
        kanvas.find_task(board, "DV-04")["yolo"] = True
        write_board(self.path, board)
        self.assertNotIn("changes requested", self.run_cli("status")[1])
        self.assertNotIn("Revise:", self.run_cli("show", "DV-04")[1])

    def test_html_change_request_matches_python(self):
        r, human, agent = self.REQUEST, self.HUMAN, self.AGENT
        unsigned = {k: v for k, v in agent.items() if k != "by"}
        board = kanvas.new_board()
        board["tasks"] = [task("C-01", "proposed", notes=[r]), task("C-02", "proposed", notes=[r, human]),
                          task("C-03", "proposed", notes=[r, agent]), task("C-04", "todo", notes=[r]),
                          task("C-05", "proposed", notes=[dict(r, by="codex")]), task("C-06", "proposed", notes=[dict(r, text="Changes requested")]),
                          task("C-07", "proposed"), task("C-08", "proposed", notes=[agent, r]),
                          task("C-09", "proposed", notes=[r, agent, human]), task("C-10", "proposed", notes=[r, unsigned])]
        out = run_html_js(self, "process.stdout.write(JSON.stringify(normalize(input).tasks.map((t) => !!changeRequest(t))));", json.dumps(board))
        expected = [kanvas.change_request(t) is not None for t in board["tasks"]]
        self.assertEqual(json.loads(out), expected)
        self.assertEqual(expected, [True, True, False, False, False, False, False, True, False, False])


class YoloTests(TempBoard):
    def initial_board(self):
        board = super().initial_board()
        board["tasks"][2]["yolo"] = True  # DV-03, which depends on DV-02
        board["tasks"].append(task("DV-05", "todo", "DV", ["DV-01"]))
        return board

    def test_yolo_is_stored_after_deps(self):
        t = json.loads(self.raw())["tasks"][2]
        self.assertEqual(list(t), ["id", "title", "state", "group", "deps", "yolo"])

    def test_covered_tasks_skip_review_and_the_yolo_task_does_not(self):
        _, out, _ = self.run_cli("ready")
        self.assertEqual(out.splitlines(), ["DV-02  Task DV-02  (YOLO)", "DV-05  Task DV-05"])
        self.run_cli("start", "DV-02")
        _, out, _ = self.run_cli("finish", "DV-02", "-m", "Parser done")
        self.assertIn("doing -> done", out)
        self.assertEqual(self.get("DV-02")["state"], "done")
        self.run_cli("start", "DV-03")
        _, out, _ = self.run_cli("finish", "DV-03", "-m", "Feature done")
        self.assertIn("doing -> review", out)
        self.assertIn("YOLO is set on it", out)
        self.assertEqual(self.get("DV-03")["state"], "review")
        self.run_cli("start", "DV-05")
        self.run_cli("finish", "DV-05", "-m", "Outside the chain")
        self.assertEqual(self.get("DV-05")["state"], "review")

    def test_only_the_last_yolo_task_of_a_chain_goes_to_review(self):
        board = self.board()
        kanvas.find_task(board, "DV-02")["yolo"] = True
        board["tasks"].append(board["tasks"].pop(1))  # DV-03's YOLO comes first in the file
        write_board(self.path, board)
        _, out, _ = self.run_cli("status")
        self.assertIn("YOLO on DV-03:", out)
        _, out, _ = self.run_cli("show", "DV-02")
        self.assertIn("YOLO:   through DV-03", out)
        self.run_cli("start", "DV-02")
        _, out, _ = self.run_cli("finish", "DV-02", "-m", "Parser done")
        self.assertIn("YOLO covers it through DV-03", out)
        self.assertEqual(self.get("DV-02")["state"], "done")
        self.run_cli("start", "DV-03")
        self.run_cli("finish", "DV-03", "-m", "Feature done")
        self.assertEqual(self.get("DV-03")["state"], "review")

    def test_html_yolo_cover_matches_python(self):
        board = kanvas.new_board()
        board["tasks"] = [task("X-03", deps=["X-02"], yolo=True), task("X-01"), task("X-02", deps=["X-01"], yolo=True),
                          task("X-04", deps=["X-01"], yolo=True), task("X-05", deps=["X-03", "X-04"]),
                          task("X-06"), task("X-07", "review", deps=["X-06"], yolo=True), task("X-08", "done", deps=["X-06"], yolo=True)]
        out = run_html_js(self, "process.stdout.write(JSON.stringify(Object.fromEntries(yoloCoverIn(normalize(input)))));", json.dumps(board))
        cover = kanvas.yolo_cover(board)
        self.assertEqual(json.loads(out), cover)
        self.assertEqual(cover, {"X-03": "X-03", "X-02": "X-03", "X-01": "X-03", "X-04": "X-04"})

    def test_html_yolo_takes_over_the_chain(self):
        board = kanvas.new_board()
        board["tasks"] = [task("Y-01", "review", yolo=True), task("Y-02", "proposed", deps=["Y-01"]),
                          task("Y-03", deps=["Y-02"], yolo=True), task("Y-04", "proposed", deps=["Y-03", "Y-05"]),
                          task("Y-05", "done", yolo=True), task("Y-06", "review", yolo=True)]
        out = run_html_js(self, "const b = normalize(input); yoloOn(b, 'y-04');"
                                "process.stdout.write(JSON.stringify(b.tasks.map((t) => [t.id, t.state, !!t.yolo])));", json.dumps(board))
        self.assertEqual(json.loads(out), [["Y-01", "done", False], ["Y-02", "todo", False], ["Y-03", "todo", False],
                                           ["Y-04", "todo", True], ["Y-05", "done", True], ["Y-06", "review", True]])

    def test_yolo_ends_when_its_task_reaches_review(self):
        board = self.board()
        kanvas.find_task(board, "DV-03")["state"] = "review"
        write_board(self.path, board)
        _, out, _ = self.run_cli("status")
        self.assertNotIn("YOLO on", out)
        _, out, _ = self.run_cli("ready")
        self.assertNotIn("(YOLO)", out)
        self.run_cli("start", "DV-02")
        self.run_cli("finish", "DV-02", "-m", "Parser done")
        self.assertEqual(self.get("DV-02")["state"], "review")

    def test_a_proposal_linked_into_the_chain_waits_for_approval(self):
        self.run_cli("propose", "Development", "Delete the production database", "--as", "codex")
        self.assertIn("a human must approve it", self.fails("start", "DV-06"))
        _, out, _ = self.run_cli("depend", "DV-02", "DV-04", "DV-06")
        self.assertIn("DV-02 now depends on DV-04, DV-06 (blocked until they are done)", out)
        self.assertIn("ask the human to approve DV-04, DV-06 in the board", out)
        before = self.raw()
        for tid in ("DV-04", "DV-06"):
            self.assertIn("a human must approve it", self.fails("start", tid))
        self.assertEqual(self.raw(), before)
        out = self.run_cli("status")[1].replace("\r\n", "\n")
        self.assertIn("Proposed, awaiting human approval (2)\n  DV-04  Task DV-04\n  DV-06  Delete the production database\n", out)
        self.assertIn("DV-02  Task DV-02  (waiting on DV-04, DV-06)  (YOLO)", out)
        self.assertIn("YOLO:   joins the chain of DV-03 once a human approves it", self.run_cli("show", "DV-06")[1])
        self.assertNotIn("ask the human", self.run_cli("depend", "DV-03", "DV-04")[1])
        board = self.board()
        kanvas.find_task(board, "DV-06")["state"] = "todo"  # the human approves it in the board
        write_board(self.path, board)
        self.assertIn("DV-06  Delete the production database  (YOLO)", self.run_cli("ready")[1])
        self.run_cli("start", "DV-06")
        self.run_cli("finish", "DV-06", "-m", "Done")
        self.assertEqual(self.get("DV-06")["state"], "done")

    def test_html_a_link_the_human_makes_approves_the_proposals_it_brings_into_the_chain(self):
        board = kanvas.new_board()
        board["tasks"] = [task("Y-01", "proposed"), task("Y-02", deps=["Y-01"]), task("Y-03", deps=["Y-02"], yolo=True),
                          task("Y-04", "proposed", deps=["Y-05"]), task("Y-05", "proposed"), task("Y-06", "proposed"), task("Y-07", "proposed")]
        code = ("const b = normalize(input), ix = indexOf(b), seen = b.tasks.map((t) => vstateIn(ix, t));"
                "ops.addDep('Y-02', 'Y-04')(b); ops.addTask({ title: 'New', deps: ['Y-06'], blocks: ['Y-03'] }, {})(b);"
                "process.stdout.write(JSON.stringify([seen, b.tasks.map((t) => t.state)]));")
        sections = [("function mustFind(", "\n// ----"), ("function inlineMd(", "\n// ----")]
        seen, after = json.loads(run_html_js(self, code, json.dumps(board), sections))
        tasks = kanvas.task_map(board)
        self.assertEqual(seen, [kanvas.view_state(t, tasks) for t in board["tasks"]])
        self.assertEqual(seen, ["proposed", "blocked", "blocked", "proposed", "proposed", "proposed", "proposed"])
        # Y-01 was linked in before (by an agent) and stays proposed; Y-07 is outside the chain.
        self.assertEqual(after, ["proposed", "todo", "todo", "todo", "todo", "todo", "proposed", "todo"])


class ProposalTests(TempBoard):
    def test_group_prefix_with_digits(self):
        self.run_cli("add-group", "Level 0 Contract")
        self.run_cli("propose", "Level 0 Contract", "Define the contract", "Scope and terms")
        t = self.get("L0C-01")
        self.assertEqual((t["state"], t["group"], t["desc"]), ("proposed", "L0C", "Scope and terms"))
        self.assertIn("approve", self.fails("start", "L0C-01"))
        self.run_cli("note", "L0C-01", "ids with digits in the prefix work")
        self.run_cli("depend", "L0C-01", "DV-02")
        self.assertEqual(self.get("l0c-01")["deps"], ["DV-02"])

    def test_propose_without_group_and_with_deps(self):
        self.run_cli("propose", "-", "Loose end", "--deps", "DV-02", "DV-03")
        t = self.get("G-01")
        self.assertEqual((t["group"], t["deps"]), (None, ["DV-02", "DV-03"]))
        self.assertIn("not found", self.fails("propose", "Development", "x", "--deps", "DV-99"))

    def test_batch_resolves_titles_and_creates_groups(self):
        spec = {
            "groups": ["Research", {"name": "Quality", "prefix": "QA"}],
            "tasks": [
                {"title": "Survey users", "group": "Research"},
                {"title": "Write test plan", "group": "QA", "deps": ["Survey users", "DV-02"]},
                {"title": "Automate tests", "group": "Quality", "depends_on": "Write test plan", "description": "CI"},
            ],
        }
        _, out, _ = self.run_cli("batch", stdin=json.dumps(spec).encode("utf-8"))
        self.assertIn("Proposed 3 tasks", out)
        self.assertEqual(self.get("QA-01")["deps"], ["RS-01", "DV-02"])
        self.assertEqual(self.get("QA-02")["deps"], ["QA-01"])
        self.assertEqual(self.get("QA-02")["desc"], "CI")
        self.assertTrue(all(self.get(i)["state"] == "proposed" for i in ("RS-01", "QA-01", "QA-02")))

    def test_batch_is_all_or_nothing(self):
        before = self.raw()
        spec = {"groups": ["Research"], "tasks": [{"title": "ok", "group": "Research"}, {"title": "bad", "deps": ["Nope"]}]}
        self.assertIn("Dependency 'Nope' not found", self.fails("batch", stdin=json.dumps(spec).encode("utf-8")))
        self.assertEqual(self.raw(), before)

    def test_batch_rejects_malformed_input(self):
        before = self.raw()
        ok = {"title": "Fine", "group": "Development"}
        cases = [
            ({"groups": "Backend"}, 'Batch "groups" must be a list'),
            ({"tasks": "nope"}, 'Batch "tasks" must be a list'),
            ({}, "The batch has no groups or tasks"),
            ({"groups": [["Research"]]}, "Group #1 in the batch must be a name or"),
            ({"groups": ["Research", {"prefix": "QA"}]}, 'Group #2 in the batch needs a "name"'),
            ({"groups": [{"name": "Quality", "prefix": 7}]}, "Group 'Quality': \"prefix\" must be a string"),
            ({"tasks": [ok, "Just a title"]}, "Task #2 in the batch must be an object"),
            ({"tasks": [{"title": 5}]}, 'Task #1 in the batch: "title" must be a string'),
            ({"tasks": [ok, {"group": "Development"}]}, "Task #2 in the batch has no title"),
            ({"tasks": [{"title": "Seven", "desc": 7}]}, "Task 'Seven': \"desc\" must be a string"),
            ({"tasks": [{"title": "Listed", "description": ["a"]}]}, "Task 'Listed': \"description\" must be a string"),
            ({"tasks": [{"title": "Object", "group": {"x": 1}}]}, "Task 'Object': \"group\" must be a string"),
            ({"tasks": [ok, {"title": "Missing", "group": "Nope"}]}, "Task 'Missing': Group 'Nope' not found"),
            ({"tasks": [{"title": "Number", "deps": 3}]}, "Task 'Number': \"deps\" must be a task ID or title"),
            ({"tasks": [{"title": "Mixed", "depends_on": ["DV-02", 3]}]}, "Task 'Mixed': \"depends_on\" must be a task ID or title"),
        ]
        for spec, message in cases:
            with self.subTest(spec=spec):
                code, out, err = self.run_cli("batch", stdin=json.dumps(spec).encode("utf-8"), check=False)
                self.assertEqual(code, 1, out)
                self.assertRegex(err, r"\AError: [^\n]+\n\Z")
                self.assertIn(message, err)
                self.assertEqual(self.raw(), before)

    def test_batch_of_one_task_says_task(self):
        _, out, _ = self.run_cli("batch", stdin=json.dumps([{"title": "Only one", "deps": "DV-02", "desc": None}]).encode())
        self.assertIn("Proposed 1 task. A human must approve it before work starts.", out)
        self.assertEqual((self.get("G-01")["deps"], self.get("G-01")["desc"]), (["DV-02"], ""))

    def test_more_than_99_tasks(self):
        spec = [{"title": f"Step {i}", "group": "Development"} for i in range(1, 101)]
        self.run_cli("batch", stdin=json.dumps(spec).encode("utf-8"))
        ids = [t["id"] for t in self.board()["tasks"]]
        self.assertIn("DV-99", ids)
        self.assertIn("DV-104", ids)
        self.assertEqual(len(set(ids)), len(ids))
        self.run_cli("check")


class RevisionTests(TempBoard):
    """Agents may rework proposals until a human approves them."""

    def initial_board(self):
        board = super().initial_board()
        board["groups"].append({"id": "QA", "name": "Quality"})
        board["tasks"] += [task("DV-05", "proposed", "DV", ["DV-04"]), task("DV-06", "proposed", "DV", ["DV-04", "DV-02"])]
        return board

    def test_edit_revises_a_proposal(self):
        _, out, _ = self.run_cli("edit", "DV-04", "--title", "  Better   title ", "--group", "QA",
                                 "--stdin", "-m", "Narrowed after the discussion", "--as", "codex",
                                 stdin="Finished when it works.\r\n".encode("utf-8"))
        self.assertIn("Revised DV-04: title, group and description, with a note", out)
        t = self.get("DV-04")
        self.assertEqual((t["id"], t["title"], t["group"], t["desc"], t["state"]),
                         ("DV-04", "Better title", "QA", "Finished when it works.", "proposed"))
        self.assertEqual((t["notes"][-1]["by"], t["notes"][-1]["text"]), ("codex", "Narrowed after the discussion"))
        self.run_cli("edit", "DV-04", "--desc", "", "--group", "-")
        self.assertEqual((self.get("DV-04")["desc"], self.get("DV-04")["group"]), ("", None))
        _, out, _ = self.run_cli("edit", "DV-04", "--title", "Better title")
        self.assertIn("nothing", out)
        self.run_cli("check")

    def test_edit_refuses_approved_tasks_and_bad_input(self):
        before = self.raw()
        self.assertIn("only a human can change it", self.fails("edit", "DV-02", "--title", "x"))
        self.assertIn("Nothing to change", self.fails("edit", "DV-04"))
        self.assertIn("not found", self.fails("edit", "DV-04", "--group", "Nope"))
        self.assertIn("title is empty", self.fails("edit", "DV-04", "--title", "  "))
        self.assertIn("unrecognized arguments", self.fails("edit", "DV-04", "stray text"))
        self.assertEqual(self.raw(), before)

    def test_withdraw_removes_proposals_and_their_links(self):
        _, out, _ = self.run_cli("withdraw", "DV-04", "dv-05")
        self.assertIn("Withdrew DV-04, DV-05; DV-06 no longer depend on them", out)
        ids = [t["id"] for t in self.board()["tasks"]]
        self.assertNotIn("DV-04", ids)
        self.assertNotIn("DV-05", ids)
        self.assertEqual(self.get("DV-06")["deps"], ["DV-02"])
        self.run_cli("check")

    def test_withdraw_keeps_what_approved_tasks_depend_on(self):
        self.run_cli("depend", "DV-03", "DV-06")
        before = self.raw()
        self.assertIn("DV-03 is approved and depends on DV-06", self.fails("withdraw", "DV-06"))
        self.assertIn("only a human can change it", self.fails("withdraw", "DV-04", "DV-02"))
        self.assertEqual(self.raw(), before)

    def test_undepend_rewires_a_proposal(self):
        _, out, _ = self.run_cli("undepend", "DV-06", "dv-04")
        self.assertIn("DV-06 no longer depends on DV-04", out)
        self.assertEqual(self.get("DV-06")["deps"], ["DV-02"])
        before = self.raw()
        self.assertIn("does not depend on DV-05", self.fails("undepend", "DV-06", "DV-05"))
        self.assertIn("only a human can change it", self.fails("undepend", "DV-03", "DV-02"))
        self.assertEqual(self.raw(), before)

    def test_proposals_in_a_yolo_chain_stay_proposals(self):
        board = self.board()
        dv3 = kanvas.find_task(board, "DV-03")
        dv3["yolo"], dv3["deps"] = True, ["DV-02", "DV-05"]
        write_board(self.path, board)
        self.run_cli("edit", "DV-05", "--title", "Revised in the chain")
        self.run_cli("undepend", "DV-05", "DV-04")
        before = self.raw()
        self.assertIn("DV-03 is approved and depends on DV-05", self.fails("withdraw", "DV-05"))
        self.assertIn("a human must approve it", self.fails("start", "DV-05"))
        self.assertEqual(self.raw(), before)


class InitTests(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="kanvas-init-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def init(self, *args, cwd=None):
        """Runs `init` on self.dir, or with `args` from `cwd` when either is given."""
        if not args and cwd is None:
            args = (self.dir,)
        proc = subprocess.run([sys.executable, SCRIPT, "init", *args], cwd=cwd, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        return proc.stdout.decode("utf-8")

    def folder(self, *parts):
        path = os.path.join(self.dir, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def test_init_refreshes_skill_copies_from_older_versions(self):
        old = os.path.join(self.dir, ".cursor", "skills", "kanvas", "SKILL.md")
        os.makedirs(os.path.dirname(old))
        with open(old, "w", encoding="utf-8") as f:
            f.write("old skill\n")
        self.init()
        with open(old, encoding="utf-8") as f, open(os.path.join(ROOT, "skill", "SKILL.md"), encoding="utf-8") as g:
            self.assertEqual(f.read(), g.read())
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".gemini")))

    def test_init_installs_the_skill_and_never_overwrites(self):
        mine = "# My agent instructions\n"
        for name in ("AGENTS.md", "CLAUDE.md"):
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
                f.write(mine)
        os.makedirs(os.path.join(self.dir, ".cursor"))
        out = self.init()
        self.assertIn("created   kanvas.json", out)
        self.assertNotIn("created   AGENTS.md", out)
        self.assertIn("add this line to AGENTS.md or CLAUDE.md", out)
        for rel in (".agents/skills/kanvas/kanvas.py", ".agents/skills/kanvas/kanvas.html",
                    ".agents/skills/kanvas/SKILL.md", ".claude/skills/kanvas/SKILL.md"):
            self.assertTrue(os.path.isfile(os.path.join(self.dir, rel)), rel)
        self.assertEqual(os.listdir(os.path.join(self.dir, ".cursor")), [])
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".gemini")))
        for name in ("AGENTS.md", "CLAUDE.md"):
            with open(os.path.join(self.dir, name), encoding="utf-8") as f:
                self.assertEqual(f.read(), mine)

        board = os.path.join(self.dir, "kanvas.json")
        data = kanvas.load_board(board)
        data["tasks"].append(task("G-01"))
        write_board(board, data)
        self.assertIn("kept      kanvas.json", self.init())
        self.assertEqual(len(kanvas.load_board(board)["tasks"]), 1)

        installed = os.path.join(self.dir, ".agents", "skills", "kanvas", "kanvas.py")
        proc = subprocess.run([sys.executable, installed, "status"], cwd=self.dir, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())

    def test_init_in_a_git_subfolder_sets_up_the_top_folder(self):
        os.mkdir(os.path.join(self.dir, ".git"))
        sub = self.folder("src", "deep")
        out = self.init(cwd=sub)
        self.assertEqual(out.splitlines()[0], f"Kanvas {kanvas.VERSION} in {self.dir}, the top folder of this git repository")
        self.assertIn("created   kanvas.json", out)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "kanvas.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.dir, ".agents", "skills", "kanvas", "SKILL.md")))
        self.assertEqual(os.listdir(sub), [])

    def test_a_git_file_marks_the_top_folder_too(self):
        """Worktrees and submodules have a .git file."""
        with open(os.path.join(self.dir, ".git"), "w", encoding="utf-8") as f:
            f.write("gitdir: /elsewhere/.git/worktrees/project\n")
        out = self.init(cwd=self.folder("src"))
        self.assertIn("the top folder of this git repository", out.splitlines()[0])
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "kanvas.json")))

    def test_init_dot_keeps_the_current_folder(self):
        os.mkdir(os.path.join(self.dir, ".git"))
        sub = self.folder("docs")
        out = self.init(".", cwd=sub)
        self.assertEqual(out.splitlines()[0], f"Kanvas {kanvas.VERSION} in {sub}")
        self.assertTrue(os.path.isfile(os.path.join(sub, "kanvas.json")))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "kanvas.json")))

    def test_an_existing_board_wins_over_the_git_top_folder(self):
        os.mkdir(os.path.join(self.dir, ".git"))
        app = self.folder("app")
        write_board(os.path.join(app, "kanvas.json"), kanvas.new_board("app"))
        out = self.init(cwd=self.folder("app", "src"))
        self.assertEqual(out.splitlines()[0], f"Kanvas {kanvas.VERSION} in {app}, the folder of the existing kanvas.json")
        self.assertIn("kept      kanvas.json", out)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "kanvas.json")))
        self.assertEqual(os.listdir(os.path.join(app, "src")), [])

    def test_outside_git_init_sets_up_the_current_folder(self):
        if kanvas.find_git_top(self.dir) or kanvas.find_board(self.dir):
            self.skipTest("the temporary folder is inside a git repository or below a kanvas.json")
        sub = self.folder("notes")
        out = self.init(cwd=sub)
        self.assertEqual(out.splitlines()[0], f"Kanvas {kanvas.VERSION} in {sub}")
        self.assertTrue(os.path.isfile(os.path.join(sub, "kanvas.json")))
        self.assertEqual(os.listdir(self.dir), ["notes"])

    def test_init_creates_agents_md_when_missing(self):
        out = self.init()
        self.assertIn("created   AGENTS.md", out)
        self.assertNotIn("add this line", out)
        with open(os.path.join(self.dir, "AGENTS.md"), "rb") as f:
            self.assertEqual(f.read(), b"# Agent instructions\n\n"
                                       b"Plan and track work on the Kanvas board; follow .agents/skills/kanvas/SKILL.md.\n")
        out = self.init()
        self.assertNotIn("AGENTS.md", out)

    def test_a_refused_skill_folder_leaves_the_project_untouched(self):
        """Agent sandboxes such as Codex's keep .agents/ read-only."""
        with open(os.path.join(self.dir, ".agents"), "w", encoding="utf-8") as f:
            f.write("not a folder")
        proc = subprocess.run([sys.executable, SCRIPT, "init", self.dir], capture_output=True)
        err = proc.stderr.decode("utf-8")
        self.assertEqual(proc.returncode, 1, err)
        self.assertIn("Could not write .agents", err)
        self.assertIn("init` outside the sandbox", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(os.listdir(self.dir), [".agents"])

    def test_existing_agent_instructions_stay_byte_identical(self):
        mine = "# Mine\r\nNo newline at the end é".encode("utf-8")
        for name, creates in (("AGENTS.md", False), ("agents.md", False), ("CLAUDE.md", True)):
            with self.subTest(name=name):
                project = tempfile.mkdtemp(dir=self.dir)
                with open(os.path.join(project, name), "wb") as f:
                    f.write(mine)
                out = self.init(project)
                with open(os.path.join(project, name), "rb") as f:
                    self.assertEqual(f.read(), mine)
                self.assertEqual("created   AGENTS.md" in out, creates)
                self.assertEqual("add this line to AGENTS.md" in out, not creates)
                docs = sorted(f for f in os.listdir(project) if f.lower().endswith(".md"))
                self.assertEqual(docs, sorted([name, "AGENTS.md"] if creates else [name]))

    def test_skill_has_frontmatter(self):
        with open(os.path.join(ROOT, "skill", "SKILL.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith("---\nname: kanvas\ndescription: "), text[:80])

    def test_version_file_matches_the_script(self):
        """The package takes its version from kanvas.py; the VERSION file must agree."""
        with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), kanvas.VERSION)


class InstalledCommandTests(TempBoard):
    """Messages name the command the way it was typed, and the installed command checks the project's agent copy."""

    def running(self, installed=False, on_path=False, argv0="kanvas.py"):
        """Installed with or without `kanvas` on PATH (`uvx kanvas-board`), or run as a script from `argv0`."""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(kanvas, "INSTALLED", installed))
        stack.enter_context(mock.patch("shutil.which", return_value="/usr/local/bin/kanvas" if on_path else None))
        stack.enter_context(mock.patch.object(sys, "argv", [argv0]))
        return stack

    def write_copy(self, text):
        skill = os.path.join(self.dir, ".agents", "skills", "kanvas")
        os.makedirs(skill, exist_ok=True)
        with open(os.path.join(skill, "kanvas.py"), "w", encoding="utf-8") as f:
            f.write(f'"""Kanvas."""\n\n{text}\nFORMAT = 1\n')

    def hint(self, copy_version, version="0.5.0", **mode):
        self.write_copy(f'VERSION = "{copy_version}"')
        with self.running(**mode), mock.patch.object(kanvas, "VERSION", version):
            return kanvas.agent_copy_hint(self.path)

    def status(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kanvas.cmd_status(argparse.Namespace(board=self.path))
        return out.getvalue()

    def test_command_line_and_project_command(self):
        with self.running(installed=True, on_path=True):
            self.assertEqual(kanvas.command_line("init"), "kanvas init")
            self.assertEqual(kanvas.command_line(), "kanvas")
            self.assertEqual(kanvas.project_command("serve"), "kanvas serve")
        with self.running(installed=True):
            self.assertEqual(kanvas.command_line("init"), "uvx kanvas-board init")
            self.assertEqual(kanvas.command_line(), "uvx kanvas-board")
            self.assertEqual(kanvas.project_command("serve"), "python .agents/skills/kanvas/kanvas.py serve")
        with self.running(on_path=True, argv0=".agents/skills/kanvas/kanvas.py"):
            self.assertEqual(kanvas.command_line("status"), "python .agents/skills/kanvas/kanvas.py status")
            self.assertEqual(kanvas.project_command(), "python .agents/skills/kanvas/kanvas.py")
        with self.running(argv0="/home/me/My Tools/kanvas.py"):
            self.assertEqual(kanvas.command_line("init"), 'python "/home/me/My Tools/kanvas.py" init')
            self.assertEqual(kanvas.command_line(), 'python "/home/me/My Tools/kanvas.py"')

    def test_the_kanvas_of_a_uvx_run_does_not_count(self):
        """uvx puts its temporary environment on PATH and sets UV; that `kanvas` is gone afterwards."""
        temporary = os.path.join(sys.prefix, "Scripts" if os.name == "nt" else "bin", "kanvas")
        for found, uv, expected in ((temporary, "/usr/bin/uv", "uvx kanvas-board init"),
                                    (temporary, None, "kanvas init"),
                                    ("/usr/local/bin/kanvas", "/usr/bin/uv", "kanvas init")):
            with self.subTest(found=found, uv=uv), mock.patch.object(kanvas, "INSTALLED", True), \
                    mock.patch("shutil.which", return_value=found), \
                    mock.patch.dict(os.environ, {"UV": uv} if uv else {}, clear=False):
                if not uv:
                    os.environ.pop("UV", None)
                self.assertEqual(kanvas.command_line("init"), expected)

    def test_help_names_the_program_as_typed(self):
        with self.running(installed=True):
            text = kanvas.build_parser().format_help()
        self.assertTrue(text.startswith("usage: kanvas [-h]"), text[:40])
        self.assertIn("\n  kanvas status\n", text)
        with self.running(argv0=os.path.join("tools", "kanvas.py")):
            text = kanvas.build_parser().format_help()
        self.assertTrue(text.startswith("usage: kanvas.py [-h]"), text[:40])
        self.assertIn("\n  kanvas.py start DV-02\n", text)
        with self.running(argv0=""):
            self.assertTrue(kanvas.build_parser().format_help().startswith("usage: kanvas.py [-h]"))

    def test_serve_help_says_which_ports_it_tries(self):
        with mock.patch.object(sys, "stdout", io.StringIO()) as out, self.assertRaises(SystemExit):
            kanvas.main(["serve", "-h"])
        text = " ".join(out.getvalue().split())
        self.assertIn("--port PORT port to serve on (default: 8765), or the next free one up to 19 higher (8784 by default);", text)
        self.assertIn("when a Kanvas already runs on any of these ports, serve opens the board there and returns", text)

    def test_missing_board_names_the_init_command(self):
        nowhere = argparse.Namespace()
        with mock.patch.object(kanvas, "find_board", return_value=None), mock.patch.dict(os.environ):
            os.environ.pop("KANVAS_BOARD", None)
            with self.running(installed=True, on_path=True):
                with self.assertRaisesRegex(kanvas.KanvasError, "Create one with `kanvas init`, or pass --board PATH"):
                    kanvas.board_path(nowhere)
            with self.running(installed=True):
                with self.assertRaisesRegex(kanvas.KanvasError, "`uvx kanvas-board init`"):
                    kanvas.board_path(nowhere)
            with self.running(argv0="kanvas.py"):
                with self.assertRaisesRegex(kanvas.KanvasError, "`python kanvas.py init`"):
                    kanvas.board_path(nowhere)

    def test_other_messages_name_the_command(self):
        args = argparse.Namespace(board=self.path, id="DV-02", message=None, stdin=False, file=None)
        with self.running(installed=True, on_path=True):
            with self.assertRaisesRegex(kanvas.KanvasError, 'e.g. `kanvas finish DV-02 -m "Added X; tests pass"`'):
                kanvas.cmd_finish(args)
            with self.assertRaisesRegex(kanvas.KanvasError, "upgrade with `uv tool upgrade kanvas-board`"):
                kanvas.parse_board('{"kanvas": 99}')
        with self.running(argv0="kanvas.py"):
            with self.assertRaisesRegex(kanvas.KanvasError, "`python kanvas.py finish DV-02 -m"):
                kanvas.cmd_finish(args)

    def test_older_agent_copy(self):
        self.assertEqual(self.hint("0.4.0", installed=True, on_path=True),
                         "This project's agent copy is 0.4.0; run `kanvas init` to update it to 0.5.0.")
        self.assertEqual(self.hint("0.9.0", "0.10.0", installed=True),
                         "This project's agent copy is 0.9.0; run `uvx kanvas-board init` to update it to 0.10.0.")

    def test_newer_agent_copy(self):
        self.assertEqual(self.hint("0.5.0", "0.4.0", installed=True, on_path=True),
                         "This project's agent copy is 0.5.0, newer than your kanvas 0.4.0; "
                         "update it with `uv tool upgrade kanvas-board`.")

    def test_test_versions_come_before_their_release(self):
        order = ["0.4.0a1", "0.4.0b2", "0.4.0rc1", "0.4.0rc2", "0.4", "0.4.1", "0.10.0rc1", "0.10.0"]
        self.assertEqual(sorted(order, key=kanvas._version_key), order)
        self.assertIn("update it to 0.4.0.", self.hint("0.4.0rc1", "0.4.0", installed=True, on_path=True))
        self.assertIn("update it to 0.4.0rc2.", self.hint("0.4.0rc1", "0.4.0rc2", installed=True, on_path=True))
        self.assertIn("newer than your kanvas 0.4.0rc1", self.hint("0.4.0", "0.4.0rc1", installed=True, on_path=True))

    def test_no_hint_when_versions_match_or_cannot_be_read(self):
        self.assertIsNone(self.hint("0.5.0", installed=True, on_path=True))
        self.assertIsNone(self.hint("0.5", installed=True, on_path=True))
        self.assertIsNone(self.hint("0.4.0", "0.5.0.dev1", installed=True, on_path=True))
        self.write_copy("# no version here")
        with self.running(installed=True, on_path=True):
            self.assertIsNone(kanvas.agent_copy_hint(self.path))
        shutil.rmtree(os.path.join(self.dir, ".agents"))
        with self.running(installed=True, on_path=True):
            self.assertIsNone(kanvas.agent_copy_hint(self.path))

    def test_no_hint_when_run_as_a_script(self):
        self.assertIsNone(self.hint("0.4.0", on_path=True))

    def test_status_ends_with_the_hint(self):
        self.write_copy('VERSION = "0.0.1"')
        with self.running(installed=True, on_path=True):
            out = self.status()
        self.assertTrue(out.endswith(f"\n\nThis project's agent copy is 0.0.1; run `kanvas init` to update it to "
                                     f"{kanvas.VERSION}.\n"), out)
        with self.running():
            self.assertNotIn("agent copy", self.status())


LEGACY_CANVAS = {
    "nodes": [
        {"id": "g-res", "type": "group", "x": 0, "y": 0, "width": 400, "height": 600, "label": "Research"},
        {"id": "g-dev", "type": "group", "x": 500, "y": 0, "width": 400, "height": 600, "label": "Development"},
        {"id": "legend", "type": "text", "text": "## Legend\nRed = To Do", "x": -500, "y": 0, "width": 300, "height": 200, "color": "0"},
        {"id": "n1", "type": "text", "text": "## RS-01 Read papers\nFind prior work", "x": 20, "y": 20, "width": 300, "height": 120, "color": "4"},
        {"id": "n2", "type": "text", "text": "## RS-02 Summarise", "x": 20, "y": 200, "width": 300, "height": 120, "color": "0"},
        {"id": "n3", "type": "text", "text": "## DV-01 Build it\nLine one\nLine two", "x": 520, "y": 20, "width": 300, "height": 120, "color": "2"},
        {"id": "n4", "type": "text", "text": "## Untitled idea\nSomething new", "x": 520, "y": 200, "width": 300, "height": 120, "color": "6"},
        {"id": "n5", "type": "text", "text": "## DV-01 Duplicate id", "x": 520, "y": 400, "width": 300, "height": 120, "color": "5"},
        {"id": "note", "type": "text", "text": "free text without a heading", "x": 0, "y": 900, "width": 300, "height": 120},
    ],
    "edges": [
        {"id": "e1", "fromNode": "n1", "toNode": "n2"},
        {"id": "e2", "fromNode": "n2", "toNode": "n3"},
        {"id": "e3", "fromNode": "n3", "toNode": "n4"},
    ],
}


class ImportExportTests(TempBoard):
    def initial_board(self):
        return kanvas.new_board("empty")

    def test_import_legacy_canvas(self):
        src = os.path.join(self.dir, "Project.canvas")
        with open(src, "w", encoding="utf-8") as f:
            json.dump(LEGACY_CANVAS, f)
        self.run_cli("import", "Project.canvas")
        board = self.board()
        self.assertEqual(board["title"], "Project")
        self.assertEqual(board["groups"], [{"id": "RS", "name": "Research"}, {"id": "DV", "name": "Development"}])
        by_title = {t["title"]: t for t in board["tasks"]}
        self.assertEqual(set(by_title), {"Read papers", "Summarise", "Build it", "Untitled idea", "Duplicate id"})
        self.assertEqual(by_title["Read papers"]["state"], "done")
        self.assertEqual(by_title["Summarise"]["state"], "todo")
        self.assertEqual(by_title["Build it"]["state"], "doing")
        self.assertEqual(by_title["Build it"]["desc"], "Line one\nLine two")
        self.assertEqual(by_title["Untitled idea"]["state"], "proposed")
        self.assertEqual(by_title["Duplicate id"]["state"], "review")
        self.assertEqual(by_title["Summarise"]["deps"], ["RS-01"])
        self.assertEqual(by_title["Untitled idea"]["deps"], [by_title["Build it"]["id"]])
        ids = [t["id"] for t in board["tasks"]]
        self.assertEqual(len(set(ids)), 5)
        self.assertIn("DV-01", ids)
        self.run_cli("check")
        with open(src, encoding="utf-8") as f:
            self.assertEqual(json.load(f), LEGACY_CANVAS)
        self.assertIn("--force", self.fails("import", "Project.canvas"))
        self.run_cli("import", "Project.canvas", "--force")

    def test_import_refuses_a_malformed_canvas(self):
        card = {"id": "a", "type": "text", "text": "## AB-1 x", "color": "1", "x": 10, "y": 10, "width": 10, "height": 10}
        group = {"type": "group", "x": 0, "y": 0, "width": 100, "height": 100}
        cases = [
            ([], "JSON object"),
            ({"nodes": {"a": card}}, '"nodes"'),
            ({"nodes": [card], "edges": 5}, '"edges"'),
            ({"nodes": [card, "b"]}, '"nodes"'),
            ({"nodes": [{"type": "text", "text": "## AB-1 x", "color": "1"}]}, '"id"'),
            ({"nodes": [card, group]}, '"id"'),
            ({"nodes": [dict(card, x="10")]}, "numbers"),
            ({"nodes": [card, dict(group, id="g", width=None)]}, "numbers"),
            ({"nodes": [dict(card, text=["## AB-1 x"])]}, '"text"'),
            ({"nodes": [card], "edges": [{"fromNode": ["a"], "toNode": "a"}]}, '"fromNode"'),
        ]
        before = self.raw()
        for canvas, problem in cases:
            with self.subTest(canvas=canvas):
                with open(os.path.join(self.dir, "Bad.canvas"), "w", encoding="utf-8") as f:
                    json.dump(canvas, f)
                code, _, err = self.run_cli("import", "Bad.canvas", check=False)
                self.assertEqual(code, 1, err)
                self.assertIn("Error: Bad.canvas is not an Obsidian canvas: ", err)
                self.assertIn(problem, err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(self.raw(), before)

    def test_export_snapshot(self):
        board = self.board()
        board["tasks"].append(task("G-01", title="Café \ud83d", desc="</script><script>alert(1)</script>"))
        write_board(self.path, board)
        self.run_cli("export", "snap.html")
        with open(os.path.join(self.dir, "snap.html"), encoding="utf-8") as f:
            html = f.read()
        m = re.search(r'<script id="kanvas-snapshot" type="application/json" data-at="[^"]+" data-name="([^"]+)">(.*?)</script>', html, re.S)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), kanvas.board_name(self.path))
        self.assertNotIn("<", m.group(2))
        self.assertEqual(json.loads(m.group(2))["tasks"][0]["desc"], "</script><script>alert(1)</script>")
        self.assertEqual(json.loads(m.group(2))["tasks"][0]["title"], "Café \ud83d")
        self.assertIn("Café", m.group(2))

    def test_an_untitled_board_goes_by_its_name_in_the_page(self):
        """As on the boards page: the name in its address when served, the one export recorded, else "Kanvas"."""
        code = ("globalThis.location = { pathname: '/my-project/' };\n"
                "let board, store;\n"
                "process.stdout.write(JSON.stringify(input.map(([title, kind, boardName]) => {\n"
                "  board = { title }; store = kind ? { kind, boardName } : null;\n"
                "  return boardLabel();\n"
                "})));")
        cases = [["Mine", "server", None], ["", "server", None], ["", "snapshot", "exported"], ["", "file", None], ["", None, None]]
        out = run_html_js(self, code, json.dumps(cases), [("const boardHere =", "\nasync function refreshBoards")])
        self.assertEqual(json.loads(out), ["Mine", "my-project", "exported", "Kanvas", "Kanvas"])


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


class ImageTests(TempBoard):
    """Images live next to the board in kanvas-files/, named by a hash of their bytes."""

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_images_are_kept_once_by_their_hash(self):
        ref = kanvas.store_image(self.path, PNG)
        self.assertRegex(ref, r"^kanvas-files/[0-9a-f]{16}\.png$")
        self.assertEqual(kanvas.store_image(self.path, PNG), ref)
        self.assertEqual(os.listdir(os.path.join(self.dir, "kanvas-files")), [ref.split("/")[1]])
        with open(kanvas.image_path(self.path, ref), "rb") as f:
            self.assertEqual(f.read(), PNG)
        for data in (b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"<html>", b""):
            with self.subTest(data=data):
                self.assertRaises(kanvas.KanvasError, kanvas.store_image, self.path, data)
        self.assertEqual([kanvas.image_type(d) for d in (b"\xff\xd8\xff\xe0", b"GIF89a..", b"RIFF\0\0\0\0WEBPVP8 ")], ["jpg", "gif", "webp"])
        for bad in ("kanvas-files/../kanvas.json", "kanvas-files/0123456789abcdef.svg", "elsewhere/0123456789abcdef.png"):
            self.assertIsNone(kanvas.image_path(self.path, bad))

    def test_attach_adds_a_note_with_the_images(self):
        shot = self.write("login [form].png", PNG)
        _, out, _ = self.run_cli("attach", "DV-02", shot, "-m", "The login form after the fix", "--as", "claude")
        ref = kanvas.store_image(self.path, PNG)
        note = self.get("DV-02")["notes"][-1]
        self.assertEqual((note["by"], note["text"]), ("claude", f"The login form after the fix\n\n![login (form)]({ref})"))
        self.assertIn(f"![login (form)]({ref})", out)
        self.run_cli("attach", "DV-03", shot, shot)
        self.assertEqual(self.get("DV-03")["notes"][-1]["text"], f"![login (form)]({ref})\n\n![login (form)]({ref})")
        before = self.raw()
        self.assertIn("Only PNG, JPEG, GIF and WebP", self.fails("attach", "DV-02", self.write("x.svg", b"<svg/>")))
        self.assertIn("Cannot read", self.fails("attach", "DV-02", "missing.png"))
        self.assertIn("is done", self.fails("attach", "DV-01", shot))
        self.assertEqual(self.raw(), before)

    def test_check_warns_about_missing_and_unused_images(self):
        used = kanvas.store_image(self.path, PNG)
        unused = kanvas.store_image(self.path, PNG + b"x")
        missing = "kanvas-files/ffffffffffffffff.png"
        board = self.board()
        board["tasks"][1]["desc"] = f"![a]({used})"
        board["tasks"][2]["notes"] = [{"at": "2026-10-28T10:00:00Z", "by": "human", "text": f"![b]({missing})"}]
        write_board(self.path, board)
        code, out, _ = self.run_cli("check")
        self.assertEqual(code, 0)
        self.assertIn(f"warning: DV-03: image {missing} is missing", out)
        self.assertIn(f"warning: {unused} is not used by any task", out)
        self.assertNotIn(f"warning: {used}", out)

    def test_a_refused_attach_keeps_no_image(self):
        shot = self.write("shot.png", PNG)
        self.assertIn("is done", self.fails("attach", "DV-01", shot))
        self.assertIn("Only PNG, JPEG, GIF and WebP", self.fails("attach", "DV-02", shot, self.write("x.svg", b"<svg/>")))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "kanvas-files")))

    def test_export_and_check_leave_out_files_that_are_not_the_image_they_are_named_after(self):
        folder = os.path.join(self.dir, "kanvas-files")
        os.mkdir(folder)
        with open(os.path.join(self.dir, "secret.env"), "wb") as f:
            f.write(PNG + b"TOKEN=secret")
        ref = "kanvas-files/0123456789abcdef.png"
        try:
            os.symlink(os.path.join("..", "secret.env"), os.path.join(self.dir, ref))
        except (OSError, NotImplementedError):
            self.skipTest("this system cannot create symbolic links")
        board = self.board()
        board["tasks"][1]["desc"] = f"![a]({ref})"
        write_board(self.path, board)
        _, out, _ = self.run_cli("export", "snap.html")
        self.assertNotIn("image", out)
        with open(os.path.join(self.dir, "snap.html"), encoding="utf-8") as f:
            self.assertNotIn(base64.b64encode(PNG + b"TOKEN=secret").decode("ascii"), f.read())
        self.assertIn(f"image {ref} is missing", self.run_cli("check")[1])
        # Keeping the image whose name a link holds replaces the link, never the file the link points to.
        real = kanvas.image_path(self.path, kanvas.store_image(self.path, PNG))
        os.remove(real)
        os.symlink(os.path.join("..", "secret.env"), real)
        kanvas.store_image(self.path, PNG)
        self.assertFalse(os.path.islink(real))
        with open(os.path.join(self.dir, "secret.env"), "rb") as f:
            self.assertEqual(f.read(), PNG + b"TOKEN=secret")

    def test_export_carries_the_images(self):
        ref = kanvas.store_image(self.path, PNG)
        board = self.board()
        board["tasks"][1]["desc"] = f"![a]({ref}) and ![gone](kanvas-files/ffffffffffffffff.png)"
        write_board(self.path, board)
        _, out, _ = self.run_cli("export", "snap.html")
        self.assertIn("and 1 image.", out)
        with open(os.path.join(self.dir, "snap.html"), encoding="utf-8") as f:
            html = f.read()
        m = re.search(r'<script id="kanvas-files" type="application/json">(.*?)</script>', html, re.S)
        self.assertEqual(json.loads(m.group(1)), {ref: "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")})

    def test_images_attached_through_a_link_sit_next_to_the_real_board(self):
        link = os.path.join(self.dir, "proj", "kanvas.json")
        os.mkdir(os.path.dirname(link))
        try:
            os.symlink(os.path.join("..", "kanvas.json"), link)
        except (OSError, NotImplementedError):
            self.skipTest("this system cannot create symbolic links")
        self.run_cli("-b", link, "attach", "DV-02", self.write("shot.png", PNG))
        ref = re.search(r"\((kanvas-files/[^)]+)\)", self.get("DV-02")["notes"][-1]["text"]).group(1)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, ref)))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "proj", kanvas.FILES_DIR)))
        self.assertEqual(kanvas.image_path(link, ref), kanvas.image_path(self.path, ref))
        # Through the real path, the board finds the image it was given through the link.
        self.assertNotIn("missing", self.run_cli("check")[1])
        self.assertIn("and 1 image.", self.run_cli("export", "snap.html")[1])


class ConcurrencyTests(TempBoard):
    def test_parallel_agents_do_not_lose_writes(self):
        workers, per_worker = 6, 4
        script = (
            "import subprocess, sys\n"
            "for i in range(%d):\n"
            "    subprocess.run([sys.executable, %r, 'note', 'DV-02', '--as', sys.argv[1], str(i)], check=True, stdout=subprocess.DEVNULL)\n"
        ) % (per_worker, SCRIPT)
        procs = [subprocess.Popen([sys.executable, "-c", script, f"w{n}"], cwd=self.dir) for n in range(workers)]
        for p in procs:
            self.assertEqual(p.wait(timeout=120), 0)
        self.assertEqual(len(self.get("DV-02")["notes"]), workers * per_worker)
        leftovers = [f for f in os.listdir(self.dir) if f.endswith((".lock", ".tmp"))]
        self.assertEqual(leftovers, [])

    def test_a_failed_write_leaves_no_temp_file(self):
        """A full disk, or text that cannot be encoded, fails after the temporary file exists."""
        def full_disk(*args, **kwargs):
            f = open(*args, **kwargs)
            f.write = mock.Mock(side_effect=OSError("No space left on device"))
            return f
        before = self.raw()
        with mock.patch.object(kanvas, "open", side_effect=full_disk, create=True), self.assertRaises(OSError):
            kanvas.write_text_atomic(self.path, "{}")
        with self.assertRaises(UnicodeEncodeError):
            kanvas.write_text_atomic(self.path, "Lone \ud83d surrogate")
        self.assertEqual(self.raw(), before)
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".tmp")], [])

    def test_stale_lock_is_cleared(self):
        lock = self.path + ".lock"
        open(lock, "w").close()
        old = time.time() - 60
        os.utime(lock, (old, old))
        self.run_cli("note", "DV-02", "after a crash")
        self.assertFalse(os.path.exists(lock))

    def test_writers_outwait_a_lock_left_by_a_crash(self):
        self.assertLess(kanvas.STALE_LOCK_AGE, kanvas.LOCK_TIMEOUT)
        lock = self.path + ".lock"
        open(lock, "w").close()
        with mock.patch.object(kanvas, "STALE_LOCK_AGE", 0.3), mock.patch.object(kanvas, "LOCK_TIMEOUT", 0.6):
            kanvas.mutate(self.path, lambda board: kanvas.add_note(kanvas.find_task(board, "DV-02"), "after a crash", "codex"))
        self.assertFalse(os.path.exists(lock))
        self.assertEqual(self.get("DV-02")["notes"][-1]["text"], "after a crash")

    def test_busy_lock_gives_up_at_the_deadline(self):
        open(self.path + ".lock", "w").close()
        with mock.patch.object(kanvas, "LOCK_TIMEOUT", 0.2):
            with self.assertRaisesRegex(kanvas.KanvasError, "Board is busy"):
                kanvas.mutate(self.path, lambda board: None)

    def test_writes_through_a_link_reach_the_board(self):
        link = os.path.join(self.dir, "proj", "kanvas.json")
        os.mkdir(os.path.dirname(link))
        try:
            os.symlink(os.path.join("..", "kanvas.json"), link)
        except (OSError, NotImplementedError):
            self.skipTest("this system cannot create symbolic links")
        self.run_cli("-b", link, "note", "DV-02", "through a link")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(self.get("DV-02")["notes"][-1]["text"], "through a link")
        # The lock sits next to the real file, so writers through the link wait for the board's lock.
        open(self.path + ".lock", "w").close()
        with mock.patch.object(kanvas, "LOCK_TIMEOUT", 0.2):
            with self.assertRaisesRegex(kanvas.KanvasError, "Board is busy"):
                kanvas.mutate(link, lambda board: None)

    def test_unwritable_folder_is_reported(self):
        started = time.monotonic()
        with mock.patch.object(kanvas, "LOCK_TIMEOUT", 0.2), mock.patch("os.open", side_effect=PermissionError):
            with self.assertRaisesRegex(kanvas.KanvasError, "permission denied"):
                kanvas.mutate(self.path, lambda board: None)
        self.assertLess(time.monotonic() - started, 5)


class ServerTests(TempBoard):
    def setUp(self):
        super().setUp()
        self.server = kanvas.start_server(self.path, "127.0.0.1", 0, quiet=True)
        self.port = self.server.server_address[1]
        self.name = kanvas.board_name(self.path)
        self.api = f"/{self.name}/api/board"
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            r = conn.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            conn.close()

    def put(self, board, etag=None, ctype="application/json"):
        headers = {"Content-Type": ctype}
        if etag:
            headers["If-Match"] = etag
        return self.request("PUT", self.api, json.dumps(board).encode("utf-8"), headers)

    def close_board(self, name=None, headers=None):
        return self.request("DELETE", "/api/boards/" + (name or self.name), b"{}",
                            headers if headers is not None else {"Content-Type": "application/json"})

    def test_close_forgets_the_board_and_keeps_its_file(self):
        self.server.remember = os.path.join(self.dir, "state", "boards.json")
        other = self.other_project()
        self.server.add_board(other)
        before = self.raw()
        status, _, body = self.close_board()
        self.assertEqual(status, 200)
        info = json.loads(body)
        self.assertFalse(info["stopping"])
        self.assertEqual([b["path"] for b in info["boards"]], [other])
        self.assertEqual(self.raw(), before)
        self.assertEqual(json.loads(kanvas.read_text_file(self.server.remember))["boards"], [other])
        self.assertEqual(self.request("GET", self.api)[0], 404)
        self.assertEqual(self.request("PUT", self.api, b"{}", {"Content-Type": "application/json"})[0], 404)
        self.assertEqual(self.close_board()[0], 404)
        self.assertEqual(self.request("GET", "/other/api/board")[0], 200)
        self.assertTrue(json.loads(self.request("GET", "/api/boards")[2])["manage"])

    def test_closed_urls_cannot_read_or_write_another_project(self):
        for replacement_title in ("Other", "Different JSON"):
            with self.subTest(replacement_title=replacement_title):
                folder = "identical" if replacement_title == "Other" else "different"
                original = self.other_project(os.path.join(folder, "a", "project"))
                replacement = self.other_project(os.path.join(folder, "b", "project"), title=replacement_title)
                name = json.loads(self.open_board(original)[2])["name"]
                api = f"/{name}/api/board"
                _, headers, body = self.request("GET", api)
                self.assertEqual(self.close_board(name)[0], 200)
                replacement_name = json.loads(self.open_board(replacement)[2])["name"]
                before = kanvas.read_text_file(replacement)
                stale_edit = json.loads(body)
                stale_edit["title"] = "Edit from the closed tab"
                status, _, _ = self.request("PUT", api, json.dumps(stale_edit).encode("utf-8"),
                                            {"Content-Type": "application/json", "If-Match": headers["ETag"]})
                # A 409 is unsafe too: the browser would replay the change onto its response.
                self.assertEqual(status, 404)
                self.assertEqual(self.request("GET", api)[0], 404)
                self.assertEqual(kanvas.read_text_file(replacement), before)
                self.assertNotEqual(replacement_name, name)
                self.assertEqual(self.request("GET", f"/{replacement_name}/api/board")[0], 200)
                self.assertEqual(json.loads(self.open_board(original)[2])["name"], name)
                self.assertEqual(self.request("GET", api)[2], body)

    def test_close_requires_this_computer_and_json(self):
        for headers, expected in (({}, 415), ({"Content-Type": "text/plain"}, 415),
                                  ({"Content-Type": "application/json", "Host": "evil.example"}, 403),
                                  ({"Content-Type": "application/json", "X-Forwarded-For": "100.64.0.8"}, 403)):
            with self.subTest(headers=headers):
                self.assertEqual(self.close_board(headers=headers)[0], expected)
                self.assertEqual(self.server.board_path(self.name), self.path)
        self.server.opens_boards = False
        try:
            self.assertEqual(self.close_board()[0], 403)
            self.assertFalse(json.loads(self.request("GET", "/api/boards")[2])["manage"])
        finally:
            self.server.opens_boards = True

    def test_close_cli_finds_the_board_and_reports_when_it_is_not_open(self):
        self.server.add_board(self.other_project())
        _, out, _ = self.run_cli("close", "--port", str(self.port))
        self.assertIn("Closed " + self.name, out)
        self.assertIn("file was kept", out)
        self.assertIsNone(self.server.board_path(self.name))
        _, out, _ = self.run_cli("close", "--port", str(self.port))
        self.assertIn("is not open", out)
        self.assertNotIn("Closed ", out)

    def test_close_cli_last_board_reports_shutdown(self):
        _, out, _ = self.run_cli("close", "--port", str(self.port))
        self.assertIn("last board. Kanvas is stopping", out)
        self.assertEqual(self.server.board_list(), [])
        self.assertIsNotNone(self.server.stop_at)

    def test_close_cli_with_no_server_is_harmless(self):
        before = self.raw()
        with mock.patch.object(kanvas, "find_running", return_value=None), \
                mock.patch.object(sys, "stdout", io.StringIO()) as out:
            self.assertEqual(kanvas.main(["-b", self.path, "close"]), 0)
        self.assertIn("not running", out.getvalue())
        self.assertEqual(self.raw(), before)

    def test_close_cli_accepts_an_explicit_missing_board(self):
        other = self.other_project()
        self.server.add_board(other)
        os.remove(other)
        _, out, _ = self.run_cli("-b", other, "close", "--port", str(self.port))
        self.assertIn("Closed other", out)
        self.assertEqual(self.server.board_path(self.name), self.path)

    def test_missing_boards_are_removed_without_a_browser_poll(self):
        other = self.other_project()
        self.server.add_board(other)
        os.remove(other)
        deadline = time.monotonic() + 6
        while self.server.board_path("other") is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNone(self.server.board_path("other"))
        self.assertEqual(self.request("GET", self.api)[0], 200)

    def test_starting_skips_the_host_name_lookup(self):
        """socket.getfqdn, which HTTPServer calls on start, can take half a minute on macOS."""
        with mock.patch("socket.getfqdn", side_effect=AssertionError("getfqdn was called")):
            server = kanvas.start_server(self.path, "127.0.0.1", 0, quiet=True)
        server.server_close()
        self.assertEqual(server.server_name, "127.0.0.1")

    def test_a_busy_port_moves_kanvas_to_the_next_free_one(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            server = kanvas.start_server(self.path, "127.0.0.1", port, quiet=True)
        server.server_close()
        self.assertIn(server.server_address[1], range(port + 1, port + 20))

    def test_serves_the_page_and_the_board(self):
        status, headers, body = self.request("GET", f"/{self.name}/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>Kanvas</title>", body)
        status, headers, body = self.request("GET", self.api)
        self.assertEqual(status, 200)
        self.assertEqual(body, self.raw())
        status, _, body = self.request("GET", self.api, headers={"If-None-Match": headers["ETag"]})
        self.assertEqual((status, body), (304, b""))

    def test_compare_and_swap(self):
        _, headers, body = self.request("GET", self.api)
        etag = headers["ETag"]
        board = json.loads(body)
        self.run_cli("note", "DV-02", "agent wrote first")
        board["tasks"][3]["state"] = "todo"
        status, headers, body = self.put(board, etag)
        self.assertEqual(status, 409)
        current = json.loads(body)
        self.assertEqual(current["tasks"][1]["notes"][0]["text"], "agent wrote first")
        current["tasks"][3]["state"] = "todo"
        status, headers2, body = self.put(current, headers["ETag"])
        self.assertEqual(status, 200)
        self.assertEqual(body, self.raw())
        self.assertEqual(headers2["ETag"], kanvas.etag_of(self.raw()))
        self.assertEqual(self.get("DV-04")["state"], "todo")
        self.assertEqual(self.get("DV-02")["notes"][0]["text"], "agent wrote first")

    def test_rejects_unsafe_or_invalid_writes(self):
        before = self.raw()
        board = json.loads(before)
        self.assertEqual(self.put(board, ctype="text/plain")[0], 415)
        bad = json.loads(before)
        bad["tasks"][0]["state"] = "finished"
        self.assertEqual(self.put(bad, kanvas.etag_of(before))[0], 400)
        dup = json.loads(before)
        dup["tasks"].append(dict(dup["tasks"][0]))
        self.assertEqual(self.put(dup, kanvas.etag_of(before))[0], 400)
        status, _, _ = self.request("GET", self.api, headers={"Host": "evil.example:8765"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("PUT", self.api, b"{}", {"Host": "evil.example", "Content-Type": "application/json"})
        self.assertEqual(status, 403)
        self.assertEqual(self.raw(), before)

    def test_rejects_fields_of_the_wrong_type(self):
        before = self.raw()
        for bad, message in WRONG_TYPES:
            with self.subTest(message=message):
                status, _, body = self.put(dict(kanvas.new_board(), tasks=[bad]), kanvas.etag_of(before))
                self.assertEqual((status, json.loads(body)["error"]), (400, message))
        self.assertEqual(self.raw(), before)

    def test_writes_need_the_version_they_change_and_a_task_list(self):
        before = self.raw()
        board = json.loads(before)
        board["tasks"][3]["state"] = "todo"
        status, _, body = self.put(board)
        self.assertEqual(status, 428)
        self.assertIn("If-Match", json.loads(body)["error"])
        for partial in ({}, {"kanvas": 1, "title": "Test", "groups": []}):
            with self.subTest(partial=partial):
                status, _, body = self.put(partial, kanvas.etag_of(before))
                self.assertEqual((status, json.loads(body)["error"]), (400, "Expected a board with its task list"))
        self.assertEqual(self.put({"tasks": None}, kanvas.etag_of(before))[0], 400)
        self.assertEqual(self.raw(), before)
        status, headers, _ = self.put(board, kanvas.etag_of(before))
        self.assertEqual(status, 200)
        # A board whose tasks were all deleted still has its task list.
        self.assertEqual(self.put(dict(board, tasks=[]), headers["ETag"])[0], 200)
        self.assertEqual(self.board()["tasks"], [])

    def test_a_malformed_content_length_is_answered_at_once(self):
        before = self.raw()
        for length in ("abc", "-1", "1.5"):
            for method, path in (("PUT", self.api), ("POST", "/api/boards")):
                with self.subTest(length=length, method=method), \
                        socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
                    s.sendall(f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nContent-Type: application/json\r\n"
                              f"If-Match: {kanvas.etag_of(before)}\r\nContent-Length: {length}\r\n\r\n".encode("ascii"))
                    status = s.makefile("rb").readline().split()[1]
                    self.assertEqual(status, b"400")
        self.assertEqual(self.raw(), before)

    def test_uploads_and_serves_images(self):
        files = f"/{self.name}/api/files"
        before = self.raw()
        status, _, body = self.request("POST", files, PNG, {"Content-Type": "image/png"})
        self.assertEqual(status, 200)
        ref = json.loads(body)["path"]
        self.assertEqual(ref, kanvas.store_image(self.path, PNG))
        status, headers, body = self.request("GET", f"/{self.name}/{ref}")
        self.assertEqual((status, body, headers["Content-Type"]), (200, PNG, "image/png"))
        self.assertIn("immutable", headers["Cache-Control"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        # Uploading leaves the board as it is: the image goes into it as Markdown, with the next save.
        self.assertEqual(self.raw(), before)

    def test_refuses_what_is_not_an_image_of_the_board(self):
        files = f"/{self.name}/api/files"
        cases = [
            (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", {"Content-Type": "image/svg+xml"}, 415),
            (PNG, {"Content-Type": "application/x-www-form-urlencoded"}, 415),
            (PNG, {"Content-Type": "text/plain"}, 415),
            (b"<html><script>alert(1)</script>", {"Content-Type": "image/png"}, 400),
            (PNG, {"Content-Type": "image/png", "Host": "evil.example"}, 403),
        ]
        for body, headers, expected in cases:
            with self.subTest(headers=headers, body=body[:12]):
                self.assertEqual(self.request("POST", files, body, headers)[0], expected)
        self.assertEqual(self.request("POST", "/nope/api/files", PNG, {"Content-Type": "image/png"})[0], 404)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "kanvas-files")))
        for path in ("kanvas-files/..%2Fkanvas.json", "kanvas-files/0123456789abcdef.png", "kanvas-files/kanvas.json"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", f"/{self.name}/{path}")[0], 404)

    def test_serves_no_file_put_under_an_images_name(self):
        # A cloned project can hold a link or another file named like an image; neither is read out.
        folder = os.path.join(self.dir, "kanvas-files")
        os.mkdir(folder)
        with open(os.path.join(self.dir, "secret.env"), "wb") as f:
            f.write(PNG + b"TOKEN=secret")
        link = os.path.join(folder, "0123456789abcdef.png")
        try:
            os.symlink(os.path.join("..", "secret.env"), link)
        except (OSError, NotImplementedError):
            self.skipTest("this system cannot create symbolic links")
        with open(os.path.join(folder, "fedcba9876543210.png"), "wb") as f:
            f.write(PNG + b"TOKEN=secret")
        for name in ("0123456789abcdef.png", "fedcba9876543210.png"):
            with self.subTest(name=name):
                self.assertEqual(self.request("GET", f"/{self.name}/kanvas-files/{name}")[0], 404)
        ref = kanvas.store_image(self.path, PNG)
        status, _, body = self.request("GET", f"/{self.name}/{ref}")
        self.assertEqual((status, body), (200, PNG))

    def other_project(self, folder="other", title="Other"):
        path = os.path.join(self.dir, folder, "kanvas.json")
        os.makedirs(os.path.dirname(path))
        write_board(path, dict(self.initial_board(), title=title))
        return path

    def open_board(self, path, host="127.0.0.1"):
        body = json.dumps({"path": path}).encode("utf-8")
        return self.request("POST", "/api/boards", body, {"Content-Type": "application/json", "Host": host})

    def test_serves_several_boards_each_at_its_own_address(self):
        status, headers, _ = self.request("GET", "/")
        self.assertEqual((status, headers["Location"]), (302, f"/{self.name}/"))
        other = self.other_project()
        status, _, body = self.open_board(other)
        self.assertEqual((status, json.loads(body)), (200, {"name": "other", "url": "/other/"}))
        with open(other, "rb") as f:
            self.assertEqual(self.request("GET", "/other/api/board")[2], f.read())
        self.assertEqual(self.request("GET", self.api)[2], self.raw())
        _, headers, body = self.request("GET", "/other/api/board")
        board = json.loads(body)
        board["tasks"][3]["state"] = "todo"
        status, _, _ = self.request("PUT", "/other/api/board", json.dumps(board).encode("utf-8"),
                                    {"Content-Type": "application/json", "If-Match": headers["ETag"]})
        self.assertEqual(status, 200)
        with open(other) as f:
            self.assertEqual(json.load(f)["tasks"][3]["state"], "todo")
        self.assertEqual(self.get("DV-04")["state"], "proposed")

        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'href="/other/"', body)
        self.assertIn(f'href="/{self.name}/"'.encode(), body)
        listing = json.loads(self.request("GET", "/api/boards")[2])
        self.assertEqual(listing["kanvas"], kanvas.VERSION)
        self.assertEqual([(b["name"], b["title"]) for b in listing["boards"]], [(self.name, "Test"), ("other", "Other")])
        self.assertEqual(self.request("GET", "/other")[1]["Location"], "/other/")
        self.assertIn(b"<title>Kanvas</title>", self.request("GET", "/other/")[2])

    def test_names_boards_after_their_folders(self):
        self.assertEqual(json.loads(self.open_board(self.path)[2])["name"], self.name)
        first = self.other_project(os.path.join("a", "Some project!"))
        second = self.other_project(os.path.join("b", "Some project!"))
        api = self.other_project("api")
        self.assertEqual([json.loads(self.open_board(p)[2])["name"] for p in (first, second, api, first)],
                         ["Some-project", "Some-project-2", "api-2", "Some-project"])

    def test_unknown_boards_say_how_to_open_them(self):
        status, _, body = self.request("GET", "/nothing/")
        self.assertEqual(status, 404)
        self.assertIn(b"Run <code>kanvas serve</code> in its project", body)
        self.assertIn(b"its project folder may have been moved or renamed", body)
        self.assertNotIn(b"forgets", body)
        status, _, body = self.request("GET", "/nothing/api/board")
        self.assertEqual(status, 404)
        self.assertIn("kanvas serve", json.loads(body)["error"])
        self.assertEqual(self.request("PUT", "/nothing/api/board", b"{}", {"Content-Type": "application/json"})[0], 404)

    def test_the_boards_page_says_once_how_to_open_a_board(self):
        hint = b"Run <code>kanvas serve</code> in a project to open its board here."
        empty = kanvas.start_server(None, "127.0.0.1", 0, quiet=True)
        threading.Thread(target=empty.serve_forever, daemon=True).start()
        self.addCleanup(empty.server_close)
        self.addCleanup(empty.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", empty.server_address[1], timeout=10)
        self.addCleanup(conn.close)
        conn.request("GET", "/")
        body = conn.getresponse().read()
        self.assertIn(b"No boards are open.", body)
        self.assertEqual(body.count(hint), 1)
        self.open_board(self.other_project())
        self.assertEqual(self.request("GET", "/")[2].count(hint), 1)

    def test_opening_a_board_needs_a_board_file_and_this_computer(self):
        other = self.other_project()
        self.assertEqual(self.open_board(other, host="evil.example")[0], 403)
        self.assertEqual(self.request("POST", "/api/boards", json.dumps({"path": other}).encode(), {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.open_board("other/kanvas.json")[0], 400)
        notes = os.path.join(self.dir, "notes.json")
        with open(notes, "w") as f:
            f.write('{"hello": 1}')
        self.assertEqual(self.open_board(notes)[0], 400)
        self.assertEqual([b["name"] for b in json.loads(self.request("GET", "/api/boards")[2])["boards"]], [self.name])

    def test_opening_json_that_is_not_an_object_is_refused(self):
        null = os.path.join(self.dir, "null.json")
        with open(null, "w") as f:
            f.write("null")
        status, _, body = self.open_board(null)
        self.assertEqual((status, json.loads(body)["error"]), (400, f"{null} is not a Kanvas board"))

    def test_serve_in_another_project_opens_its_board_in_the_running_kanvas(self):
        other = self.other_project()
        code, out, err = self.run_cli("serve", "--no-open", "--port", str(self.port), cwd=os.path.dirname(other))
        self.assertIn(f"Opened in the Kanvas already running: http://127.0.0.1:{self.port}/other/", out)
        # The second project's own run sees its folder with symlinks resolved, as /private/var on macOS.
        self.assertEqual([os.path.realpath(b["path"]) for b in json.loads(self.request("GET", "/api/boards")[2])["boards"]],
                         [os.path.realpath(self.path), os.path.realpath(other)])

    def test_serve_finds_a_kanvas_that_took_a_later_port(self):
        """A Kanvas that took port + 2 while the ports below were busy is found once they are free again."""
        for _ in range(10):
            server = kanvas.start_server(self.path, "127.0.0.1", 0, quiet=True)
            port = server.server_address[1]
            if kanvas.port_free("127.0.0.1", port - 2) and kanvas.port_free("127.0.0.1", port - 1):
                break
            server.server_close()
        else:
            self.skipTest("no test port with two free ports below it")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        other = self.other_project()
        env = dict(os.environ, KANVAS_AGENT="")
        env.pop("KANVAS_BOARD", None)
        # A timeout, since a serve that misses the running Kanvas starts its own and keeps running.
        proc = subprocess.run([sys.executable, SCRIPT, "serve", "--no-open", "--port", str(port - 2)],
                              cwd=os.path.dirname(other), input=b"", capture_output=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8"))
        self.assertIn(f"Opened in the Kanvas already running: http://127.0.0.1:{port}/other/", proc.stdout.decode("utf-8"))
        self.assertEqual([b["name"] for b in server.board_list()], [self.name, "other"])
        self.assertTrue(kanvas.port_free("127.0.0.1", port - 2))

    def test_finding_a_kanvas_probes_only_taken_ports(self):
        """Connecting to a closed port takes seconds on Windows."""
        answer = mock.MagicMock()
        answer.__enter__.return_value.read.return_value = json.dumps({"kanvas": "9.9", "boards": []}).encode("utf-8")
        with mock.patch.object(kanvas, "port_free", side_effect=lambda host, p: p != 40019), \
                mock.patch.object(kanvas.LOCAL, "open", return_value=answer) as probe:
            self.assertEqual(kanvas.find_running(40000), ("http://127.0.0.1:40019/", "9.9"))
        self.assertEqual([c[0][0] for c in probe.call_args_list], ["http://127.0.0.1:40019/api/boards"])
        with mock.patch.object(kanvas, "port_free", return_value=True), mock.patch.object(kanvas.LOCAL, "open") as probe:
            self.assertIsNone(kanvas.find_running(40000))
        probe.assert_not_called()

    def phone_server(self):
        """A listener like the one `serve --tailscale` adds, here on another local port."""
        phone = kanvas.BoardServer(("127.0.0.1", 0), hosts=frozenset({"box.tail1.ts.net", "box", "100.64.0.7"}), quiet=True, share=self.server)
        threading.Thread(target=phone.serve_forever, daemon=True).start()
        self.addCleanup(phone.server_close)
        self.addCleanup(phone.shutdown)
        return phone.server_address[1]

    def phone_request(self, port, method, path, host, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=dict(headers or {}, Host=host))
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def test_a_phone_on_tailscale_sees_the_same_boards_by_this_computers_names_only(self):
        port = self.phone_server()
        for host in ("box.tail1.ts.net", f"box.tail1.ts.net:{port}", "box", "100.64.0.7:8765", "box.tail1.ts.net."):
            with self.subTest(host=host):
                self.assertEqual(self.phone_request(port, "GET", self.api, host), (200, self.raw()))
        for host in ("evil.example", "127.0.0.1", "localhost", "box.evil.example"):
            with self.subTest(host=host):
                self.assertEqual(self.phone_request(port, "GET", self.api, host)[0], 403)
        other = self.other_project()
        self.assertEqual(self.open_board(other)[0], 200)
        with open(other, "rb") as f:
            self.assertEqual(self.phone_request(port, "GET", "/other/api/board", "box"), (200, f.read()))
        board = json.loads(self.raw())
        board["tasks"][3]["state"] = "todo"
        status, _ = self.phone_request(port, "PUT", self.api, "box", json.dumps(board).encode("utf-8"),
                                       {"Content-Type": "application/json", "If-Match": kanvas.etag_of(self.raw())})
        self.assertEqual(status, 200)
        self.assertEqual(self.get("DV-04")["state"], "todo")

    def test_a_phone_cannot_open_boards(self):
        port = self.phone_server()
        body = json.dumps({"path": self.other_project()}).encode("utf-8")
        status, reply = self.phone_request(port, "POST", "/api/boards", "box", body, {"Content-Type": "application/json"})
        self.assertEqual(status, 403)
        self.assertIn("only from the computer running Kanvas", json.loads(reply)["error"])
        self.assertEqual([b["name"] for b in json.loads(self.request("GET", "/api/boards")[2])["boards"]], [self.name])

    def test_only_this_computer_sees_where_the_boards_live(self):
        other = self.other_project()
        self.open_board(other)
        path = self.path.encode("utf-8")
        self.assertEqual([b["path"] for b in json.loads(self.request("GET", "/api/boards")[2])["boards"]], [self.path, other])
        self.assertIn(path, self.request("GET", "/")[2])
        names_only = [{"name": self.name, "title": "Test"}, {"name": "other", "title": "Other"}]
        port = self.phone_server()
        status, body = self.phone_request(port, "GET", "/api/boards", "box")
        self.assertEqual((status, json.loads(body)["boards"]), (200, names_only))
        status, body = self.phone_request(port, "GET", "/", "box")
        self.assertEqual(status, 200)
        self.assertIn(b'href="/other/"', body)
        self.assertNotIn(path, body)
        # `tailscale serve` passes phones on to this computer's own address, by its Tailscale name.
        self.open_to_phone()
        self.assertEqual(json.loads(self.request("GET", "/api/boards", headers={"Host": "box.tail1.ts.net"})[2])["boards"], names_only)
        handler = kanvas.BoardHandler.__new__(kanvas.BoardHandler)
        for address, host, local in (("127.0.0.1", "127.0.0.1:8765", True), ("::1", "[::1]:8765", True), ("127.0.0.1", "localhost", True),
                                     ("100.64.0.9", "127.0.0.1:8765", False), ("127.0.0.1", "box.tail1.ts.net", False),
                                     ("192.168.1.5", "localhost:8765", False), ("127.0.0.1", "", False)):
            with self.subTest(address=address, host=host):
                handler.client_address, handler.headers = (address, 50000), {"Host": host}
                self.assertEqual(handler._from_this_computer(), local)

    def test_a_phone_is_not_told_where_a_board_it_could_not_save_lives(self):
        port = self.phone_server()
        board = json.loads(self.raw())
        board["tasks"][3]["state"] = "todo"
        body, headers = json.dumps(board).encode("utf-8"), {"Content-Type": "application/json", "If-Match": kanvas.etag_of(self.raw())}
        open(self.path + ".lock", "w").close()
        self.addCleanup(os.remove, self.path + ".lock")
        with mock.patch.object(kanvas, "LOCK_TIMEOUT", 0.2):
            status, reply = self.phone_request(port, "PUT", self.api, "box", body, headers)
            self.assertEqual((status, json.loads(reply)["error"]), (503, "The computer running Kanvas could not save the board"))
            status, _, reply = self.request("PUT", self.api, body, headers)
            self.assertEqual(status, 503)
            self.assertIn("lock file", json.loads(reply)["error"])

    def open_to_phone(self, magic=True):
        """POST to open the boards to phones, with Tailscale and its listener stood in for."""
        listener = mock.Mock()
        with mock.patch.object(kanvas, "tailscale_self", return_value=("100.64.0.7", "box.tail1.ts.net", magic)), \
                mock.patch.object(kanvas, "BoardServer", return_value=listener) as made:
            status, _, body = self.request("POST", f"/{self.name}/api/phone", b"{}", {"Content-Type": "application/json"})
        self.addCleanup(setattr, self.server, "phone_listener", None)
        return status, json.loads(body), made, listener

    def test_the_page_opens_the_boards_to_phones_and_gets_a_qr_code(self):
        self.assertEqual(json.loads(self.request("GET", f"/{self.name}/api/phone")[2]), {"urls": [], "qr": None, "manage": True})
        self.assertEqual(self.request("POST", f"/{self.name}/api/phone", b"{}", {"Content-Type": "text/plain"})[0], 415)
        status, info, made, listener = self.open_to_phone()
        self.assertEqual(status, 200)
        self.assertEqual(info["urls"], [f"http://box.tail1.ts.net:{self.port}/{self.name}/", f"http://100.64.0.7:{self.port}/{self.name}/"])
        self.assertTrue(info["qr"].startswith("<svg"))
        self.assertEqual(made.call_args[0][0], ("100.64.0.7", self.port))
        self.assertEqual(made.call_args[1]["hosts"], {"100.64.0.7", "box.tail1.ts.net", "box"})
        self.assertIs(made.call_args[1]["share"], self.server)
        self.assertEqual(json.loads(self.request("GET", f"/{self.name}/api/phone")[2])["urls"], info["urls"])
        # `tailscale serve` forwards requests for this computer's name to its own address.
        self.assertEqual(self.request("GET", self.api, headers={"Host": "box.tail1.ts.net"})[0], 200)
        self.assertEqual(self.request("GET", self.api, headers={"Host": "evil.example"})[0], 403)

    def test_without_magic_dns_phones_get_the_address_only(self):
        status, info, _, _ = self.open_to_phone(magic=False)
        self.assertEqual(info["urls"], [f"http://100.64.0.7:{self.port}/{self.name}/"])

    def test_opening_to_phones_says_what_tailscale_is_missing(self):
        with mock.patch.object(kanvas, "tailscale_self", side_effect=kanvas.KanvasError("Tailscale is not installed here.")):
            status, _, body = self.request("POST", f"/{self.name}/api/phone", b"{}", {"Content-Type": "application/json"})
        self.assertEqual((status, json.loads(body)["error"]), (503, "Tailscale is not installed here."))

    def test_a_phone_cannot_open_the_boards_to_phones(self):
        port = self.phone_server()
        status, _ = self.phone_request(port, "POST", f"/{self.name}/api/phone", "box", b"{}", {"Content-Type": "application/json"})
        self.assertEqual(status, 403)
        status, _ = self.phone_request(port, "DELETE", f"/{self.name}/api/phone", "box", b"{}", {"Content-Type": "application/json"})
        self.assertEqual(status, 403)
        self.assertIs(json.loads(self.phone_request(port, "GET", f"/{self.name}/api/phone", "box")[1])["manage"], False)

    def test_a_refusal_reads_the_body_first(self):
        """An answer sent with the request body unread lets Windows reset the connection and lose the answer."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(f"POST /{self.name}/api/phone HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-Forwarded-For: 100.64.0.9\r\nContent-Type: application/json\r\n"
                      "Content-Length: 2\r\n\r\n".encode("ascii"))
            s.settimeout(0.3)
            with self.assertRaises(socket.timeout):
                s.recv(1)
            s.settimeout(5)
            s.sendall(b"{}")
            reply = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                reply += chunk
        self.assertTrue(reply.startswith(b"HTTP/1.0 403"), reply[:40])
        self.assertIn(b"Only the computer running Kanvas can open it to phones", reply)

    def test_phones_that_come_through_tailscale_serve_cannot_open_boards_or_change_phone_access(self):
        """`tailscale serve` passes phones on to this computer's own address, by its Tailscale name and with X-Forwarded headers."""
        self.open_to_phone()
        other = self.other_project()
        body, json_type = json.dumps({"path": other}).encode("utf-8"), {"Content-Type": "application/json"}
        for headers in ({"Host": "box.tail1.ts.net"}, {"Host": f"box.tail1.ts.net:{self.port}"}, {"Host": "box"}, {"Host": "100.64.0.7"},
                        {"Host": f"127.0.0.1:{self.port}", "X-Forwarded-For": "100.64.0.9"}, {"Host": "localhost", "X-Forwarded-Host": "box.tail1.ts.net"}):
            with self.subTest(headers=headers):
                status, _, reply = self.request("POST", "/api/boards", body, dict(headers, **json_type))
                self.assertEqual((status, json.loads(reply)["error"]), (403, "Boards open only from the computer running Kanvas"))
                status, _, reply = self.request("POST", f"/{self.name}/api/phone", b"{}", dict(headers, **json_type))
                self.assertEqual((status, json.loads(reply)["error"]), (403, "Only the computer running Kanvas can open it to phones"))
                status, _, reply = self.request("DELETE", f"/{self.name}/api/phone", b"{}", dict(headers, **json_type))
                self.assertEqual((status, json.loads(reply)["error"]), (403, "Only the computer running Kanvas can close it to phones"))
                status, _, reply = self.request("GET", f"/{self.name}/api/phone", headers=headers)
                self.assertEqual((status, len(json.loads(reply)["urls"]), json.loads(reply)["manage"]), (200, 2, False))
                status, _, reply = self.request("GET", "/api/boards", headers=headers)
                self.assertEqual((status, json.loads(reply)["boards"]), (200, [{"name": self.name, "title": "Test"}]))
        self.assertEqual([b["name"] for b in self.server.board_list()], [self.name])
        self.assertTrue(self.server.phone)
        status, headers, data = self.request("GET", self.api, headers={"Host": "box.tail1.ts.net", "X-Forwarded-For": "100.64.0.9"})
        self.assertEqual((status, data), (200, self.raw()))
        board = json.loads(data)
        board["tasks"][3]["state"] = "todo"
        status, _, _ = self.request("PUT", self.api, json.dumps(board).encode("utf-8"),
                                    dict(json_type, Host="box.tail1.ts.net", **{"If-Match": headers["ETag"], "X-Forwarded-For": "100.64.0.9"}))
        self.assertEqual(status, 200)
        self.assertEqual(self.get("DV-04")["state"], "todo")
        # This computer itself still opens boards and opens and closes Kanvas to phones, by each of its names.
        self.assertIs(json.loads(self.request("GET", f"/{self.name}/api/phone")[2])["manage"], True)
        status, _, reply = self.open_board(other, host=f"localhost:{self.port}")
        self.assertEqual((status, json.loads(reply)["name"]), (200, "other"))
        status, _, reply = self.request("DELETE", f"/{self.name}/api/phone", b"{}", dict(json_type, Host="[::1]"))
        self.assertEqual((status, json.loads(reply)), (200, {"urls": [], "qr": None, "manage": True}))
        self.assertEqual(self.open_to_phone()[0], 200)

    def test_the_page_closes_the_boards_to_phones_again(self):
        _, _, _, listener = self.open_to_phone()
        self.assertEqual(self.request("DELETE", f"/{self.name}/api/phone", b"{}", {"Content-Type": "text/plain"})[0], 415)
        status, _, body = self.request("DELETE", f"/{self.name}/api/phone", b"{}", {"Content-Type": "application/json"})
        self.assertEqual((status, json.loads(body)), (200, {"urls": [], "qr": None, "manage": True}))
        listener.shutdown.assert_called_once_with()
        listener.server_close.assert_called_once_with()
        self.assertEqual(self.request("GET", self.api, headers={"Host": "box.tail1.ts.net"})[0], 403)
        self.assertEqual(self.request("GET", self.api)[0], 200)
        self.assertEqual(self.request("DELETE", f"/{self.name}/api/phone", b"{}", {"Content-Type": "application/json"})[0], 200)

    def test_qr_codes_hold_the_address(self):
        m = kanvas.qr_matrix("http://box.tail1.ts.net:8765/kanvas/")
        self.assertEqual(len(m), 29)  # version 3
        for x, y in ((0, 0), (len(m) - 7, 0), (0, len(m) - 7)):
            self.assertEqual([m[y][x + i] for i in range(7)], [True] * 7)  # finder patterns
        self.assertEqual(len(kanvas.qr_matrix("http://h/" + "x" * 300)), 69)  # version 13
        lines = kanvas.qr_text("http://h/").splitlines()
        self.assertEqual(len(lines), (21 + 5) // 2)

    def test_a_board_opened_in_a_kanvas_open_to_phones_gets_its_phone_addresses(self):
        self.server.phone = ["http://box.tail1.ts.net:8765/", "http://100.64.0.7:8765/"]
        status, _, body = self.open_board(self.other_project())
        self.assertEqual(json.loads(body)["phone"], ["http://box.tail1.ts.net:8765/other/", "http://100.64.0.7:8765/other/"])
        url = f"http://127.0.0.1:{self.port}/"
        self.assertEqual(kanvas.join_running(url, self.path), (url + f"{self.name}/", [f"http://box.tail1.ts.net:8765/{self.name}/", f"http://100.64.0.7:8765/{self.name}/"]))

    def browser_after_agent(self, op, *agent_args):
        """The page loads the board, an agent runs a command, then the page saves `op`.

        `op` is JavaScript for the change the human makes, with `seen(id)` giving a task as the page showed it.
        """
        code = (
            "const base = `http://127.0.0.1:${input.port}/${input.name}/`;\n"
            "const fetch = (url, opts) => globalThis.fetch(new URL(url, base), opts);\n"
            "(async () => {\n"
            "  const store = serverStore(), shown = await store.load();\n"
            "  const seen = (id) => shown.tasks.find((x) => x.id === id);\n"
            "  require('child_process').execFileSync(input.python, [input.script, ...input.agent], { cwd: input.dir });\n"
            f"  try {{ await store.save({op}, shown); process.stdout.write('saved'); }}\n"
            "  catch (e) { process.stdout.write(`refused: ${e.message}`); }\n"
            "})();"
        )
        sections = [("function serverStore()", "function fileStore("), ("function mustFind(", "\n// ----"), ("function inlineMd(", "\n// ----")]
        args = {"port": self.port, "name": self.name, "python": sys.executable, "script": SCRIPT, "dir": self.dir, "agent": list(agent_args)}
        return run_html_js(self, code, json.dumps(args), sections)

    def browser_move_after_agent(self, state, *agent_args):
        """The page loads the board, an agent runs a command, then the human moves DV-02 to `state`."""
        return self.browser_after_agent(f"ops.state('DV-02', seenOf(seen('DV-02')), {json.dumps(state)})", *agent_args)

    def test_a_browser_move_does_not_overwrite_an_agent_move(self):
        self.run_cli("start", "DV-02")
        out = self.browser_move_after_agent("todo", "finish", "DV-02", "-m", "Done; tests pass")
        self.assertEqual(out, "refused: DV-02 is in Review now, not Doing")
        self.assertEqual(self.get("DV-02")["state"], "review")

    def test_a_browser_move_is_replayed_over_other_agent_changes(self):
        out = self.browser_move_after_agent("doing", "note", "DV-03", "agent wrote first")
        self.assertEqual(out, "saved")
        self.assertEqual(self.get("DV-02")["state"], "doing")
        self.assertEqual(self.get("DV-03")["notes"][0]["text"], "agent wrote first")

    def test_approving_refuses_a_proposal_an_agent_revised(self):
        approve = "ops.state('DV-04', seenOf(seen('DV-04')), 'todo')"
        out = self.browser_after_agent(approve, "edit", "DV-04", "--desc", "Now it also rewrites the parser")
        self.assertEqual(out, "refused: DV-04 was revised since you opened it; read the new version first")
        self.assertEqual(self.get("DV-04")["state"], "proposed")
        self.run_cli("propose", "Development", "Base")
        self.assertIn("refused: DV-04 was revised", self.browser_after_agent(approve, "depend", "DV-04", "DV-05"))
        self.assertEqual(self.browser_after_agent(approve, "note", "DV-04", "Only a note"), "saved")
        self.assertEqual(self.get("DV-04")["state"], "todo")

    def test_approving_several_stops_at_a_revised_one(self):
        self.run_cli("propose", "Development", "Second")
        self.run_cli("propose", "Development", "Third")
        approve = "ops.approve(['DV-04', 'DV-05', 'DV-06'].map((id) => seenOf(seen(id))))"
        out = self.browser_after_agent(approve, "edit", "DV-05", "--title", "Second, reworded")
        self.assertEqual(out, "refused: DV-05 was revised since you looked; read the new version first")
        self.assertEqual([self.get(i)["state"] for i in ("DV-04", "DV-05", "DV-06")], ["proposed"] * 3)
        self.assertEqual(self.browser_after_agent(approve, "withdraw", "DV-06"), "saved")
        self.assertEqual([self.get(i)["state"] for i in ("DV-04", "DV-05")], ["todo", "todo"])

    def test_a_browser_edit_does_not_overwrite_an_agent_edit(self):
        edit = "ops.set('DV-04', { title: 'Mine' }, { title: seen('DV-04').title })"
        out = self.browser_after_agent(edit, "edit", "DV-04", "--title", "Theirs")
        self.assertEqual(out, "refused: the title of DV-04 changed meanwhile")
        self.assertEqual(self.get("DV-04")["title"], "Theirs")
        desc = "ops.set('DV-04', { desc: 'Mine' }, { desc: seen('DV-04').desc })"
        self.assertEqual(self.browser_after_agent(desc, "edit", "DV-04", "--desc", "Theirs"),
                         "refused: the description of DV-04 changed meanwhile")
        self.assertEqual(self.browser_after_agent(edit, "edit", "DV-04", "--desc", "Theirs, again"), "saved")
        self.assertEqual((self.get("DV-04")["title"], self.get("DV-04")["desc"]), ("Mine", "Theirs, again"))

    def test_a_ticked_box_follows_its_item_through_an_agent_edit(self):
        self.run_cli("edit", "DV-04", "--desc", "- [ ] a\n- [ ] b")
        tick = "ops.checkItem('DV-04', 'b', 0, true)"
        self.assertEqual(self.browser_after_agent(tick, "edit", "DV-04", "--desc", "Steps:\n\n- [ ] new\n- [ ] a\n- [ ] b"), "saved")
        self.assertEqual(self.get("DV-04")["desc"], "Steps:\n\n- [ ] new\n- [ ] a\n- [x] b")
        self.assertEqual(self.browser_after_agent(tick, "edit", "DV-04", "--desc", "- [ ] a\n- [ ] c"),
                         "refused: That checklist item in DV-04 changed meanwhile")
        self.assertEqual(self.get("DV-04")["desc"], "- [ ] a\n- [ ] c")

    def test_browser_writes_are_canonical(self):
        board = json.loads(self.raw())
        board["tasks"][3]["state"] = "todo"
        status, _, _ = self.request("PUT", self.api, json.dumps(board, indent=None).encode("utf-8"),
                                    {"Content-Type": "application/json", "If-Match": kanvas.etag_of(self.raw())})
        self.assertEqual(status, 200)
        text = self.raw().decode("utf-8")
        self.assertEqual(text, kanvas.dumps(kanvas.parse_board(text)))


class RememberTests(TempBoard):
    """A Kanvas started again reopens the boards and the phone access it had."""

    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.dir, "state", "boards.json")

    def start(self, path=None):
        server = kanvas.start_server(path, "127.0.0.1", 0, quiet=True, remember=self.state)
        self.addCleanup(server.server_close)
        return server

    def other(self, folder):
        path = os.path.join(self.dir, folder, "kanvas.json")
        os.makedirs(os.path.dirname(path))
        write_board(path, self.initial_board())
        return path

    def remembered(self):
        with open(self.state, encoding="utf-8") as f:
            return json.load(f)

    def test_closing_the_last_board_is_remembered_and_reopening_cancels_shutdown(self):
        server = self.start(self.path)
        name = kanvas.board_name(self.path)
        self.assertTrue(server.remove_board(name))
        self.assertEqual(self.remembered()["boards"], [])
        self.assertIsNotNone(server.stop_at)
        self.assertTrue(os.path.isfile(self.path))
        self.assertEqual(self.start().board_list(), [])
        server.add_board(self.path)
        self.assertIsNone(server.stop_at)
        self.assertEqual(self.remembered()["boards"], [self.path])

    def test_missing_files_need_to_stay_missing_and_bad_json_stays_open(self):
        server = self.start(self.path)
        other = self.other("other")
        server.add_board(other)
        name = kanvas.board_name(self.path)
        moved = self.path + ".moved"
        os.rename(self.path, moved)
        with mock.patch.object(kanvas.time, "monotonic", return_value=10):
            server.service_actions()
        self.assertEqual(server.board_path(name), self.path)
        os.rename(moved, self.path)
        with mock.patch.object(kanvas.time, "monotonic", return_value=20):
            server.service_actions()
        self.assertNotIn(name, server.missing_boards)
        with open(self.path, "w") as f:
            f.write("not JSON")
        server.service_actions()
        self.assertEqual(server.board_path(name), self.path)
        with mock.patch.object(kanvas.os, "stat", side_effect=PermissionError):
            server.service_actions()
        self.assertEqual(server.board_path(name), self.path)
        os.remove(self.path)
        with mock.patch.object(kanvas.time, "monotonic", return_value=30):
            server.service_actions()
        with mock.patch.object(kanvas.time, "monotonic", return_value=33):
            server.service_actions()
        self.assertIsNone(server.board_path(name))
        self.assertEqual(self.remembered()["boards"], [other])
        self.assertIsNone(server.stop_at)

    def test_open_boards_come_back_after_a_restart(self):
        first = self.start(self.path)
        other, gone = self.other("other"), self.other("gone")
        first.add_board(other)
        first.add_board(gone)
        self.assertEqual(self.remembered(), {"boards": [self.path, other, gone], "phone": False,
                                            "names": {kanvas.board_name(path): os.path.normcase(os.path.realpath(path))
                                                      for path in (self.path, other, gone)}})
        first.server_close()
        os.remove(gone)
        again = self.start()
        self.assertEqual([(b["name"], b["path"]) for b in again.board_list()], [(kanvas.board_name(self.path), self.path), ("other", other)])
        self.assertFalse(again.phone_wanted)
        self.assertEqual(self.remembered()["boards"], [self.path, other])

    def test_closed_names_and_collision_suffixes_survive_restarts(self):
        first, second, third = [self.other(os.path.join(parent, "project")) for parent in ("a", "b", "c")]
        server = self.start(self.path)
        self.assertEqual(server.add_board(first), "project")
        self.assertEqual(server.add_board(second), "project-2")
        server.remove_board("project")
        server.server_close()
        again = self.start()
        self.assertIsNone(again.board_path("project"))
        self.assertEqual(again.board_path("project-2"), second)
        self.assertEqual(again.add_board(third), "project-3")
        self.assertEqual(again.add_board(first), "project")
        self.assertEqual(again.board_path("project"), first)

    def test_deleted_board_names_stay_reserved_after_restart(self):
        original, replacement = [self.other(os.path.join(parent, "project")) for parent in ("a", "b")]
        server = self.start(self.path)
        server.add_board(original)
        os.remove(original)
        with mock.patch.object(kanvas.time, "monotonic", return_value=10):
            server.service_actions()
        with mock.patch.object(kanvas.time, "monotonic", return_value=13):
            server.service_actions()
        self.assertIsNone(server.board_path("project"))
        server.server_close()
        again = self.start()
        self.assertEqual(again.add_board(replacement), "project-2")
        self.assertIsNone(again.board_path("project"))

    def test_legacy_state_reserves_missing_boards_before_restoring_others(self):
        first, second = [self.other(os.path.join(parent, "project")) for parent in ("a", "b")]
        os.remove(first)
        os.makedirs(os.path.dirname(self.state))
        with open(self.state, "w") as f:
            json.dump({"boards": [first, second], "phone": False}, f)
        server = self.start()
        self.assertIsNone(server.board_path("project"))
        self.assertEqual(server.board_path("project-2"), second)

    def test_bad_name_reservations_do_not_break_restore(self):
        os.makedirs(os.path.dirname(self.state))
        for names in (None, [], "bad", {"api": self.path, "../bad": self.path, ".": self.path,
                                        "relative": "relative.json", "number": 5, "null": None}):
            with self.subTest(names=names):
                with open(self.state, "w") as f:
                    json.dump({"boards": [self.path], "names": names}, f)
                server = self.start()
                self.assertEqual(server.board_names, {kanvas.board_name(self.path): os.path.normcase(os.path.realpath(self.path))})
                self.assertEqual([b["path"] for b in server.board_list()], [self.path])
                server.server_close()

    def test_a_board_served_again_keeps_its_place(self):
        other = self.other("other")
        self.start(self.path).add_board(other)
        self.assertEqual([b["path"] for b in self.start(other).board_list()], [self.path, other])

    def test_only_boards_are_reopened(self):
        notes = os.path.join(self.dir, "notes.json")
        with open(notes, "w") as f:
            f.write('{"hello": 1}')
        for state in ('{"boards": [%s, 7, null], "phone": "yes"}' % json.dumps(notes), "[]", "not json", '{"boards": "x"}'):
            with self.subTest(state=state):
                os.makedirs(os.path.dirname(self.state), exist_ok=True)
                with open(self.state, "w") as f:
                    f.write(state)
                server = self.start()
                self.assertEqual(server.board_list(), [])
                self.assertFalse(server.phone_wanted)
                server.server_close()

    def test_json_that_is_not_an_object_is_not_reopened(self):
        files = []
        for text in ("null", "7", "true"):
            files.append(os.path.join(self.dir, f"{text}.json"))
            with open(files[-1], "w") as f:
                f.write(text)
        os.makedirs(os.path.dirname(self.state))
        with open(self.state, "w") as f:
            json.dump({"boards": files + [self.path]}, f)
        self.assertEqual([b["path"] for b in self.start().board_list()], [self.path])

    def test_phone_access_is_remembered(self):
        server = self.start(self.path)
        with mock.patch.object(kanvas, "tailscale_self", return_value=("100.64.0.7", "box.tail1.ts.net", True)), \
                mock.patch.object(kanvas, "BoardServer"):
            server.open_to_phone()
        self.assertTrue(self.remembered()["phone"])
        self.assertTrue(self.start().phone_wanted)
        server.close_to_phone()
        self.assertFalse(self.remembered()["phone"])

    def test_phone_access_waits_for_tailscale(self):
        server = self.start(self.path)
        answers = [kanvas.KanvasError("Tailscale is not connected (Stopped)."), ("100.64.0.7", "box.tail1.ts.net", True)]
        with mock.patch.object(kanvas, "tailscale_self", side_effect=answers), mock.patch.object(kanvas, "BoardServer"):
            server.open_to_phone_soon(tries=3, wait=0.01)
            for _ in range(200):
                if server.phone:
                    break
                time.sleep(0.01)
        self.assertEqual(server.phone[-1], f"http://100.64.0.7:{server.server_address[1]}/")
        server.phone_listener = None

    def test_the_state_lives_in_the_users_own_folders(self):
        cases = (("posix", "linux", {"XDG_STATE_HOME": "/x/state"}, "/x/state/kanvas/boards.json"),
                 ("posix", "linux", {}, os.path.expanduser("~/.local/state/kanvas/boards.json")),
                 ("posix", "darwin", {}, os.path.expanduser("~/Library/Application Support/Kanvas/boards.json")),
                 ("nt", "win32", {"LOCALAPPDATA": "C:\\Users\\a\\AppData\\Local"}, os.path.join("C:\\Users\\a\\AppData\\Local", "Kanvas", "boards.json")),
                 ("posix", "linux", {"KANVAS_STATE_DIR": "/elsewhere"}, "/elsewhere/boards.json"))
        for name, platform, env, expected in cases:
            with self.subTest(platform=platform, env=env), mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(kanvas.os, "name", name), mock.patch.object(kanvas.sys, "platform", platform):
                if "KANVAS_STATE_DIR" not in env:
                    os.environ.pop("KANVAS_STATE_DIR")
                if not env:
                    os.environ.pop("XDG_STATE_HOME", None)
                # Run on Windows, the other systems' paths come out with backslashes.
                self.assertEqual(os.path.normpath(kanvas.state_file()), os.path.normpath(expected))


class ServiceTests(unittest.TestCase):
    """`kanvas service` starts `kanvas serve --saved` at login on each system."""

    CMD = ["/home/a b/.local/bin/kanvas", "serve", "--saved", "--no-open"]

    def test_the_systemd_unit_quotes_the_command(self):
        unit = kanvas.systemd_unit(["/x/100% sure/kanvas", 'say "hi"'])
        self.assertIn('ExecStart="/x/100%% sure/kanvas" "say \\"hi\\""\n', unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("WantedBy=default.target", unit)

    def test_the_launch_agent_runs_at_login(self):
        import plistlib
        plist = plistlib.loads(kanvas.launchd_plist(self.CMD))
        self.assertEqual((plist["Label"], plist["ProgramArguments"], plist["RunAtLoad"]), ("kanvas", self.CMD, True))

    def test_the_windows_task_runs_at_logon(self):
        import xml.etree.ElementTree as ET
        with mock.patch.dict(os.environ, {"USERDOMAIN": "PC", "USERNAME": "a&b"}):
            xml = kanvas.windows_task(["C:\\Py thon\\pythonw.exe", "C:\\k\\kanvas.py", "serve", "--saved"])
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        root = ET.fromstring(xml.split("\n", 1)[1])
        self.assertEqual(root.find(".//t:LogonTrigger/t:UserId", ns).text, "PC\\a&b")
        self.assertEqual(root.find(".//t:Exec/t:Command", ns).text, "C:\\Py thon\\pythonw.exe")
        self.assertEqual(root.find(".//t:Exec/t:Arguments", ns).text, "C:\\k\\kanvas.py serve --saved")

    def test_a_kanvas_run_by_uvx_is_not_installed_as_a_service(self):
        with mock.patch.object(kanvas, "INSTALLED", True), \
                mock.patch.object(kanvas.sys, "prefix", os.path.join(os.sep, "c", "uv", "archive-v0", "x")):
            with self.assertRaisesRegex(kanvas.KanvasError, "uv tool install kanvas-board"):
                kanvas.service_command()

    @unittest.skipIf(os.name == "nt" or sys.platform == "darwin", "the systemd service")
    def test_install_and_uninstall_on_linux(self):
        config = tempfile.mkdtemp(prefix="kanvas-config-")
        self.addCleanup(shutil.rmtree, config, True)
        unit = os.path.join(config, "systemd", "user", "kanvas.service")
        out = io.StringIO()
        running = ("http://127.0.0.1:40000/", kanvas.VERSION)
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": config}), \
                mock.patch.object(kanvas, "run_quiet", return_value=(0, "")) as run, \
                mock.patch.object(kanvas, "find_running", side_effect=[None, running, None]) as find, \
                mock.patch.object(sys, "stdout", out):
            calls = mock.Mock()
            calls.attach_mock(run, "run")
            calls.attach_mock(find, "find")
            self.assertEqual(kanvas.main(["service", "install", "--tailscale"]), 0)
            with open(unit) as f:
                self.assertIn("serve --saved --no-open", f.read().replace('"', ""))
            # The search for a Kanvas started by hand comes after the service's own one stops, and before it starts again.
            self.assertEqual(calls.mock_calls, [mock.call.run(["systemctl", "--user", "stop", "kanvas.service"]),
                                                mock.call.find(kanvas.DEFAULT_PORT),
                                                mock.call.run(["systemctl", "--user", "daemon-reload"]),
                                                mock.call.run(["systemctl", "--user", "enable", "--now", "kanvas.service"]),
                                                mock.call.find(kanvas.DEFAULT_PORT)])
            self.assertIn("Open http://127.0.0.1:40000/\n", out.getvalue())
            with open(kanvas.state_file()) as f:
                self.assertTrue(json.load(f)["phone"])
            run.return_value = (1, "Failed to connect to bus")
            with self.assertRaisesRegex(kanvas.KanvasError, "Failed to connect to bus"):
                kanvas.service_install(False)
            run.return_value = (0, "")
            self.assertEqual(kanvas.main(["service", "uninstall"]), 0)
            self.assertFalse(os.path.exists(unit))
        os.remove(kanvas.state_file())
        self.assertIn("Kanvas now starts when you log in", out.getvalue())
        self.assertIn("Kanvas no longer starts when you log in.", out.getvalue())

    @contextlib.contextmanager
    def system(self, port):
        """Service files in a temporary folder, system commands that succeed without running, and Kanvas's ports from `port` on."""
        folder = tempfile.mkdtemp(prefix="kanvas-config-")
        self.addCleanup(shutil.rmtree, folder, True)
        with mock.patch.object(kanvas, "service_paths", return_value=os.path.join(folder, "kanvas.service")), \
                mock.patch.object(kanvas, "run_quiet", return_value=(0, "")), \
                mock.patch.object(kanvas, "DEFAULT_PORT", port), mock.patch.object(sys, "stdout", io.StringIO()) as out:
            yield out

    def kanvas_above_free_ports(self):
        """Start a Kanvas on a port with the two ports below it free, as one that took port + 2; return its port."""
        for _ in range(10):
            server = kanvas.start_server(None, "127.0.0.1", 0, quiet=True)
            port = server.server_address[1]
            if kanvas.port_free("127.0.0.1", port - 2) and kanvas.port_free("127.0.0.1", port - 1):
                break
            server.server_close()
        else:
            self.skipTest("no test port with two free ports below it")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return port

    def test_status_sees_a_kanvas_on_any_of_its_ports(self):
        port = self.kanvas_above_free_ports()
        with self.system(port - 2) as out:
            self.assertEqual(kanvas.main(["service", "status"]), 0)
        self.assertIn(f"Running: yes, at http://127.0.0.1:{port}/\n", out.getvalue())

    def test_install_waits_for_the_service_to_answer(self):
        running = ("http://127.0.0.1:40002/", kanvas.VERSION)
        with self.system(40000) as out, mock.patch.object(kanvas, "find_running", side_effect=[None, None, None, running]) as find:
            self.assertEqual(kanvas.main(["service", "install"]), 0)
        self.assertEqual(find.call_args_list, [mock.call(40000)] * 4)
        self.assertIn("Open http://127.0.0.1:40002/\n", out.getvalue())
        with self.system(40000) as out, mock.patch.object(kanvas, "find_running", return_value=None):
            kanvas.service_install(False, wait=0)
        self.assertIn("It has not answered yet; `kanvas service status` says when it runs.\n", out.getvalue())

    def test_install_says_when_a_kanvas_started_by_hand_keeps_the_boards(self):
        """The service's `serve --saved` would join that Kanvas and exit, so nothing serves the boards once it stops."""
        port = self.kanvas_above_free_ports()
        for args, again in (([], "kanvas service install"), (["--tailscale"], "kanvas service install --tailscale")):
            with self.subTest(args=args), self.system(port - 2) as out:
                self.assertEqual(kanvas.main(["service", "install", *args]), 0)
                text = out.getvalue()
                self.assertIn(f"Kanvas already runs at http://127.0.0.1:{port}/, started with `kanvas serve`", text)
                self.assertIn(f"stop it (Ctrl+C in its terminal) and run `{again}` again.", text)
                self.assertNotIn("Open ", text)
        os.remove(kanvas.state_file())



class Terminal(io.StringIO):
    def isatty(self):
        return True


class ServeWithoutBoardTests(unittest.TestCase):
    """serve offers to create a missing board, and only asks a person at the terminal."""

    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="kanvas-serve-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.addCleanup(os.chdir, os.getcwd())
        if kanvas.find_board(self.dir):
            self.skipTest("a kanvas.json above the temporary folder would be served")

    def project(self):
        """A fresh git project, with the current folder in its src/ subfolder."""
        top = tempfile.mkdtemp(dir=self.dir)
        os.mkdir(os.path.join(top, ".git"))
        os.mkdir(os.path.join(top, "src"))
        os.chdir(os.path.join(top, "src"))
        return top

    def serve(self, answer, *args, env=None, start=None):
        """Runs `serve` at a pretend terminal, answering its question with `answer` (text or an exception).
        `start` stands in for start_server."""
        self.server = mock.Mock(server_address=("127.0.0.1", 8765), check_host=True, phone=[], quiet=False, phone_wanted=False,
                                **{"add_board.return_value": "project", "board_list.return_value": []})
        stdin, stdout, stderr = mock.Mock(), Terminal(), io.StringIO()
        stdin.isatty.return_value = True
        reply = {"return_value": answer} if isinstance(answer, str) else {"side_effect": answer}
        started = {"side_effect": start} if start else {"return_value": self.server}
        with mock.patch.dict(os.environ), mock.patch("builtins.input", **reply) as self.ask, \
                mock.patch.object(kanvas, "start_server", **started) as self.start, \
                mock.patch.object(kanvas, "find_running", return_value=None), \
                mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout), \
                mock.patch.object(sys, "stderr", stderr):
            os.environ.pop("KANVAS_BOARD", None)
            os.environ.update(env or {})
            code = kanvas.main(["serve", "--no-open", *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_without_a_terminal_serve_names_init_and_creates_nothing(self):
        top = self.project()
        env = dict(os.environ)
        env.pop("KANVAS_BOARD", None)
        proc = subprocess.run([sys.executable, SCRIPT, "serve", "--no-open", "--port", "0"],
                              cwd=os.path.join(top, "src"), input=b"", capture_output=True, env=env, timeout=60)
        err = proc.stderr.decode("utf-8")
        self.assertEqual(proc.returncode, 1, err)
        self.assertIn("No kanvas.json found", err)
        self.assertIn(" init", err)
        self.assertNotIn("[Y/n]", proc.stdout.decode("utf-8"))
        self.assertEqual(sorted(os.listdir(top)), [".git", "src"])
        self.assertEqual(os.listdir(os.path.join(top, "src")), [])

    def test_at_a_terminal_yes_sets_up_the_project_and_serves_it(self):
        for answer in ("", "y", "YES"):
            with self.subTest(answer=answer):
                top = self.project()
                code, out, err = self.serve(answer)
                self.assertEqual(code, 0, err)
                self.ask.assert_called_once_with(f"No kanvas.json here. Create one in {top}? [Y/n] ")
                board = os.path.join(top, "kanvas.json")
                self.assertTrue(os.path.isfile(board))
                self.assertTrue(os.path.isfile(os.path.join(top, ".agents", "skills", "kanvas", "SKILL.md")))
                self.assertIn(f"Kanvas {kanvas.VERSION} in {top}, the top folder of this git repository", out)
                self.assertIn("created   kanvas.json", out)
                self.assertNotIn("Open the board:", out)
                self.assertIn(f"Kanvas board: {board}", out)
                self.assertEqual(self.start.call_args[0][0], board)
                self.server.serve_forever.assert_called_once_with()

    def test_at_a_terminal_no_creates_nothing(self):
        for answer in ("n", "No", EOFError, KeyboardInterrupt):
            with self.subTest(answer=answer):
                top = self.project()
                code, out, err = self.serve(answer)
                self.assertEqual(code, 1, out)
                self.assertIn("No kanvas.json found", err)
                self.assertEqual(sorted(os.listdir(top)), [".git", "src"])
                self.start.assert_not_called()

    def test_a_missing_board_given_by_name_is_not_offered(self):
        self.project()
        for args, env in ((["--board", "missing.json"], None), ([], {"KANVAS_BOARD": "missing.json"})):
            with self.subTest(args=args, env=env):
                code, _, err = self.serve("y", *args, env=env)
                self.assertEqual(code, 1)
                self.assertIn("Board not found", err)
                self.ask.assert_not_called()
                self.assertFalse(os.path.exists("missing.json"))

    def test_tailscale_serves_on_the_tailscale_address_and_prints_the_phone_addresses(self):
        top = self.project()
        write_board(os.path.join(top, "kanvas.json"), {"title": "", "groups": [], "tasks": []})
        name = os.path.basename(top)
        for magic, expected in ((True, f"On your phone (Tailscale): http://box.tail1.ts.net:8765/{name}/  or  http://100.64.0.7:8765/{name}/"),
                                (False, f"On your phone (Tailscale): http://100.64.0.7:8765/{name}/\n")):
            with self.subTest(magic=magic), \
                    mock.patch.object(kanvas, "tailscale_self", return_value=("100.64.0.7", "box.tail1.ts.net", magic)):
                code, out, err = self.serve("n", "--tailscale", start=self.real_server)
                self.assertEqual(code, 0, err)
                self.assertIn(expected, out)
                self.assertIn("Scan it with your phone's camera:", out)
                self.assertIn("\u2580", out)
                self.assertIn("Anyone on your Tailscale network can open and edit your boards.", out)
                self.assertEqual(self.made.call_args[0][0], ("100.64.0.7", 8765))
                self.made.return_value.server_close.assert_called_once_with()
                self.made_patch.stop()

    def real_server(self, path, host, port, remember=None):
        """A BoardServer that does not listen, whose phone listener is a stand-in."""
        with mock.patch.object(socketserver.TCPServer, "server_bind"), mock.patch.object(socketserver.TCPServer, "server_activate"):
            server = kanvas.BoardServer((host, port), path, quiet=True)
        server.server_address = ("127.0.0.1", 8765)
        server.phone_wanted = False
        server.serve_forever = lambda: None
        self.made_patch = mock.patch.object(kanvas, "BoardServer")
        self.made = self.made_patch.start()
        self.addCleanup(mock.patch.stopall)
        return server

    def test_saved_serves_the_remembered_boards_without_a_project(self):
        os.chdir(self.dir)
        code, out, err = self.serve("n", "--saved")
        self.assertEqual(code, 0, err)
        self.ask.assert_not_called()
        self.assertIsNone(self.start.call_args[0][0])
        self.assertEqual(self.start.call_args[1]["remember"], kanvas.state_file())
        self.assertIn("Open http://127.0.0.1:8765/  (Ctrl+C to stop)", out)
        with mock.patch.object(kanvas, "find_running", return_value=("http://127.0.0.1:8765/", kanvas.VERSION)), \
                mock.patch.object(sys, "stdout", io.StringIO()) as stdout:
            self.assertEqual(kanvas.main(["serve", "--saved", "--no-open"]), 0)
        self.assertEqual(stdout.getvalue(), "Kanvas already runs at http://127.0.0.1:8765/\n")

    def test_tailscale_and_host_do_not_mix(self):
        top = self.project()
        write_board(os.path.join(top, "kanvas.json"), {"title": "", "groups": [], "tasks": []})
        code, _, err = self.serve("n", "--tailscale", "--host", "0.0.0.0")
        self.assertEqual(code, 1)
        self.assertIn("--tailscale or --host, not both", err)
        self.start.assert_not_called()


class TailscaleTests(unittest.TestCase):
    def status(self, info, which="/usr/bin/tailscale"):
        out = mock.Mock(stdout=json.dumps(info).encode("utf-8") if isinstance(info, dict) else info)
        with mock.patch("shutil.which", return_value=which), mock.patch("os.path.isfile", return_value=False), \
                mock.patch("subprocess.run", return_value=out) as run:
            result = kanvas.tailscale_self()
        self.assertEqual(run.call_args[0][0], [which, "status", "--json"])
        return result

    def test_reads_this_computers_address_and_name(self):
        info = {"BackendState": "Running", "Self": {"DNSName": "Box.tail1.ts.net.", "TailscaleIPs": ["fd7a:115c:a1e0::7", "100.64.0.7"]},
                "CurrentTailnet": {"MagicDNSEnabled": True}}
        self.assertEqual(self.status(info), ("100.64.0.7", "box.tail1.ts.net", True))
        info["CurrentTailnet"]["MagicDNSEnabled"] = False
        self.assertEqual(self.status(info), ("100.64.0.7", "box.tail1.ts.net", False))
        self.assertEqual(kanvas.tailscale_names("100.64.0.7", "box.tail1.ts.net"), {"100.64.0.7", "box.tail1.ts.net", "box"})

    def test_says_what_is_missing(self):
        with mock.patch("shutil.which", return_value=None), mock.patch("os.path.isfile", return_value=False):
            with self.assertRaisesRegex(kanvas.KanvasError, "Tailscale is not installed"):
                kanvas.tailscale_self()
        with self.assertRaisesRegex(kanvas.KanvasError, r"not connected \(Stopped\)"):
            self.status({"BackendState": "Stopped", "Self": {}})
        with self.assertRaisesRegex(kanvas.KanvasError, "did not say"):
            self.status(b"failed to connect to local tailscaled")
        with self.assertRaisesRegex(kanvas.KanvasError, "no IPv4 address"):
            self.status({"BackendState": "Running", "Self": {"TailscaleIPs": ["fd7a:115c:a1e0::7"]}})


def page_css():
    with open(os.path.join(ROOT, "kanvas.html"), encoding="utf-8") as f:
        html = f.read()
    return html[html.index("<style>") + 7:html.index("</style>")]


def css_block(css, head):
    """The text of the at-rule starting with `head`, through its closing brace."""
    start = css.index(head)
    i, depth = css.index("{", start) + 1, 1
    while depth:
        depth += {"{": 1, "}": -1}.get(css[i], 0)
        i += 1
    return css[start:i]


def css_args(text):
    """`text` split at its top-level commas."""
    parts, depth, cur = [], 0, ""
    for c in text:
        depth += {"(": 1, ")": -1}.get(c, 0)
        if c == "," and not depth:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += c
    return parts + [cur.strip()]


def css_rules(css):
    """(selectors, declarations) of each innermost rule; keyframe steps come out as rules too."""
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        sel = re.sub(r"/\*.*?\*/", "", sel, flags=re.S).strip()
        yield [p for p in css_args(sel) if p], body


def css_rule(css, selector):
    """The declarations of the rules outside at-rules whose selector list is `selector`, later ones winning."""
    while "@media" in css:
        css = css.replace(css_block(css, "@media"), "")
    found = [body for sels, body in css_rules(css) if ", ".join(sels) == selector]
    if not found:
        raise KeyError(selector)
    return {k.strip(): v.strip() for body in found for k, _, v in (d.partition(":") for d in body.split(";")) if v}


def theme_env(css):
    """The custom properties of the light and the dark theme, as written."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    pick = lambda head: {k: v.strip() for k, v in re.findall(r"(--[\w-]+):\s*([^;{}]+);", css_block(css, head))}
    light = pick(":root {")
    return {"light": light, "dark": {**light, **pick('[data-theme="dark"] {')}}


def css_color(value, env):
    """(r, g, b, alpha) of a color written with hex, rgb(), transparent, var() and color-mix(in srgb, ...)."""
    v = value.strip()
    if v.startswith("var("):
        name, _, fallback = v[4:-1].partition(",")
        return css_color(env.get(name.strip(), fallback), env)
    if v.startswith("color-mix("):
        space, *args = css_args(v[10:-1])
        assert space == "in srgb", space
        parts = []
        for arg in args:
            m = re.match(r"(.+?)\s+(var\([^()]*\)|[\d.]+%)$", arg)
            color, share = (m.group(1), m.group(2)) if m else (arg, None)
            share = env[share[4:-1]] if share and share.startswith("var(") else share
            parts.append((css_color(color, env), float(share[:-1]) / 100 if share else None))
        (a, p), (b, q) = parts
        p = p if p is not None else 1 - q if q is not None else .5
        q = 1 - p if q is None else q
        alpha = a[3] * p + b[3] * q
        return tuple((x * a[3] * p + y * b[3] * q) / alpha if alpha else 0 for x, y in zip(a, b))[:3] + (alpha,)
    if v == "transparent":
        return (0, 0, 0, 0)
    m = re.match(r"rgb\((\d+) (\d+) (\d+)(?: / ([\d.]+))?\)$", v)
    if m:
        return tuple(float(x) for x in m.groups()[:3]) + (float(m.group(4) or 1),)
    h = v.lstrip("#")
    h = "".join(c * 2 for c in h) if len(h) == 3 else h
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) + (1,)


def over(top, under):
    return tuple(x * top[3] + y * (1 - top[3]) for x, y in zip(top[:3], under[:3])) + (1,)


def theme_colors(css):
    """The hex color variables of the light and the dark theme."""
    pick = lambda head: dict(re.findall(r"--([\w-]+):\s*(#[0-9a-f]{3,6})\b", css_block(css, head)))
    light = pick(":root {")
    return {"light": light, "dark": {**light, **pick('[data-theme="dark"] {')}}


def linear_rgb(color):
    if isinstance(color, str):
        h = color.lstrip("#")
        h = "".join(c * 2 for c in h) if len(h) == 3 else h
        color = [int(h[i:i + 2], 16) for i in (0, 2, 4)]
    return [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in (c / 255 for c in color[:3])]


def contrast(a, b):
    """The WCAG contrast ratio of two colors."""
    lum = lambda c: sum(w * v for w, v in zip((.2126, .7152, .0722), linear_rgb(c)))
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + .05) / (lo + .05)


def oklab_distance(a, b):
    def lab(c):
        r, g, b = linear_rgb(c)
        l, m, s = (x ** (1 / 3) for x in (.4122214708 * r + .5363325363 * g + .0514459929 * b,
                                          .2119034982 * r + .6806995451 * g + .1073969566 * b,
                                          .0883024619 * r + .2817188376 * g + .6299787005 * b))
        return (.2104542553 * l + .7936177850 * m - .0040720468 * s,
                1.9779984951 * l - 2.4285922050 * m + .4505937099 * s,
                .0259040371 * l + .7827717662 * m - .8086757660 * s)
    return sum((x - y) ** 2 for x, y in zip(lab(a), lab(b))) ** .5


class StyleTests(unittest.TestCase):
    MOVES = r"\b(scale|translate|rotate|transform|background-position|stroke-dashoffset|flex-grow)\b"

    def test_reduced_motion_holds_everything_still(self):
        css = page_css()
        still = css_block(css, "@media (prefers-reduced-motion: reduce)")
        rest = css.replace(still, "")
        keyframes = set(re.findall(r"@keyframes ([\w-]+)", rest))
        compound = lambda s: set(re.findall(r":is\([^)]*\)|::?[\w-]+|[.#][\w-]+|^[a-z]+", s))
        # A rule in the reduced-motion block covers a selector when it names a part of everything the selector names.
        covered = lambda sel, prop: any(compound(s) <= compound(sel) for sels, body in css_rules(still) if re.search(prop, body) for s in sels)
        for sels, body in css_rules(rest):
            for value in re.findall(r"(?:^|;)\s*animation:([^;]+)", body):
                name = next((w for w in value.split() if w in keyframes), None)
                for sel in sels if name else ():
                    with self.subTest(selector=sel, animation=name):
                        if "infinite" in value:
                            self.assertTrue(covered(sel, r"animation:\s*none"), "loops must stop")
                        else:
                            # One-shot animations keep their name, so animationend still clears the classes that start them.
                            self.assertIn(f"@keyframes {name} ", still)
                            self.assertNotRegex(css_block(still, f"@keyframes {name} "), self.MOVES)
            for value in re.findall(r"(?:^|;)\s*transition:([^;]+)", body):
                if re.search(self.MOVES, value):
                    for sel in sels:
                        with self.subTest(selector=sel, transition=value):
                            self.assertTrue(covered(sel, r"transition:(?![^;]*" + self.MOVES + ")"), "moving transitions must stop")

    def test_groups_past_the_tenth_get_colors_of_their_own(self):
        first_ten = ["#6366f1", "#0ea5e9", "#14b8a6", "#f59e0b", "#ec4899", "#84cc16", "#a855f7", "#f43f5e", "#06b6d4", "#78716c"]
        agents = ["claude", "codex", "cursor", "cursor-visual", "gemini", "aider"]
        out = json.loads(run_html_js(self, "console.log(JSON.stringify([GROUP_COLORS, input.map(colorFor)]))", json.dumps(agents)))
        groups, agent_colors = out
        self.assertEqual(groups[:10], first_ten, "boards keep their groups' colors")
        self.assertGreaterEqual(len(groups), 12)
        self.assertEqual(len(set(groups)), len(groups))
        for name, color in zip(agents, agent_colors):
            n = 0
            for ch in name:
                n = (n * 31 + ord(ch)) & 0xFFFFFFFF
            self.assertEqual(color, first_ten[n % 10], f"{name} keeps its color")
        themes = theme_colors(page_css())
        states = [t[k] for t in themes.values() for k in ("proposed", "todo", "blocked", "doing", "review", "done", "yolo", "accent")]
        for color in groups[10:]:
            with self.subTest(color=color):
                for theme, tokens in themes.items():
                    self.assertGreaterEqual(contrast(color, tokens["surface"]), 3, f"tags read on {theme} cards")
                self.assertGreaterEqual(min(oklab_distance(color, s) for s in states), .1, "unlike every state color")
                self.assertGreaterEqual(min(oklab_distance(color, g) for g in groups if g != color), .1, "unlike every other group")

    def test_text_reaches_wcag_aa_in_both_themes(self):
        css = page_css()
        rule = lambda selector: css_rule(css, selector)
        states = {s: {k: v for k, v in rule(f".s-{s}").items() if k.startswith("--")} for s in ("proposed", "todo", "blocked", "doing", "review", "done")}
        hover = float(re.search(r"brightness\(([\d.]+)\)", rule("button.primary:hover")["filter"]).group(1))
        brighter = lambda c: tuple(min(255, v * hover) for v in c[:3]) + (1,)
        stripes = css_args(re.search(r"repeating-linear-gradient\((.*)\)", rule(':is(.card, .kcard).s-blocked, [data-theme="dark"] :is(.card, .kcard).s-blocked')["background"]).group(1))[1:]
        for theme, env in theme_env(css).items():
            color = lambda value, **more: css_color(value, {**env, **more})

            def check(what, fg, under, need=4.5):
                with self.subTest(theme=theme, what=what):
                    self.assertGreaterEqual(contrast(fg, under), need)

            surface, bg = color("var(--surface)"), color("var(--bg)")
            grounds = {"surface": surface, "surface-2": color("var(--surface-2)"), "bg": bg, "lane": over(color("var(--lane)"), bg)}
            for token in ("text", "muted", "faint"):
                for name, ground in grounds.items():
                    check(f"--{token} on {name}", color(f"var(--{token})"), ground)
            check("placeholders", color(rule("::placeholder")["color"]), surface)
            for name in ("surface", "surface-2"):
                check(f"danger text on {name}", color(rule("button.danger")["color"]), grounds[name])
            for selector in ("button.primary", "button.danger.primary", "#inbox b", ".toast.error"):
                fill = color(rule(selector)["background"])
                check(selector, color(rule(selector)["color"]), fill)
                if selector.startswith("button"):
                    check(f"{selector} on hover", color(rule(selector)["color"]), brighter(fill))
            asking = rule(":is(.card, .kcard).asking .badge")
            check("waiting for you", color(asking["color"], **{"--c": "var(--accent)"}), color(asking["background"], **{"--c": "var(--accent)"}))
            badge, toast_badge, filled, button = rule(".badge"), rule(".toast .badge"), rule(":is(.card, .kcard):is(.s-doing, .s-review) .badge"), rule("button.primary.to")
            for state, own in states.items():
                cards = [color(rule(".card")["background"], **own)]
                if state == "blocked":
                    cards = [color(re.sub(r"(\s+[\d.]+(px)?)+$", "", stop), **own) for stop in stripes]
                for card in cards + [surface]:
                    check(f"{state} badge", color(badge["color"], **own), over(color(badge["background"], **own), card))
                    check(f"YOLO mark on {state}", color(rule(".yolo-mark")["color"]), card, 3)
                mine = {**own, "--c-on": toast_badge["--c-on"]}
                check(f"{state} badge in a toast", color(toast_badge["color"], **mine), over(color(toast_badge["background"], **mine), color(rule(".toast")["background"])))
                if state in ("doing", "review"):
                    check(f"filled {state} badge", color(filled["color"], **own), color(filled["background"], **own))
                if "--c-fill" in own:
                    fill = color(button["background"], **own)
                    check(f"button to {state}", color(button["color"], **own), fill)
                    check(f"button to {state} on hover", color(button["color"], **own), brighter(fill))
        # Opacity would take the text of the zoom bar and of empty state chips below these ratios.
        for sels, body in css_rules(css):
            if re.search(r"(^|;)\s*opacity:\s*(?!1\b)", body):
                for sel in sels:
                    subject = re.sub(r"\([^()]*\)", "", re.sub(r"\([^()]*\)", "", sel)).split()[-1]
                    self.assertNotRegex(subject, r"^(\.zoombar|\.schip\.zero)(:[\w-]+)*$", f"{sel} fades its text")

    def test_states_read_apart_without_color(self):
        css = page_css()
        rule = lambda selector: css_rule(css, selector)
        states = {s: {k: v for k, v in rule(f".s-{s}").items() if k.startswith("--")} for s in ("proposed", "blocked", "todo", "doing", "review", "done")}
        marks = [own.get("--mark") for own in states.values()]
        self.assertTrue(all(marks), "every state has a mark")
        self.assertEqual(len(set(marks)), len(marks), "each mark has a shape of its own")
        self.assertIn("var(--mark)", rule(".dot")["mask"])
        for theme, env in theme_env(css).items():
            color = lambda value, **more: css_color(value, {**env, **more})
            surface_2 = color("var(--surface-2)")
            for state, own in states.items():
                grounds = {
                    "surface": color("var(--surface)"), "surface-2": surface_2, "a chosen tab": color(rule(".track > button.on")["background"]),
                    "a chip on hover": over(color(rule(".track > button:hover")["background"]), surface_2),
                    "the chosen chip": color(rule(".track > .schip.on")["background"], **own),
                    "an arrow menu row": color(rule(".ctx-menu .ctx-task")["background"], **own),
                    "an arrow menu row on hover": color(rule(".ctx-menu .ctx-task:hover, .ctx-menu .ctx-task:focus-visible")["background"], **own),
                    "a picker chip": color(rule(".pick-chip")["background"], **own),
                    "the picker's active option": color(rule(".pick-opt.on")["background"]),
                    "a column under a drag": color(rule(".col.drop")["background"]),
                }
                fgs = {"mark": (color(rule(".dot")["background"], **own), grounds)}
                fgs["mark of an empty chip"] = (color(rule(".schip.zero:not(.on) .dot")["background"]), {k: v for k, v in grounds.items() if k != "the chosen chip"})
                if state == "done":
                    fgs["hidden finished tasks"] = (color(rule(".schip.off .icon")["color"], **own), grounds)
                for what, (fg, under) in fgs.items():
                    for where, ground in under.items():
                        with self.subTest(theme=theme, state=state, mark=what, on=where):
                            self.assertGreaterEqual(contrast(fg, ground), 3)


class LayoutTests(unittest.TestCase):
    """The graph layout must not get more cluttered. Lower the ceilings when it improves;
    `python tests/layout_stats.py -v` shows the current numbers and what causes them, and
    `--unfolded` the same with every lane's finished tasks shown."""

    # (board, flows down, finished tasks unfolded): (crossing arrow pairs, arrows running under cards, bends, space over card area)
    CEILINGS = {
        ("sample.json", False, False): (3, 0, 12, 3.8),
        ("sample.json", True, False): (3, 0, 12, 3.8),
        ("tetris.json", False, False): (18, 0, 26, 6.1),
        ("tetris.json", True, False): (18, 0, 26, 6.7),
        ("phases.json", False, False): (3, 0, 14, 4.5),
        ("phases.json", True, False): (3, 0, 14, 4.7),
        ("sample.json", False, True): (3, 0, 12, 3.8),
        ("sample.json", True, True): (3, 0, 12, 3.8),
        ("tetris.json", False, True): (18, 0, 26, 6.1),
        ("tetris.json", True, True): (18, 0, 26, 6.7),
        ("phases.json", False, True): (5, 0, 21, 5.5),
        ("phases.json", True, True): (5, 0, 21, 6.7),
    }

    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")

    def test_clutter_stays_under_ceilings(self):
        import layout_stats
        for unfolded in (False, True):
            for (path, vertical), m in layout_stats.measure(layout_stats.DEFAULT_BOARDS, unfolded=unfolded).items():
                crossings, behind, bends, space = self.CEILINGS[(os.path.basename(path), vertical, unfolded)]
                with self.subTest(board=os.path.basename(path), vertical=vertical, unfolded=unfolded):
                    self.assertLessEqual(m["crossings"], crossings)
                    self.assertLessEqual(m["behind"], behind)
                    self.assertLessEqual(m["bends"], bends)
                    self.assertLessEqual(m["space"], space)

    def test_lanes_that_share_a_track_stay_apart(self):
        code = (
            "const out = [];\n"
            "for (const text of input) {\n"
            "  const b = normalize(JSON.parse(text)), open = b.tasks.filter((t) => t.state !== 'done'), fold = foldDone(b.tasks, b, new Set());\n"
            "  for (const vertical of [false, true]) for (const levelGap of [30, 50, 70, 500]) for (const manual of [false, true]) for (const [tasks, stubs] of [[b.tasks, []], [open, []], [fold.tasks, fold.stubs]]) {\n"
            "    const L = layout(tasks, b, { vertical, levelGap, manual, emptyLanes: tasks !== open, stubs });\n"
            "    const box = (l) => [l.left, l.top, l.left + l.width, l.top + l.height];\n"
            "    const where = `${b.title} ${vertical ? 'down' : 'right'} ${levelGap}${manual ? ' manual' : ''}`;\n"
            "    L.lanes.forEach((l, i) => L.lanes.slice(i + 1).forEach((o) => {\n"
            "      const [a, c] = [box(l), box(o)];\n"
            "      if (a[0] < c[2] && c[0] < a[2] && a[1] < c[3] && c[1] < a[3]) out.push(`${where}: lanes ${l.id} and ${o.id} overlap`);\n"
            "    }));\n"
            "    for (const t of tasks) {\n"
            "      const p = L.pos.get(key(t.id)), l = L.lanes.find((x) => x.id === (b.groups.some((g) => g.id === t.group) ? t.group : null)), r = box(l);\n"
            "      if (p.x < r[0] || p.y < r[1] + LANE_HEAD || p.x + CW > r[2] || p.y + CH > r[3]) out.push(`${where}: ${t.id} is outside its lane`);\n"
            "    }\n"
            "    for (const s of L.stubs) {\n"
            "      const r = box(L.lanes.find((x) => x.id === s.group));\n"
            "      if (s.x < r[0] || s.y < r[1] + LANE_HEAD || s.x + CW > r[2] || s.y + STUB > r[3]) out.push(`${where}: the stub of ${s.group} is outside its lane`);\n"
            "    }\n"
            "    const boxes = [...[...L.pos].map(([k, p]) => [k, p.x, p.y, CH]), ...L.stubs.map((s) => [s.key, s.x, s.y, STUB])];\n"
            "    boxes.forEach((a, i) => boxes.slice(i + 1).forEach((c) => {\n"
            "      if (a[1] < c[1] + CW && c[1] < a[1] + CW && a[2] < c[2] + c[3] && c[2] < a[2] + a[3]) out.push(`${where}: ${a[0]} and ${c[0]} overlap`);\n"
            "    }));\n"
            "  }\n"
            "}\n"
            "process.stdout.write(JSON.stringify(out));"
        )
        import layout_stats
        texts = []
        for path in layout_stats.DEFAULT_BOARDS:
            with open(path, encoding="utf-8") as f:
                texts.append(f.read())
        self.assertEqual(json.loads(run_html_js(self, code, json.dumps(texts))), [])

    def test_hiding_finished_tasks_drops_groups_with_nothing_left(self):
        code = (
            "const b = normalize(input), open = b.tasks.filter((t) => t.state !== 'done');\n"
            "const lanes = (tasks, emptyLanes) => layout(tasks, b, { vertical: false, levelGap: 70, emptyLanes }).lanes.map((l) => `${l.id}:${l.color}`);\n"
            "process.stdout.write(JSON.stringify({ all: lanes(b.tasks, false), shown: lanes(open, true), hidden: lanes(open, false) }));"
        )
        with open(os.path.join(ROOT, "examples", "sample.json"), encoding="utf-8") as f:
            out = json.loads(run_html_js(self, code, f.read()))
        self.assertEqual(len(out["all"]), 4)
        self.assertEqual(out["shown"], out["all"])
        self.assertEqual(out["hidden"], out["all"][1:])
        self.assertTrue(out["all"][0].startswith("RS:"))

    def test_finished_tasks_fold_into_a_stub_per_lane(self):
        board = {"kanvas": 1, "title": "Fold", "groups": [{"id": "A", "name": "Alpha"}, {"id": "B", "name": "Beta"}, {"id": "C", "name": "Gamma"}], "tasks": [
            task("A-1", "done", "A"), task("A-2", "done", "A", ["A-1"]), task("A-3", "done", "A", ["A-2"]),
            task("A-4", "todo", "A", ["A-3"]), task("A-5", "todo", "A"),
            task("B-1", "done", "B"), task("B-2", "done", "B"), task("B-3", "todo", "B", ["B-1", "A-2"]),
            task("C-1", "done", "C"), task("C-2", "done", "C"), task("C-3", "done", "C", ["C-1"]),
            task("X-1", "done"), task("X-2", "done"), task("X-3", "done"),
        ]}
        code = (
            "const b = normalize(input), out = {};\n"
            "const run = (name, open, keep, opts = {}) => {\n"
            "  const f = foldDone(b.tasks, b, new Set(open), keep), L = layout(f.tasks, b, { vertical: false, levelGap: 70, stubs: f.stubs, ...opts });\n"
            "  const stub = (g) => L.stubs.find((s) => s.group === g), lane = (g) => L.lanes.find((l) => l.id === g);\n"
            "  const inLane = (s) => { const l = lane(s.group); return s.x >= l.left && s.y >= l.top + LANE_HEAD && s.x + CW <= l.left + l.width && s.y + STUB <= l.top + l.height; };\n"
            "  const leads = (s) => [...L.pos].filter(([k]) => b.tasks.some((t) => key(t.id) === k && (t.group || null) === s.group) && L.pos.get(k).x === s.x).every(([, p]) => p.y >= s.y + STUB + GAP_Y);\n"
            "  out[name] = { shown: f.tasks.map((t) => t.id), stubs: L.stubs.map((s) => [s.group, s.ids.length, s.folded, s.open]),\n"
            "    edges: L.edges.filter((e) => !e.implied).map((e) => `${e.from}>${e.to}`).sort(), inLanes: L.stubs.every(inLane), lead: L.stubs.every(leads),\n"
            "    first: L.stubs.every((s) => s.x === L.pos.get('A-5').x), empty: L.lanes.filter((l) => l.empty).map((l) => l.id), behind: layoutMetrics(L).behind };\n"
            "};\n"
            "run('folded', [], null);\n"
            "run('manual', [], null, { manual: true });\n"
            "run('down', [], null, { vertical: true });\n"
            "run('open', ['A'], null);\n"
            "run('kept', [], 'C-3');\n"
            "out.hidden = foldDone(b.tasks.filter((t) => t.state !== 'done'), b, new Set()).stubs.length;\n"
            "process.stdout.write(JSON.stringify(out));"
        )
        out = json.loads(run_html_js(self, code, json.dumps(board)))
        folded = out["folded"]
        # Lanes with three or more finished tasks fold them; Beta, with two, shows its own.
        self.assertEqual(folded["shown"], ["A-4", "A-5", "B-1", "B-2", "B-3"])
        self.assertEqual(folded["stubs"], [["A", 3, ["A-1", "A-2", "A-3"], False], ["C", 3, ["C-1", "C-2", "C-3"], False], [None, 3, ["X-1", "X-2", "X-3"], False]])
        # Arrows from folded tasks go with them, so A-4 starts its lane, after the stub.
        self.assertEqual(folded["edges"], ["B-1>B-3"])
        self.assertEqual(folded["empty"], [])
        for name in ("folded", "manual", "down", "open", "kept"):
            with self.subTest(name):
                self.assertTrue(out[name]["inLanes"])
                self.assertTrue(out[name]["lead"])
                self.assertEqual(out[name]["behind"], 0)
        self.assertTrue(folded["first"])
        self.assertEqual(out["manual"]["shown"], folded["shown"])
        # An opened lane shows its finished tasks and their arrows, and keeps its stub to fold them again.
        self.assertEqual(out["open"]["stubs"][0], ["A", 3, [], True])
        self.assertEqual(out["open"]["edges"], ["A-1>A-2", "A-2>A-3", "A-2>B-3", "A-3>A-4", "B-1>B-3"])
        # The task the human selected stays shown in a folded lane.
        self.assertIn("C-3", out["kept"]["shown"])
        self.assertEqual(out["kept"]["stubs"][1], ["C", 3, ["C-1", "C-2"], False])
        # With finished tasks hidden there is nothing to fold.
        self.assertEqual(out["hidden"], 0)

    def test_graph_opens_readable_on_the_work_that_waits(self):
        code = (
            "const b = normalize(input), L = layout(b.tasks, b, { vertical: false, levelGap: 70 }), ix = indexOf(b);\n"
            "const ids = (s) => b.tasks.filter((t) => vstateIn(ix, t) === s).map((t) => key(t.id));\n"
            "const room = (w, h) => ({ left: 24, top: 24, right: 24 + w, bottom: 24 + h });\n"
            "const shown = (r, v, ks) => ks.every((k) => { const p = L.pos.get(k), x = v.x + p.x * v.k, y = v.y + p.y * v.k;\n"
            "  return x >= r.left && y >= r.top && x + CW * v.k <= r.right && y + CH * v.k <= r.bottom; });\n"
            "const tiers = [ids('review'), ids('doing'), ids('proposed')], out = {};\n"
            "for (const [name, w, h] of [['small', 0.5, 0.5], ['narrow', 0.2, 0.6]]) {\n"
            "  const r = room(L.width * w, L.height * h), v = readableView(r, L, tiers, 0.7);\n"
            "  out[name] = { k: v.k, review: shown(r, v, tiers[0]), doing: shown(r, v, tiers[1]), proposed: shown(r, v, tiers[2]) };\n"
            "}\n"
            "const tall = readableView(room(L.width * 0.9, L.height * 0.5), L, tiers, 0.7);\n"
            "const all = readableView(room(L.width * 0.2, L.height * 0.6), L, [b.tasks.map((t) => key(t.id))], 0.7);\n"
            "out.tall = { k: tall.k, x: tall.x, review: shown(room(L.width * 0.9, L.height * 0.5), tall, tiers[0]) };\n"
            "out.all = { x: all.x, y: all.y };\n"
            "process.stdout.write(JSON.stringify(out));"
        )
        with open(os.path.join(ROOT, "examples", "sample.json"), encoding="utf-8") as f:
            out = json.loads(run_html_js(self, code, f.read()))
        # Too large to fit at a readable zoom: the work waiting for the human comes first, then what fits with it.
        self.assertEqual(out["small"], {"k": 0.7, "review": True, "doing": True, "proposed": False})
        self.assertEqual((out["narrow"]["k"], out["narrow"]["review"], out["narrow"]["proposed"]), (0.7, True, False))
        # A tall graph fits its width and scrolls to the review.
        self.assertAlmostEqual(out["tall"]["k"], 0.9)
        self.assertAlmostEqual(out["tall"]["x"], 24)
        self.assertTrue(out["tall"]["review"])
        # A focus larger than the room shows its start.
        self.assertEqual(out["all"], {"x": 24, "y": 24})

    def test_manual_layout_keeps_file_order(self):
        import layout_stats
        for (path, vertical), m in layout_stats.measure(layout_stats.DEFAULT_BOARDS, manual=True).items():
            with self.subTest(board=os.path.basename(path), vertical=vertical):
                self.assertEqual(m["misordered"], 0)
                self.assertEqual(m["behind"], 0)


if __name__ == "__main__":
    unittest.main()
