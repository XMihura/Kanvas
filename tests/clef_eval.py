"""How well Clef answers the three questions Kanvas asks it, measured on the labelled cases in tests/clef_cases.json.
Not part of the unit suite.

    python tests/clef_eval.py                    # plumbing: a fake that answers from the labels, with made-up mistakes
    python tests/clef_eval.py --real             # Cloudflare's Clef: needs KANVAS_CLEF=1, CLOUDFLARE_ACCOUNT_ID and
                                                 # CLOUDFLARE_API_TOKEN; 46 calls, about 0.03 USD
    python tests/clef_eval.py --from results.json --threshold 0.6   # score saved answers again; sends nothing
    python tests/clef_eval.py --use deps -v --json results.json

Per use it prints: accuracy on every answer; how often what Kanvas would show is right; recall; calibration (whether
answers given with 80 % are yes about 80 % of the time); latency; input tokens and cost per call. Each use is scored
at its own threshold (kanvas.CLEF_THRESHOLDS), or at --threshold for all.
"""

import argparse
import copy
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import kanvas  # noqa: E402

CASES = os.path.join(ROOT, "tests", "clef_cases.json")
USES = ("deps", "dupes", "order")
NAMES = {"deps": "Missing dependencies", "dupes": "Duplicate proposals", "order": "Work that collides"}


class CaseError(Exception):
    pass


def board_of(case):
    return copy.deepcopy(kanvas.load_board(os.path.join(ROOT, case["board"])))


def build(use, case):
    """([(state, questions)], {question id: truth}) for one case. Truth is True, False or None (true, but only through
    other tasks: left out of the measures)."""
    board = board_of(case)
    if use == "deps":
        task = kanvas.find_task(board, case["task"])
        task["deps"] = [d for d in task.get("deps", []) if d not in case.get("remove", [])]
        calls = kanvas.clef_dep_calls(board, task)
        asked = {qid[len("needs:"):] for _, qs in calls for qid in qs}
        for tid in case["label"] + case.get("also_true", []):
            if tid not in asked:
                raise CaseError(f"{case['id']}: {tid} is not one of the candidates Kanvas asks about")
        truth = {f"needs:{tid}": True if tid in case["label"] else None if tid in case.get("also_true", []) else False
                 for tid in asked}
        return calls, truth
    if use == "dupes":
        task = {"id": "NP-01", "title": case["title"], "state": "proposed", "group": None, "deps": [], "desc": case.get("desc", "")}
        board["tasks"].append(task)
        calls = kanvas.clef_dupe_calls(board, task)
        asked = {qid[len("repeats:"):] for _, qs in calls for qid in qs}
        if case["label"] is not None and case["label"] not in asked:
            raise CaseError(f"{case['id']}: {case['label']} is not on the board")
        truth = {f"repeats:{tid}": tid == case["label"] or (None if tid in case.get("also_true", []) else False) for tid in asked}
        return calls, truth
    if use == "order":
        # The tasks agents could work on at the same time as the task, as the board would have them then.
        task = kanvas.find_task(board, case["task"])
        others = [kanvas.find_task(board, tid) for tid in case["with"]]
        calls = kanvas.clef_order_calls(board, task, others)
        truth = {f"clash:{tid}": True if tid in case["label"] else None if tid in case.get("also_true", []) else False
                 for tid in case["with"]}
        return calls, truth
    raise CaseError(f"Unknown use {use}")


def _unit(*parts):
    """A number in [0, 1) that depends only on `parts`: the fake's mistakes are the same on every run."""
    return int(hashlib.sha256(":".join(map(str, parts)).encode("utf-8")).hexdigest()[:12], 16) / float(16 ** 12)


def plumbing_answer(case_id, qid, truth, accuracy):
    """What the fake says: right with probability `accuracy`, surer when right than when wrong."""
    right = _unit(case_id, qid, "right") < accuracy
    r = _unit(case_id, qid, "p")
    if truth is None:
        return {"type": "noul", "noul": round(r, 3)}
    yes = truth if right else not truth
    return {"type": "noul", "noul": round(0.6 + 0.39 * r if yes else 0.01 + 0.39 * r, 3)}


