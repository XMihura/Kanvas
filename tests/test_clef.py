"""Tests for Kanvas's use of Clef, Cloudflare's decision model. No test reaches the network: they use FakeClef, or
ClefClient against a stub server on this computer. Run with: python -m unittest discover -s tests -v"""

import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import kanvas  # noqa: E402

CREDENTIALS = {"KANVAS_CLEF": "1", "CLOUDFLARE_ACCOUNT_ID": "acc123", "CLOUDFLARE_API_TOKEN": "tok-secret"}
NOUL = {"type": "noul", "instructions": "Does BE-03 need the result of BE-02 first?"}

_real_connect = socket.create_connection


def local_only(address, *args, **kwargs):
    """socket.create_connection for the tests: this computer only."""
    if address[0] not in ("127.0.0.1", "localhost", "::1"):
        raise AssertionError(f"A test tried to reach {address[0]}")
    return _real_connect(address, *args, **kwargs)


class NoNetwork(unittest.TestCase):
    """Every Clef test fails if anything tries to leave this computer."""

    def setUp(self):
        patcher = mock.patch("socket.create_connection", local_only)
        patcher.start()
        self.addCleanup(patcher.stop)


class Stub(BaseHTTPRequestHandler):
    """Stands in for Cloudflare: records each request, then answers as the test says."""

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.seen.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(body)})
        status, reply, wait = self.server.reply
        time.sleep(wait)
        data = json.dumps(reply).encode("utf-8")
        self.send_response(status)
        if status in (301, 302, 307):
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/elsewhere")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass


