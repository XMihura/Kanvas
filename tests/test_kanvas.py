"""Tests for kanvas.py. Run with: python -m unittest discover -s tests -v"""

import argparse
import contextlib
import http.client
import io
import json
import os
import re
import shutil
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

SCRIPT = os.path.join(ROOT, "kanvas.py")


def write_board(path, board):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(kanvas.dumps(board))


def task(tid, state="todo", group=None, deps=(), **extra):
    t = {"id": tid, "title": f"Task {tid}", "state": state, "group": group, "deps": list(deps)}
    t.update(extra)
    return t


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
    proc = subprocess.run([node, f.name], input=stdin.encode("utf-8"), capture_output=True)
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


class WorkflowTests(TempBoard):
    def test_status_lists_what_matters(self):
        _, out, _ = self.run_cli("status")
        self.assertIn("Ready to start (1)", out)
        self.assertIn("DV-03  Task DV-03  (waiting on DV-02)", out)
        self.assertIn("Proposed, awaiting human approval (1)", out)
        _, out, _ = self.run_cli("ready")
        self.assertEqual(out.split(), ["DV-02", "Task", "DV-02"])

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

    def test_proposals_linked_into_the_chain_count_as_approved(self):
        self.assertIn("a human must approve it", self.fails("start", "DV-04"))
        self.run_cli("depend", "DV-02", "DV-04")
        _, out, _ = self.run_cli("status")
        self.assertIn("YOLO on DV-03", out)
        self.assertIn("DV-03 itself goes to review", out)
        self.assertNotIn("Proposed, awaiting human approval", out)
        _, out, _ = self.run_cli("start", "DV-04")
        self.assertIn("proposed -> doing, approved by YOLO", out)
        self.assertIn("YOLO:   through DV-03", out)
        self.run_cli("finish", "DV-04", "-m", "Done")
        self.assertEqual(self.get("DV-04")["state"], "done")


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

    def test_more_than_99_tasks(self):
        spec = [{"title": f"Step {i}", "group": "Development"} for i in range(1, 101)]
        self.run_cli("batch", stdin=json.dumps(spec).encode("utf-8"))
        ids = [t["id"] for t in self.board()["tasks"]]
        self.assertIn("DV-99", ids)
        self.assertIn("DV-104", ids)
        self.assertEqual(len(set(ids)), len(ids))
        self.run_cli("check")


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

    def test_no_hint_when_versions_match_or_cannot_be_read(self):
        self.assertIsNone(self.hint("0.5.0", installed=True, on_path=True))
        self.assertIsNone(self.hint("0.6.0rc1", installed=True, on_path=True))
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

    def test_export_snapshot(self):
        board = self.board()
        board["tasks"].append(task("G-01", desc="</script><script>alert(1)</script>"))
        write_board(self.path, board)
        self.run_cli("export", "snap.html")
        with open(os.path.join(self.dir, "snap.html"), encoding="utf-8") as f:
            html = f.read()
        m = re.search(r'<script id="kanvas-snapshot" type="application/json" data-at="[^"]+">(.*?)</script>', html, re.S)
        self.assertIsNotNone(m)
        self.assertNotIn("<", m.group(1))
        self.assertEqual(json.loads(m.group(1))["tasks"][0]["desc"], "</script><script>alert(1)</script>")


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

    def test_stale_lock_is_cleared(self):
        lock = self.path + ".lock"
        open(lock, "w").close()
        old = time.time() - 60
        os.utime(lock, (old, old))
        self.run_cli("note", "DV-02", "after a crash")
        self.assertFalse(os.path.exists(lock))

    def test_busy_lock_gives_up_at_the_deadline(self):
        open(self.path + ".lock", "w").close()
        with mock.patch.object(kanvas, "LOCK_TIMEOUT", 0.2):
            with self.assertRaisesRegex(kanvas.KanvasError, "Board is busy"):
                kanvas.mutate(self.path, lambda board: None)

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
        return self.request("PUT", "/api/board", json.dumps(board).encode("utf-8"), headers)

    def test_starting_skips_the_host_name_lookup(self):
        """socket.getfqdn, which HTTPServer calls on start, can take half a minute on macOS."""
        with mock.patch("socket.getfqdn", side_effect=AssertionError("getfqdn was called")):
            server = kanvas.start_server(self.path, "127.0.0.1", 0, quiet=True)
        server.server_close()
        self.assertEqual(server.server_name, "127.0.0.1")

    def test_serves_the_page_and_the_board(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>Kanvas</title>", body)
        status, headers, body = self.request("GET", "/api/board")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.raw())
        status, _, body = self.request("GET", "/api/board", headers={"If-None-Match": headers["ETag"]})
        self.assertEqual((status, body), (304, b""))

    def test_compare_and_swap(self):
        _, headers, body = self.request("GET", "/api/board")
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
        self.assertEqual(self.put(bad)[0], 400)
        dup = json.loads(before)
        dup["tasks"].append(dict(dup["tasks"][0]))
        self.assertEqual(self.put(dup)[0], 400)
        status, _, _ = self.request("GET", "/api/board", headers={"Host": "evil.example:8765"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("PUT", "/api/board", b"{}", {"Host": "evil.example", "Content-Type": "application/json"})
        self.assertEqual(status, 403)
        self.assertEqual(self.raw(), before)

    def browser_move_after_agent(self, state, *agent_args):
        """The page loads the board, an agent runs a command, then the human moves DV-02 to `state`."""
        code = (
            "const base = `http://127.0.0.1:${input.port}/`;\n"
            "const fetch = (url, opts) => globalThis.fetch(new URL(url, base), opts);\n"
            "(async () => {\n"
            "  const store = serverStore(), seen = await store.load();\n"
            "  require('child_process').execFileSync(input.python, [input.script, ...input.agent], { cwd: input.dir });\n"
            "  const t = seen.tasks.find((x) => x.id === 'DV-02');\n"
            "  try { await store.save(ops.state(t.id, t.state, input.state), seen); process.stdout.write('saved'); }\n"
            "  catch (e) { process.stdout.write(`refused: ${e.message}`); }\n"
            "})();"
        )
        sections = [("function serverStore()", "function fileStore("), ("function mustFind(", "\n// ----")]
        args = {"port": self.port, "python": sys.executable, "script": SCRIPT, "dir": self.dir,
                "state": state, "agent": list(agent_args)}
        return run_html_js(self, code, json.dumps(args), sections)

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

    def test_browser_writes_are_canonical(self):
        board = json.loads(self.raw())
        board["tasks"][3]["state"] = "todo"
        status, _, _ = self.request("PUT", "/api/board", json.dumps(board, indent=None).encode("utf-8"), {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        text = self.raw().decode("utf-8")
        self.assertEqual(text, kanvas.dumps(kanvas.parse_board(text)))


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

    def serve(self, answer, *args, env=None):
        """Runs `serve` at a pretend terminal, answering its question with `answer` (text or an exception)."""
        self.server = mock.Mock(server_address=("127.0.0.1", 8765), check_host=True)
        stdin, stdout, stderr = mock.Mock(), Terminal(), io.StringIO()
        stdin.isatty.return_value = True
        reply = {"return_value": answer} if isinstance(answer, str) else {"side_effect": answer}
        with mock.patch.dict(os.environ), mock.patch("builtins.input", **reply) as self.ask, \
                mock.patch.object(kanvas, "start_server", return_value=self.server) as self.start, \
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


class LayoutTests(unittest.TestCase):
    """The graph layout must not get more cluttered. Lower the ceilings when it improves;
    `python tests/layout_stats.py -v` shows the current numbers and what causes them."""

    # (board, flows down): (crossing arrow pairs, arrows running under cards, bends)
    CEILINGS = {
        ("sample.json", False): (3, 0, 12),
        ("sample.json", True): (3, 0, 12),
        ("tetris.json", False): (22, 0, 26),
        ("tetris.json", True): (22, 0, 26),
    }

    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")

    def test_clutter_stays_under_ceilings(self):
        import layout_stats
        for (path, vertical), m in layout_stats.measure(layout_stats.DEFAULT_BOARDS).items():
            crossings, behind, bends = self.CEILINGS[(os.path.basename(path), vertical)]
            with self.subTest(board=os.path.basename(path), vertical=vertical):
                self.assertLessEqual(m["crossings"], crossings)
                self.assertLessEqual(m["behind"], behind)
                self.assertLessEqual(m["bends"], bends)

    def test_manual_layout_keeps_file_order(self):
        import layout_stats
        for (path, vertical), m in layout_stats.measure(layout_stats.DEFAULT_BOARDS, manual=True).items():
            with self.subTest(board=os.path.basename(path), vertical=vertical):
                self.assertEqual(m["misordered"], 0)
                self.assertEqual(m["behind"], 0)


if __name__ == "__main__":
    unittest.main()