def evaluate(cases, clef_for, uses=USES):
    """Ask every case; return {use: {"records", "calls", "cases"}}. `clef_for(use, case, truth)` gives the Clef to ask.
    Records keep Clef's probability; the thresholds apply when scoring."""
    out = {}
    for use in uses:
        records, calls = [], []
        for case in cases[use]:
            built, truth = build(use, case)
            clef = clef_for(use, case, truth)
            answers = {}
            for state, questions in built:
                try:
                    reply = clef.ask(state, questions)
                except kanvas.ClefError as e:
                    calls.append({"case": case["id"], "error": str(e)})
                    continue
                tokens = kanvas.clef_input_tokens(reply["usage"])
                calls.append({"case": case["id"], "seconds": reply["seconds"], "questions": len(questions),
                              "tokens": tokens if tokens is not None else kanvas.clef_tokens(state, questions),
                              "estimated": tokens is None})
                answers.update(reply["answers"])
            for qid, want in truth.items():
                a = answers.get(qid)
                rec = {"case": case["id"], "question": qid, "truth": want, "hard": bool(case.get("hard"))}
                if a is None:
                    rec["missing"] = True
                else:
                    rec["p"] = a["p"]
                records.append(rec)
        out[use] = {"records": records, "calls": calls, "cases": len(cases[use])}
    return out


def _share(n, d):
    return n / d if d else None


def calibration(points, bins=10):
    """[(low, high, n, mean said, share true)] and the expected calibration error, from (probability, outcome) pairs."""
    rows, err = [], 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        inside = [(p, o) for p, o in points if lo <= p < hi or (b == bins - 1 and p == 1.0)]
        if inside:
            said = sum(p for p, _ in inside) / len(inside)
            true = sum(1 for _, o in inside if o) / len(inside)
            rows.append((lo, hi, len(inside), said, true))
            err += len(inside) / len(points) * abs(said - true)
    return rows, (err if points else None)


def metrics(use, result, threshold):
    recs = [r for r in result["records"] if not r.get("missing") and r["truth"] is not None]
    asked = [r for r in result["records"] if r["truth"] is not None]
    ok_calls = [c for c in result["calls"] if "error" not in c]
    shown = lambda r: r["p"] >= threshold
    tp = sum(1 for r in recs if shown(r) and r["truth"])
    fp = sum(1 for r in recs if shown(r) and not r["truth"])
    fn = sum(1 for r in recs if not shown(r) and r["truth"])
    m = {"use": use, "threshold": threshold, "cases": result["cases"], "calls": len(result["calls"]),
         "failed calls": len(result["calls"]) - len(ok_calls), "answered": _share(len(recs), len(asked)),
         "accuracy": _share(sum(1 for r in recs if (r["p"] >= 0.5) == r["truth"]), len(recs)),
         "right when shown": _share(tp, tp + fp), "recall": _share(tp, tp + fn), "shown": tp + fp,
         "calibration": calibration([(r["p"], r["truth"]) for r in recs])}
    secs = sorted(c["seconds"] for c in ok_calls)
    m["latency"] = (sum(secs) / len(secs), secs[min(len(secs) - 1, int(0.95 * len(secs)))]) if secs else None
    tokens = [c["tokens"] for c in ok_calls]
    m["tokens per call"] = sum(tokens) / len(tokens) if tokens else None
    m["tokens estimated"] = any(c.get("estimated") for c in ok_calls)
    m["usd per 1000 calls"] = m["tokens per call"] * 1000 * kanvas.CLEF_PRICE / 1e6 if tokens else None
    return m


def pct(x):
    return "—" if x is None else f"{100 * x:.0f} %"


def table(rows, head):
    widths = [max(len(str(r[i])) for r in [head] + rows) for i in range(len(head))]
    line = lambda r: "  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip()
    return "\n".join([line(head), line(["-" * w for w in widths])] + [line(r) for r in rows])