class ClefClientTests(NoNetwork):
    def setUp(self):
        super().setUp()
        self.stub = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        self.stub.daemon_threads = True
        self.stub.seen = []
        self.stub.reply = (200, {"result": {"answers": {"q": {"type": "noul", "noul": 0.9}}, "usage": {"prompt_tokens": 42}},
                                 "success": True, "errors": []}, 0)
        threading.Thread(target=self.stub.serve_forever, daemon=True).start()
        self.addCleanup(self.stub.server_close)
        self.addCleanup(self.stub.shutdown)
        self.url = f"http://127.0.0.1:{self.stub.server_address[1]}/client/v4/accounts/acc123/ai/run/@cf/cloudflare/clef"

    def client(self, timeout=5):
        return kanvas.ClefClient("acc123", "tok-secret", timeout=timeout, url=self.url)

    def test_request_has_the_documented_shape(self):
        req = kanvas.clef_from_env(CREDENTIALS).request("Task BE-03: retries", {"q": NOUL})
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, "https://api.cloudflare.com/client/v4/accounts/acc123/ai/run/@cf/cloudflare/clef")
        self.assertEqual(req.get_header("Authorization"), "Bearer tok-secret")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(req.data), {"model": "clef", "state": "Task BE-03: retries", "questions": {"q": NOUL}})

    def test_the_account_cannot_change_the_address(self):
        req = kanvas.ClefClient("../../evil?x=", "t").request("s", {"q": NOUL})
        self.assertTrue(req.full_url.startswith("https://api.cloudflare.com/client/v4/accounts/..%2F..%2Fevil%3Fx%3D/ai/run/"))

    def test_a_call_sends_the_request_and_reads_the_answer(self):
        reply = self.client().ask({"task": "BE-03"}, {"q": NOUL})
        self.assertEqual(reply["answers"], {"q": {"type": "noul", "p": 0.9}})
        self.assertEqual(kanvas.clef_input_tokens(reply["usage"]), 42)
        self.assertGreaterEqual(reply["seconds"], 0)
        seen = self.stub.seen[0]
        self.assertEqual(seen["path"], "/client/v4/accounts/acc123/ai/run/@cf/cloudflare/clef")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer tok-secret")
        self.assertEqual(seen["body"], {"model": "clef", "state": {"task": "BE-03"}, "questions": {"q": NOUL}})

    def test_answers_without_the_envelope_are_read_too(self):
        self.stub.reply = (200, {"answers": {"q": {"type": "noul", "noul": 0.25}}, "usage": {}}, 0)
        self.assertEqual(self.client().ask("s", {"q": NOUL})["answers"], {"q": {"type": "noul", "p": 0.25}})

    def test_a_slow_answer_times_out(self):
        self.stub.reply = (200, {"answers": {}}, 1.0)
        start = time.monotonic()
        with self.assertRaisesRegex(kanvas.ClefError, "timed out"):
            self.client(timeout=0.2).ask("s", {"q": NOUL})
        self.assertLess(time.monotonic() - start, 0.9)

    def test_errors_say_what_cloudflare_said_without_the_token(self):
        self.stub.reply = (403, {"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}, 0)
        with self.assertRaises(kanvas.ClefError) as e:
            self.client().ask("s", {"q": NOUL})
        self.assertIn("HTTP 403", str(e.exception))
        self.assertIn("Authentication error", str(e.exception))
        self.assertNotIn("tok-secret", str(e.exception))

    def test_a_refusal_inside_a_200_is_an_error(self):
        self.stub.reply = (200, {"success": False, "errors": [{"message": "Too many questions"}], "result": None}, 0)
        with self.assertRaisesRegex(kanvas.ClefError, "Too many questions"):
            self.client().ask("s", {"q": NOUL})

    def test_redirects_are_not_followed_with_the_token(self):
        self.stub.reply = (307, {}, 0)
        with self.assertRaisesRegex(kanvas.ClefError, "HTTP 307"):
            self.client().ask("s", {"q": NOUL})
        self.assertEqual([s["path"] for s in self.stub.seen], ["/client/v4/accounts/acc123/ai/run/@cf/cloudflare/clef"])

    def test_no_connection_is_an_error(self):
        port = self.stub.server_address[1]
        self.stub.shutdown()
        self.stub.server_close()
        client = kanvas.ClefClient("a", "t", timeout=1, url=f"http://127.0.0.1:{port}/x")
        with self.assertRaisesRegex(kanvas.ClefError, "Could not reach Clef"):
            client.ask("s", {"q": NOUL})

    def test_missing_ca_certificates_say_how_to_fix_them(self):
        import ssl
        import urllib.error
        client = self.client()
        broken = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate")
        with mock.patch.object(client.opener, "open", side_effect=urllib.error.URLError(broken)):
            with self.assertRaisesRegex(kanvas.ClefError, "CERTIFICATE_VERIFY_FAILED.*SSL_CERT_FILE=/etc/ssl/cert.pem.*Install Certificates"):
                client.ask("s", {"q": NOUL})
        with mock.patch.object(client.opener, "open", side_effect=urllib.error.URLError("timed out")):
            with self.assertRaises(kanvas.ClefError) as e:
                client.ask("s", {"q": NOUL})
            self.assertNotIn("SSL_CERT_FILE", str(e.exception), "only for certificate failures")

    def test_the_token_never_shows(self):
        client = kanvas.clef_from_env(CREDENTIALS)
        self.assertNotIn("tok-secret", repr(client))
        self.assertNotIn("tok-secret", str(vars(client).get("url")))


class QuestionTests(unittest.TestCase):
    def test_clef_takes_1_to_64_questions(self):
        kanvas.clef_check("s", {f"q{i}": NOUL for i in range(64)})
        for n in (0, 65):
            with self.subTest(questions=n), self.assertRaisesRegex(kanvas.ClefError, "1 to 64 questions"):
                kanvas.clef_check("s", {f"q{i}": NOUL for i in range(n)})

    def test_only_yes_no_questions_with_instructions(self):
        kanvas.clef_check("s", {"a": NOUL})
        bad = {
            "choice": {"type": "choice", "instructions": "Which?", "criteria": {"a": "", "b": ""}},
            "score": {"type": "score", "instructions": "How well?", "criteria": ["low", "high"]},
            "no type": {"instructions": "Yes?"},
            "instructions": {"type": "noul", "instructions": " "},
        }
        for what, q in bad.items():
            with self.subTest(what), self.assertRaises(kanvas.ClefError):
                kanvas.clef_check("s", {"q": q})
        with self.assertRaises(kanvas.ClefError):
            kanvas.clef_check(5, {"q": NOUL})


class AnswerTests(unittest.TestCase):
    def read(self, answers, questions):
        return kanvas.clef_answers({"result": {"answers": answers}}, questions)[0]

    def test_noul(self):
        self.assertEqual(self.read({"q": {"type": "noul", "noul": 0.82}}, {"q": NOUL}), {"q": {"type": "noul", "p": 0.82}})
        self.assertEqual(self.read({"q": {"type": "noul", "noul": 1.3}}, {"q": NOUL})["q"]["p"], 1.0)

    def test_answers_kanvas_cannot_read_are_left_out(self):
        got = self.read({"a": {"type": "noul"}, "d": "yes", "e": {"type": "noul", "noul": "x"}, "g": {"type": "noul", "noul": None}},
                        {"a": NOUL, "d": NOUL, "e": NOUL, "f": NOUL, "g": NOUL})
        self.assertEqual(got, {})

    def test_a_response_without_answers_is_an_error(self):
        for response in ([], {"result": {}}, {"usage": {}}):
            with self.subTest(response=response), self.assertRaises(kanvas.ClefError):
                kanvas.clef_answers(response, {"q": NOUL})


class OptInTests(NoNetwork):
    def test_nothing_is_asked_without_all_three(self):
        for env in ({}, {"KANVAS_CLEF": "1"}, {"KANVAS_CLEF": "1", "CLOUDFLARE_ACCOUNT_ID": "a"},
                    {"KANVAS_CLEF": "1", "CLOUDFLARE_API_TOKEN": "t"}, dict(CREDENTIALS, KANVAS_CLEF="0"),
                    dict(CREDENTIALS, KANVAS_CLEF="yes"), dict(CREDENTIALS, CLOUDFLARE_API_TOKEN=" ")):
            with self.subTest(env=sorted(env)):
                self.assertIsNone(kanvas.clef_from_env(env))
        self.assertIsInstance(kanvas.clef_from_env(CREDENTIALS), kanvas.ClefClient)

    def test_serve_can_say_what_is_missing(self):
        self.assertEqual(kanvas.clef_missing({}), [])
        self.assertEqual(kanvas.clef_missing({"KANVAS_CLEF": "1", "CLOUDFLARE_ACCOUNT_ID": "a"}), ["CLOUDFLARE_API_TOKEN"])
        self.assertEqual(kanvas.clef_missing(CREDENTIALS), [])

    def test_the_process_environment_is_the_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(kanvas.clef_from_env())
        with mock.patch.dict(os.environ, CREDENTIALS, clear=True):
            self.assertIsInstance(kanvas.clef_from_env(), kanvas.ClefClient)

    def test_a_fake_table_sends_nothing(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        path = os.path.join(folder, "fake.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"answers": {"needs:*": {"type": "noul", "noul": 0.82}},
                       "rules": [{"question": "needs:BE-03", "state": "BE-05", "answer": {"type": "noul", "noul": 0.1}}]}, f)
        fake = kanvas.clef_from_env({"KANVAS_CLEF_FAKE": path})
        self.assertIsInstance(fake, kanvas.FakeClef)
        reply = fake.ask("Task BE-05", {"needs:BE-03": NOUL, "needs:FE-01": NOUL})
        self.assertEqual(reply["answers"], {"needs:BE-03": {"type": "noul", "p": 0.1}, "needs:FE-01": {"type": "noul", "p": 0.82}})
        self.assertEqual(fake.ask("Task FE-02", {"needs:BE-03": NOUL})["answers"]["needs:BE-03"]["p"], 0.82)

class FakeClefTests(NoNetwork):
    def test_answers_from_the_table_and_records_calls(self):
        fake = kanvas.FakeClef({"needs:BE-02": {"type": "noul", "noul": 0.9},
                                "needs:FE-*": lambda state, q: {"type": "noul", "noul": 0.8 if "FE" in state else 0.2}},
                               default={"type": "noul", "noul": 0.3})
        reply = fake.ask("FE task", {"needs:BE-02": NOUL, "needs:FE-01": NOUL, "needs:XX-01": NOUL})
        self.assertEqual({q: a["p"] for q, a in reply["answers"].items()}, {"needs:BE-02": 0.9, "needs:FE-01": 0.8, "needs:XX-01": 0.3})
        self.assertGreater(kanvas.clef_input_tokens(reply["usage"]), 0)
        self.assertEqual(fake.calls, [{"state": "FE task", "questions": {"needs:BE-02": NOUL, "needs:FE-01": NOUL, "needs:XX-01": NOUL}}])

    def test_checks_questions_like_clef(self):
        with self.assertRaises(kanvas.ClefError):
            kanvas.FakeClef().ask("s", {})

    def test_can_fail_like_an_outage(self):
        with self.assertRaisesRegex(kanvas.ClefError, "down"):
            kanvas.FakeClef(fail="down").ask("s", {"q": NOUL})


class WorkerTests(NoNetwork):
    def test_calls_run_off_the_callers_thread(self):
        fake = kanvas.FakeClef({"q": {"type": "noul", "noul": 0.9}}, delay=0.3)
        worker = kanvas.ClefWorker(fake, log=lambda m: None)
        self.addCleanup(worker.stop)
        got = []
        start = time.monotonic()
        self.assertTrue(worker.submit("s", {"q": NOUL}, lambda r: got.append((threading.current_thread().name, r["answers"]))))
        self.assertLess(time.monotonic() - start, 0.1, "submit does not wait for Clef")
        self.assertTrue(worker.wait(5))
        self.assertEqual([(name.startswith("kanvas-clef-"), answers) for name, answers in got], [(True, {"q": {"type": "noul", "p": 0.9}})])

    def test_failures_are_logged_once_and_the_worker_goes_on(self):
        logged, got = [], []
        fake = kanvas.FakeClef(fail="Could not reach Clef: timed out")
        worker = kanvas.ClefWorker(fake, log=logged.append)
        self.addCleanup(worker.stop)
        for _ in range(3):
            worker.submit("s", {"q": NOUL}, got.append, "Notes")
        self.assertTrue(worker.wait(5))
        self.assertEqual(logged, ["Notes: Could not reach Clef: timed out"])
        fake.fail = None
        worker.submit("s", {"q": NOUL}, lambda r: 1 / 0, "Summaries")
        worker.submit("s", {"q": NOUL}, got.append)
        self.assertTrue(worker.wait(5))
        self.assertEqual(logged[1], "Summaries: ZeroDivisionError: division by zero")
        self.assertEqual(len(got), 1, "a failing use does not stop the next call")

    def test_a_newer_question_replaces_one_still_waiting(self):
        fake = kanvas.FakeClef({"q": {"type": "noul", "noul": 0.9}}, delay=0.3)
        worker = kanvas.ClefWorker(fake, log=lambda m: None, threads=1)
        self.addCleanup(worker.stop)
        got = []
        worker.submit("first", {"q": NOUL}, lambda r: got.append("busy"), key="other")
        time.sleep(0.05)  # the only thread is busy with it
        for i in range(50):
            self.assertTrue(worker.submit(f"s{i}", {"q": NOUL}, lambda r, i=i: got.append(i), key="DV-02"))
        self.assertTrue(worker.wait(5))
        self.assertEqual(got, ["busy", 49], "fifty quick changes to one task make one call, with the latest question")

    def test_a_burst_of_many_tasks_is_never_dropped(self):
        fake = kanvas.FakeClef({"q": {"type": "noul", "noul": 0.9}})
        worker = kanvas.ClefWorker(fake, log=lambda m: None)
        self.addCleanup(worker.stop)
        got = []
        for i in range(500):
            worker.submit("s", {"q": NOUL}, lambda r, i=i: got.append(i), key=("task", i))
        self.assertTrue(worker.wait(10))
        self.assertEqual(sorted(got), list(range(500)))

    def test_a_passing_failure_is_tried_again(self):
        calls = []

        class Flaky:
            def ask(self, state, questions):
                calls.append(1)
                if len(calls) < 3:
                    raise kanvas.ClefError("Clef answered HTTP 503 Service Unavailable", retry=True)
                return {"answers": {"q": {"type": "noul", "p": 0.9}}}
        worker = kanvas.ClefWorker(Flaky(), log=lambda m: None, retries=(0.01, 0.01))
        self.addCleanup(worker.stop)
        got = []
        worker.submit("s", {"q": NOUL}, got.append)
        self.assertTrue(worker.wait(5))
        self.assertEqual((len(calls), len(got)), (3, 1))

    def test_bad_credentials_turn_clef_off_with_one_message(self):
        logged, failed = [], []
        fake = kanvas.FakeClef(fail="Clef answered HTTP 401 Unauthorized")
        worker = kanvas.ClefWorker(fake, log=logged.append, threads=1)
        self.addCleanup(worker.stop)
        for i in range(20):
            worker.submit("s", {"q": NOUL}, lambda r: None, f"Dependencies (DV-{i:02})", failed.append, key=i)
        self.assertTrue(worker.wait(5))
        self.assertEqual(logged, ["Clef is off until Kanvas restarts: Clef answered HTTP 401 Unauthorized"])
        self.assertEqual(len(failed), 20)
        self.assertFalse(worker.submit("s", {"q": NOUL}, lambda r: None), "nothing more is sent")

    def test_the_start_check_says_whether_clef_answers(self):
        said = []
        worker = kanvas.ClefWorker(kanvas.FakeClef({"check": {"type": "noul", "noul": 1.0}}), log=lambda m: None)
        self.addCleanup(worker.stop)
        worker.check(lambda ok, msg: said.append((ok, msg)))
        self.assertTrue(worker.wait(5))
        self.assertTrue(said[0][0] and said[0][1].startswith("Clef answers"))
        worker2 = kanvas.ClefWorker(kanvas.FakeClef(fail="Clef answered HTTP 403 Forbidden"), log=lambda m: None)
        self.addCleanup(worker2.stop)
        worker2.check(lambda ok, msg: said.append((ok, msg)))
        self.assertTrue(worker2.wait(5))
        self.assertEqual(said[1], (False, "Clef does not answer: Clef answered HTTP 403 Forbidden"))

def board(*tasks, title="Weather dashboard"):
    return {"format": 1, "title": title, "groups": [], "tasks": [dict(t) for t in tasks]}


def t(tid, state="todo", deps=(), title=None, desc="", notes=()):
    return {"id": tid, "title": title or f"Task {tid}", "state": state, "group": None, "deps": list(deps), "desc": desc,
            "notes": list(notes)}


class QuestionBuilderTests(unittest.TestCase):
    """What each use asks Clef, built from the board alone."""

    def test_dependency_candidates_are_unfinished_and_make_no_cycle(self):
        b = board(t("A", "done"), t("B", deps=["A"]), t("C", deps=["B"]), t("D", deps=["S"]), t("E", "proposed"),
                  t("F", "review"), t("S", deps=["B"]))
        s = kanvas.find_task(b, "S")
        self.assertEqual([c["id"] for c in kanvas.clef_dep_candidates(b, s)], ["C", "E", "F"],
                         "not A (done), B (already), D (waits for S), S itself")

    def test_dependency_questions_are_split_into_calls_of_64(self):
        tasks = [t(f"T-{i:02d}") for i in range(1, 101)]
        b = board(*tasks)
        calls = kanvas.clef_dep_calls(b, b["tasks"][0])
        self.assertEqual([len(q) for _, q in calls], [64, 35])
        state, questions = calls[0]
        self.assertIn("needs:T-02", questions)
        self.assertEqual(questions["needs:T-02"]["type"], "noul")
        self.assertIn("Does T-01 need the result of T-02 first?", questions["needs:T-02"]["instructions"])
        self.assertIn('- T-02 "Task T-02" (todo)', state)
        for call in calls:
            kanvas.clef_check(*call)

    def test_long_descriptions_are_clipped(self):
        b = board(t("A", desc="word " * 1000), t("B", desc="x" * 5000))
        state, _ = kanvas.clef_dep_calls(b, kanvas.find_task(b, "A"))[0]
        self.assertLess(len(state), 1200)

    def test_suggestions_are_the_sure_ones_surest_first(self):
        answers = {"needs:B": {"type": "noul", "p": 0.72}, "needs:C": {"type": "noul", "p": 0.91},
                   "needs:D": {"type": "noul", "p": 0.4}, "repeats:B": {"type": "noul", "p": 0.99}}
        self.assertEqual(kanvas.clef_dep_readings(answers, 0.7), [{"on": "C", "p": 0.91}, {"on": "B", "p": 0.72}])
        self.assertEqual(kanvas.clef_dupe_readings(answers, 0.7), [{"of": "B", "p": 0.99}])

    def test_each_use_has_the_threshold_measured_for_it(self):
        answers = {"needs:B": {"type": "noul", "p": 0.65}, "repeats:B": {"type": "noul", "p": 0.65},
                   "repeats:C": {"type": "noul", "p": 0.7}}
        self.assertEqual(kanvas.CLEF_THRESHOLDS, {"deps": 0.6, "dupes": 0.7, "order": 0.6})
        self.assertEqual(kanvas.clef_dep_readings(answers), [{"on": "B", "p": 0.65}])
        self.assertEqual(kanvas.clef_dupe_readings(answers), [{"of": "C", "p": 0.7}])

    def test_duplicates_are_asked_about_every_other_task(self):
        b = board(t("A", "done"), t("B", "doing"), t("N", "proposed"))
        _, questions = kanvas.clef_dupe_calls(b, kanvas.find_task(b, "N"))[0]
        self.assertEqual(list(questions), ["repeats:A", "repeats:B"])

class EvalTests(NoNetwork):
    """tests/clef_eval.py is not part of the suite, but its cases must stay valid and it must run."""

    def test_every_use_has_15_valid_cases(self):
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        import clef_eval
        cases = clef_eval.load_cases()
        for use in clef_eval.USES:
            with self.subTest(use=use):
                self.assertGreaterEqual(len(cases[use]), 15)
                self.assertEqual(len({c["id"] for c in cases[use]}), len(cases[use]), "case ids are unique")
                for case in cases[use]:
                    calls, truth = clef_eval.build(use, case)
                    for call in calls:
                        kanvas.clef_check(*call)

    def test_the_runner_prints_a_table_with_the_fake(self):
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        import clef_eval
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(clef_eval.main([]), 0)
        text = out.getvalue()
        self.assertIn("plumbing fake", text)
        self.assertRegex(text, r"Missing dependencies\s+0\.60\s+\d+\s+\d+\s+100 %\s+\d+ %")
        self.assertRegex(text, r"Duplicate proposals\s+0\.70\s+24\s+24\s+100 %\s+\d+ %")

    def test_saved_answers_can_be_scored_again_at_another_threshold(self):
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        import clef_eval
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        saved = os.path.join(folder, "results.json")
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(clef_eval.main(["--json", saved]), 0)
        with open(saved, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["thresholds"], {"deps": 0.6, "dupes": 0.7, "order": 0.6})
        shown = {}
        for threshold in ("0.5", "0.9"):
            out = io.StringIO()
            with mock.patch("sys.stdout", out), mock.patch.object(kanvas.FakeClef, "ask", side_effect=AssertionError("asked")):
                self.assertEqual(clef_eval.main(["--from", saved, "--threshold", threshold, "--use", "deps"]), 0)
            self.assertIn("saved in results.json", out.getvalue())
            self.assertNotIn("Duplicate proposals", out.getvalue())
            shown[threshold] = int(re.search(r"Missing dependencies\s+\S+\s+\d+\s+\d+\s+100 %\s+\d+ %\s+\d+ % of (\d+)", out.getvalue()).group(1))
        self.assertGreater(shown["0.5"], shown["0.9"], "a lower threshold shows more")

    def test_the_cost_estimate_sends_nothing_and_covers_every_use(self):
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        import clef_eval
        text, rows = clef_eval.costs()
        self.assertIn("examples/sample.json", text)
        sample = rows[0]
        self.assertEqual(sample["tasks"], 15)
        self.assertEqual(sorted(sample["life"]), sorted(clef_eval.USES))
        self.assertTrue(all(calls and tokens for calls, tokens in sample["life"].values()))
        self.assertEqual(rows[-1]["tasks"], 105)

    def test_the_runner_asks_cloudflare_only_with_the_opt_in(self):
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        import clef_eval
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"KANVAS_CLEF": "1"}, clear=True), mock.patch("sys.stderr", err):
            self.assertEqual(clef_eval.main(["--real"]), 2)
        self.assertIn("CLOUDFLARE_ACCOUNT_ID", err.getvalue())