def report(results, thresholds, source, verbose=False):
    ms = [metrics(use, res, thresholds[use]) for use, res in results.items()]
    head = ["Use", "Threshold", "Cases", "Calls", "Answered", "Accuracy", "Right when shown", "Recall", "Calibration error",
            "Latency avg / p95", "Input tokens/call", "USD per 1000 calls"]
    rows = []
    for m in ms:
        lat = "—" if m["latency"] is None else f"{m['latency'][0]:.2f} / {m['latency'][1]:.2f} s"
        tok = "—" if m["tokens per call"] is None else f"{m['tokens per call']:.0f}" + (" (est.)" if m["tokens estimated"] else "")
        usd = "—" if m["usd per 1000 calls"] is None else f"{m['usd per 1000 calls']:.3f}"
        ece = m["calibration"][1]
        calls = f"{m['calls']}" + (f" ({m['failed calls']} failed)" if m["failed calls"] else "")
        rows.append([NAMES[m["use"]], f"{m['threshold']:.2f}", m["cases"], calls, pct(m["answered"]), pct(m["accuracy"]),
                     pct(m["right when shown"]) + (f" of {m['shown']}" if m["shown"] else ""), pct(m["recall"]),
                     "—" if ece is None else f"{ece:.2f}", lat, tok, usd])
    out = [f"Clef evaluation: {source}", "", table(rows, head), ""]
    out.append("Calibration: of the answers given with each probability, how many were yes")
    for m in ms:
        bins = m["calibration"][0]
        cells = ", ".join(f"{lo:.1f}-{hi:.1f}: {n} said {said:.2f}, {true:.2f} true" for lo, hi, n, said, true in bins)
        out.append(f"  {NAMES[m['use']]}: {cells or 'no answers'}")
    if verbose:
        out += ["", "Wrong or missing answers (what Kanvas would show wrongly, or miss):"]
        for use, res in results.items():
            for r in res["records"]:
                if r["truth"] is None:
                    continue
                if r.get("missing"):
                    out.append(f"  {use} {r['case']} {r['question']}: no answer")
                elif (r["p"] >= thresholds[use]) != r["truth"]:
                    out.append(f"  {use} {r['case']} {r['question']}: p={r['p']:.2f}, truth {'yes' if r['truth'] else 'no'}{' (hard)' if r['hard'] else ''}")
            for c in res["calls"]:
                if "error" in c:
                    out.append(f"  {use} {c['case']}: {c['error']}")
    return "\n".join(out), ms


# What Clef costs over a board's life, from the questions Kanvas would really send. Nothing is sent: the tokens are
# counted from the text (kanvas.clef_tokens, fitted to what Clef counted), so this works without credentials.
# Tasks agents work on, or could start, at the same time as one task: what the order question compares it with.
ORDER_SIDE_BY_SIDE = 8
LIFE = ("every task is proposed by an agent (one duplicate check), then created, approved and edited once "
        "(three dependency checks), and becomes ready and starts (two order checks against 8 other tasks)")
BOARDS = ("examples/sample.json", "examples/drone.json", "tests/fixtures/phases.json", "tests/fixtures/tetris.json")


def big_board(copies=5):
    """A 105-task board: five copies of the Tetris board, as a large project."""
    base = kanvas.load_board(os.path.join(ROOT, "tests", "fixtures", "tetris.json"))
    tasks = []
    for n in range(copies):
        for t in base["tasks"]:
            copy_id = lambda tid: "{0}{2}-{1}".format(*tid.split("-"), n)
            tasks.append(dict(t, id=copy_id(t["id"]), deps=[copy_id(d) for d in t.get("deps", [])], state="todo"))
    return dict(base, title="Tetris, five times over", tasks=tasks)


def board_life(board):
    """{use: (calls, input tokens)} over the life of `board` as LIFE describes it."""
    out = {use: [0, 0] for use in USES}

    def add(use, calls):
        for state, questions in calls:
            out[use][0] += 1
            out[use][1] += kanvas.clef_tokens(state, questions)
    for t in board["tasks"]:
        add("dupes", kanvas.clef_dupe_calls(board, dict(t, state="proposed")))
        for _ in range(3):
            add("deps", kanvas.clef_dep_calls(board, dict(t, state="todo")))
        # Asked when it becomes ready and when it starts, against the work agents could do at the same time.
        others = [o for o in board["tasks"] if o["id"] != t["id"]][:ORDER_SIDE_BY_SIDE]
        for _ in range(2):
            add("order", kanvas.clef_order_calls(board, t, others))
    return {use: tuple(v) for use, v in out.items()}