class BoardWithAgent(NoNetwork):
    """A Kanvas server with Clef, the sample board, and a connected agent session (a Channel writing to a buffer)."""

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="kanvas-clef-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        env = mock.patch.dict(os.environ, {"KANVAS_STATE_DIR": os.path.join(self.dir, "state")})
        env.start()
        self.addCleanup(env.stop)
        os.mkdir(os.path.join(self.dir, "weather"))
        self.path = os.path.join(self.dir, "weather", "kanvas.json")
        shutil.copyfile(os.path.join(ROOT, "examples", "sample.json"), self.path)
        self.out = io.StringIO()
        self.channel = kanvas.Channel(self.path, "Test agent", stdout=self.out)
        self.channel.beat()
        self.channel.ready.set()

    def serve(self, clef):
        self.server = kanvas.BoardServer(("127.0.0.1", 0), self.path, quiet=True, clef=clef)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.name = self.server.board_list()[0]["name"]

    def request(self, method, path, body=None, headers=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        try:
            conn.request(method, f"/{self.name}{path}", body=body, headers=dict(headers or {}))
            r = conn.getresponse()
            return r.status, r.read(), r.getheader("ETag")
        finally:
            conn.close()

    def change(self, fn):
        """Save the board as the browser does, after `fn(board)` changed it."""
        status, data, tag = self.request("GET", "/api/board")
        board = kanvas.parse_board(data.decode("utf-8"))
        fn(board)
        status, data, _ = self.request("PUT", "/api/board", kanvas.dumps(board).encode("utf-8"),
                                       {"Content-Type": "application/json", "If-Match": tag})
        self.assertEqual(status, 200, data)

    def add_note(self, tid, text):
        self.change(lambda b: kanvas.find_task(b, tid).setdefault("notes", []).append(
            {"at": kanvas.now_iso(), "by": "human", "text": text}))

    def messages(self):
        self.channel.deliver_pending()
        return [json.loads(line)["params"] for line in self.out.getvalue().splitlines() if line]


class NoticeTests(BoardWithAgent):
    """Clef changes nothing in what the agent is told."""

    def test_with_clef_on_a_notes_notice_goes_at_once_as_without_it(self):
        fake = kanvas.FakeClef(default={"type": "noul", "noul": 0.1})
        self.serve(fake)
        self.add_note("BE-03", "Does the geocoding API need a key?")
        [msg] = self.messages()
        self.assertNotIn("Clef", msg["content"])
        self.assertIn("> Does the geocoding API need a key?\n", msg["content"])
        self.assertNotIn("clef", kanvas.deliveries(self.path)[0])
        self.assertTrue(self.server.clef_worker.wait(5))
        self.assertEqual(fake.calls, [], "a note asks Clef nothing")

    def test_the_token_is_written_nowhere(self):
        stub = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        stub.daemon_threads, stub.seen = True, []
        stub.reply = (200, {"result": {"answers": {"needs:BE-02": {"type": "noul", "noul": 0.9}}}, "success": True}, 0)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        self.addCleanup(stub.server_close)
        self.addCleanup(stub.shutdown)
        self.serve(kanvas.ClefClient("acc123", "tok-secret", url=f"http://127.0.0.1:{stub.server_address[1]}/clef"))
        wait_for("the server to read the board once", lambda: self.path in self.server.clef_seen)
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(kanvas.main(["--board", self.path, "propose", "-", "Weather alerts", "Notify when rain starts", "--as", "test"]), 0)
        wait_for("the suggestion", lambda: (kanvas.clef_store(self.path)["deps"].get("G-01") or {}).get("items"))
        self.assertTrue(self.server.clef_worker.wait(5))
        self.assertEqual(stub.seen[0]["headers"]["Authorization"], "Bearer tok-secret")
        for folder, _, files in os.walk(self.dir):
            for name in files:
                with open(os.path.join(folder, name), "rb") as f:
                    self.assertNotIn(b"tok-secret", f.read(), os.path.join(folder, name))


class ServeTests(NoNetwork):
    """`kanvas serve` says whether Clef is on, and why not when the opt-in is incomplete."""

    def serve(self, env):
        import socketserver
        folder = tempfile.mkdtemp(prefix="kanvas-serve-")
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "kanvas.json")
        shutil.copyfile(os.path.join(ROOT, "examples", "sample.json"), path)
        made = []

        def start(path, host, port, remember=None, clef=None):
            with mock.patch.object(socketserver.TCPServer, "server_bind"), mock.patch.object(socketserver.TCPServer, "server_activate"):
                server = kanvas.BoardServer((host, port), path, quiet=True, clef=clef)
            server.server_address, server.phone_wanted, server.serve_forever = ("127.0.0.1", 8765), False, lambda: None
            made.append(clef)
            return server
        env = dict(env, KANVAS_STATE_DIR=os.path.join(folder, "state"))
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(kanvas, "find_running", return_value=None), \
                mock.patch.object(kanvas, "start_server", start), mock.patch("sys.stdout", out):
            kanvas.main(["--board", path, "serve", "--no-open"])
        return out.getvalue(), made[0] if made else "no server"

    def test_off_by_default(self):
        out, clef = self.serve({})
        self.assertNotIn("Clef", out)
        self.assertIsNone(clef)

    def test_an_incomplete_opt_in_says_what_is_missing(self):
        out, clef = self.serve({"KANVAS_CLEF": "1", "CLOUDFLARE_ACCOUNT_ID": "acc"})
        self.assertIn("Clef stays off: KANVAS_CLEF=1 also needs CLOUDFLARE_API_TOKEN.", out)
        self.assertIsNone(clef)

    def test_on_with_the_opt_in(self):
        out, clef = self.serve(CREDENTIALS)
        self.assertIn("Clef: on, checking that it answers. The board's tasks go to Cloudflare's Clef", out)
        self.assertIsInstance(clef, kanvas.ClefClient)
        self.assertNotIn("tok-secret", out)

    def test_a_fake_table_says_nothing_leaves(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        with open(os.path.join(folder, "fake.json"), "w") as f:
            f.write("{}")
        out, clef = self.serve({"KANVAS_CLEF_FAKE": os.path.join(folder, "fake.json")})
        self.assertIn("nothing leaves this computer", out)
        self.assertIsInstance(clef, kanvas.FakeClef)
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            self.assertEqual(self.serve({"KANVAS_CLEF_FAKE": os.path.join(folder, "missing.json")})[1], "no server")
        self.assertIn("Cannot read the fake Clef table", err.getvalue())


def noul(p):
    return {"type": "noul", "noul": p}


def wait_for(what, check, timeout=10):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"Waited {timeout} s for {what}")
        time.sleep(0.05)


class SuggestionBoard(BoardWithAgent):
    """A board served with a fake Clef answering by `rules`, and what the page reads of Clef's suggestions."""

    rules = []

    def setUp(self):
        super().setUp()
        self.fake = kanvas.FakeClef(rules=self.rules, default=noul(0.1))
        self.serve(self.fake)
        wait_for("the server to read the board once", lambda: self.path in self.server.clef_seen)

    def clef(self):
        status, data, _ = self.request("GET", "/api/clef")
        self.assertEqual(status, 200)
        return json.loads(data)

    def dismiss(self, body):
        status, data, _ = self.request("POST", "/api/clef/dismiss", json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"})
        return status, json.loads(data)


class DepSuggestionTests(SuggestionBoard):
    """Clef suggests dependencies a task may be missing; only the human's click adds one."""

    rules = [("needs:BE-05", 'planned:\nFE-05 "', noul(0.82)), ("needs:FE-04", 'planned:\nFE-05 "', noul(0.75)),
             ("needs:BE-02", 'planned:\nG-01 "', noul(0.9))]

    def asked_about(self, tid):
        return [c for c in self.fake.calls if f'planned:\n{tid} "' in c["state"]]

    def test_suggestions_show_and_change_nothing_on_the_board(self):
        self.change(lambda b: kanvas.find_task(b, "FE-05").update(state="todo"))
        view = wait_for("suggestions for FE-05", lambda: self.clef()["deps"].get("FE-05"))
        self.assertEqual(view, [{"on": "BE-05", "p": 0.82}, {"on": "FE-04", "p": 0.75}])
        self.assertTrue(self.clef()["on"])
        self.assertEqual(kanvas.find_task(kanvas.load_board(self.path), "FE-05")["deps"], ["FE-02"])
        with open(self.path, encoding="utf-8") as f:
            self.assertNotIn("Clef", f.read())
        self.assertTrue(os.path.isfile(kanvas.clef_store_path(self.path)), "kept in the state folder")
        [call] = self.asked_about("FE-05")
        self.assertNotIn("needs:FE-02", call["questions"], "not what it already waits for")
        self.assertNotIn("needs:BE-01", call["questions"], "not finished tasks")

    def test_dismissed_suggestions_never_come_back(self):
        self.change(lambda b: kanvas.find_task(b, "FE-05").update(state="todo"))
        wait_for("suggestions for FE-05", lambda: self.clef()["deps"].get("FE-05"))
        status, view = self.dismiss({"use": "deps", "task": "FE-05", "item": "FE-04"})
        self.assertEqual(status, 200)
        self.assertEqual(view["deps"]["FE-05"], [{"on": "BE-05", "p": 0.82}])
        self.change(lambda b: kanvas.find_task(b, "FE-05").update(desc="Show the last forecast with a banner, and retry."))
        wait_for("Clef asked again", lambda: len(self.asked_about("FE-05")) == 2)
        self.assertNotIn("needs:FE-04", self.asked_about("FE-05")[1]["questions"], "not even asked again")
        self.assertTrue(self.server.clef_worker.wait(5))
        self.assertEqual(self.clef()["deps"]["FE-05"], [{"on": "BE-05", "p": 0.82}])

    def test_bad_dismissals_are_refused(self):
        for body in ({"use": "nope", "task": "FE-05", "item": "FE-04"}, {"use": "deps", "task": "", "item": "FE-04"}, ["x"]):
            with self.subTest(body=body):
                self.assertEqual(self.dismiss(body)[0], 400)

    def test_an_added_dependency_is_no_longer_suggested(self):
        self.change(lambda b: kanvas.find_task(b, "FE-05").update(state="todo"))
        wait_for("suggestions for FE-05", lambda: self.clef()["deps"].get("FE-05"))
        self.change(lambda b: kanvas.find_task(b, "FE-05")["deps"].append("BE-05"))
        self.assertEqual(self.clef()["deps"]["FE-05"], [{"on": "FE-04", "p": 0.75}], "at once, before Clef answers again")

    def test_an_agents_new_task_through_the_cli_gets_suggestions(self):
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(kanvas.main(["--board", self.path, "propose", "-", "Weather alerts", "Notify when rain starts", "--as", "test"]), 0)
        self.assertEqual(wait_for("suggestions for G-01", lambda: self.clef()["deps"].get("G-01")), [{"on": "BE-02", "p": 0.9}])

    def test_without_clef_the_board_says_it_is_off(self):
        self.server.shutdown()
        self.server.server_close()
        self.serve(None)
        self.assertEqual(self.clef(), {"on": False})


class DuplicateTests(SuggestionBoard):
    """Clef flags a new proposal that repeats a task already on the board."""

    rules = [("repeats:BE-05", 'A new proposal:\nG-01 "', noul(0.77)), ("repeats:FE-05", 'A new proposal:\nG-01 "', noul(0.71))]

    def propose(self, title, desc=""):
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(kanvas.main(["--board", self.path, "propose", "-", title, desc, "--as", "test"]), 0)

    def test_an_agents_proposal_that_repeats_a_task_is_flagged(self):
        self.propose("Retry Open-Meteo requests when they fail", "Up to three retries with backoff.")
        dupes = wait_for("the flag on G-01", lambda: self.clef()["dupes"].get("G-01"))
        self.assertEqual(dupes, [{"of": "BE-05", "p": 0.77}, {"of": "FE-05", "p": 0.71}])
        [call] = [c for c in self.fake.calls if any(q.startswith("repeats:") for q in c["questions"])]
        self.assertEqual(len(call["questions"]), 15, "every other task, done ones too")
        self.assertIn("repeats:RS-01", call["questions"])

    def test_the_flag_goes_once_the_proposal_is_approved(self):
        self.propose("Retry Open-Meteo requests when they fail")
        wait_for("the flag on G-01", lambda: self.clef()["dupes"].get("G-01"))
        self.change(lambda b: kanvas.find_task(b, "G-01").update(state="todo"))
        self.assertNotIn("G-01", self.clef()["dupes"])

    def test_a_dismissed_flag_goes_and_the_other_stays(self):
        self.propose("Retry Open-Meteo requests when they fail")
        wait_for("the flag on G-01", lambda: self.clef()["dupes"].get("G-01"))
        status, view = self.dismiss({"use": "dupes", "task": "G-01", "item": "BE-05"})
        self.assertEqual(status, 200)
        self.assertEqual(view["dupes"]["G-01"], [{"of": "FE-05", "p": 0.71}])

    def test_editing_an_old_proposal_does_not_ask_again(self):
        with mock.patch("sys.stdout", io.StringIO()):
            kanvas.main(["--board", self.path, "edit", "BE-05", "--desc", "Retry three times.", "--as", "test"])
        wait_for("Clef asked about BE-05's dependencies", lambda: any('planned:\nBE-05 "' in c["state"] for c in self.fake.calls))
        self.assertTrue(self.server.clef_worker.wait(5))
        self.assertFalse([c for c in self.fake.calls if "A new proposal" in c["state"]])


if __name__ == "__main__":
    unittest.main()


class OrderSuggestionTests(SuggestionBoard):
    """Clef suggests an order for tasks agents could work on at the same time that would change the same code; only
    the human's click adds the dependency."""

    rules = [("clash:FE-02", 'The task:\nFE-04 "', noul(0.8))]

    def asked_about(self, tid):
        return [c for c in self.fake.calls if f'The task:\n{tid} "' in c["state"]]

    def test_a_task_that_starts_is_checked_against_the_work_in_progress(self):
        def start(b):
            kanvas.find_task(b, "FE-04").update(state="doing", started={"at": "2026-01-01T00:00:00Z", "by": "human"})
        self.change(start)
        view = wait_for("an order suggestion for FE-04", lambda: self.clef()["order"].get("FE-04"))
        self.assertEqual(view, [{"with": "FE-02", "p": 0.8}])
        [call] = self.asked_about("FE-04")
        self.assertEqual(sorted(call["questions"]), ["clash:BE-03", "clash:FE-02"],
                         "only approved tasks in progress or ready, not proposals, blocked tasks or reviews")

    def test_ordering_the_two_tasks_ends_the_suggestion(self):
        self.change(lambda b: kanvas.find_task(b, "FE-04").update(desc="Dark theme for the cards and the header."))
        wait_for("an order suggestion for FE-04", lambda: self.clef()["order"].get("FE-04"))
        self.change(lambda b: kanvas.find_task(b, "FE-04")["deps"].append("FE-02"))
        self.assertNotIn("FE-04", self.clef()["order"], "at once, before Clef answers again")

    def test_a_dismissed_order_never_comes_back(self):
        self.change(lambda b: kanvas.find_task(b, "FE-04").update(desc="Dark theme for the cards."))
        wait_for("an order suggestion for FE-04", lambda: self.clef()["order"].get("FE-04"))
        status, view = self.dismiss({"use": "order", "task": "FE-04", "item": "FE-02"})
        self.assertEqual((status, view["order"].get("FE-04")), (200, None))