def costs():
    rows, out = [], []
    boards = [(path, kanvas.load_board(os.path.join(ROOT, path))) for path in BOARDS] + [("105 tasks (Tetris x5)", big_board())]
    for name, board in boards:
        life = board_life(board)
        calls = sum(c for c, _ in life.values())
        tokens = sum(k for _, k in life.values())
        cells = [f"{life[u][1] / 1000:.0f}k" for u in USES]
        rows.append([name, len(board["tasks"]), calls] + cells + [f"{tokens / 1000:.0f}k", f"{tokens * kanvas.CLEF_PRICE / 1e6:.3f}"])
        out.append({"board": name, "tasks": len(board["tasks"]), "life": life, "usd": tokens * kanvas.CLEF_PRICE / 1e6})
    head = ["Board", "Tasks", "Calls"] + [f"{NAMES[u]} tokens" for u in USES] + ["All tokens", "USD"]
    return f"Clef over a board's life, if {LIFE}:\n\n" + table(rows, head), out


def load_cases(path=CASES):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_results(path, uses):
    """The answers a run saved with --json, for the uses asked that it has."""
    with open(path, encoding="utf-8") as f:
        saved = json.load(f)
    return saved.get("source", path), {use: saved["results"][use] for use in uses if use in saved.get("results", {})}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--real", action="store_true", help="ask Cloudflare's Clef (needs the opt-in and both credentials)")
    p.add_argument("--from", dest="saved", metavar="PATH", help="score the answers a run saved with --json; sends nothing")
    p.add_argument("--use", choices=USES, action="append", help="only this use (repeat for more)")
    p.add_argument("--threshold", type=float, default=None, help="for every use (default: each use's own, kanvas.CLEF_THRESHOLDS)")
    p.add_argument("--fake-accuracy", type=float, default=0.8, help="how often the plumbing fake is right (default 0.8)")
    p.add_argument("--cases", default=CASES, help="the labelled cases (default tests/clef_cases.json)")
    p.add_argument("--json", metavar="PATH", help="also write every answer and measure to this file")
    p.add_argument("-v", "--verbose", action="store_true", help="list the wrong answers")
    p.add_argument("--costs", action="store_true", help="only estimate the input tokens and cost over a board's life (sends nothing)")
    args = p.parse_args(argv)
    if args.costs:
        print(costs()[0])
        return 0
    uses = tuple(args.use or USES)
    thresholds = {use: args.threshold if args.threshold is not None else kanvas.CLEF_THRESHOLDS[use] for use in USES}
    if args.saved:
        source, results = load_results(args.saved, uses)
        source += f", saved in {os.path.basename(args.saved)}"
    else:
        cases = load_cases(args.cases)
        for use in uses:
            if len(cases[use]) < 15:
                print(f"Warning: only {len(cases[use])} cases for {use}", file=sys.stderr)
        if args.real:
            clef = kanvas.clef_from_env()
            if not isinstance(clef, kanvas.ClefClient):
                missing = kanvas.clef_missing() or ["KANVAS_CLEF=1"]
                print(f"--real asks Cloudflare, and needs {', '.join(missing)} in the environment", file=sys.stderr)
                return 2
            tokens = sum(kanvas.clef_tokens(s, q) for use in uses for c in cases[use] for s, q in build(use, c)[0])
            print(f"Asking Cloudflare's Clef: about {tokens} input tokens, about {tokens * kanvas.CLEF_PRICE / 1e6:.4f} USD", file=sys.stderr)
            clef_for = lambda use, case, truth: clef
            source = "Cloudflare's Clef"
        else:
            def clef_for(use, case, truth):
                return kanvas.FakeClef({qid: (lambda s, q, qid=qid, want=want: plumbing_answer(case["id"], qid, want, args.fake_accuracy))
                                        for qid, want in truth.items()})
            source = f"plumbing fake, right {100 * args.fake_accuracy:.0f} % of the time (not a measurement of Clef)"
        results = evaluate(cases, clef_for, uses)
    text, ms = report(results, thresholds, source, args.verbose)
    print(text)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"source": source, "thresholds": {u: thresholds[u] for u in results}, "metrics": ms, "results": results},
                      f, indent=2, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
