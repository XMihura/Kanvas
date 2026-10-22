"""Measure the clutter of kanvas.html's graph layout for one or more boards.

    python tests/layout_stats.py [BOARD.json ...] [-v] [--manual]

Runs the layout code from kanvas.html under Node.js and prints, per board and
orientation, the number of drawn arrows, pairs of arrows that cross, arrows that
run under other cards, arrows drawn as one straight line, bends (runs of curve
that shift across the flow), and the total arrow length in pixels. -v lists which
arrows cross and which cards hide which arrows. --manual measures the manual
layout, where cards keep their order in the file. Without board arguments it
measures the example and the test fixtures.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BOARDS = [
    os.path.join(ROOT, "examples", "sample.json"),
    os.path.join(ROOT, "tests", "fixtures", "tetris.json"),
]

# `misordered` counts pairs of cards in one lane and depth that appear in the
# opposite order of the file.
RUNNER = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = [];
for (const { text, vertical, levelGap, manual } of input) {
  const b = normalize(JSON.parse(text));
  const L = layout(b.tasks, b, { vertical, levelGap, manual });
  const m = layoutMetrics(L);
  const along = (p) => (vertical ? p.y : p.x), across = (p) => (vertical ? p.x : p.y);
  const cards = b.tasks.map((t) => ({ group: t.group, p: L.pos.get(key(t.id)) }));
  m.misordered = 0;
  cards.forEach((a, i) => cards.slice(i + 1).forEach((c) => {
    if (a.group === c.group && along(a.p) === along(c.p) && across(a.p) > across(c.p)) m.misordered++;
  }));
  out.push(m);
}
process.stdout.write(JSON.stringify(out));
"""


def layout_js():
    with open(os.path.join(ROOT, "kanvas.html"), encoding="utf-8") as f:
        html = f.read()
    return html[html.index("const STATES ="):html.index("// App state")]


def measure(paths, orientations=(False, True), level_gap=70, manual=False):
    """Return {(path, vertical): metrics} for every board and orientation."""
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node is not installed")
    jobs = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        jobs += [(path, v, text) for v in orientations]
    payload = [{"text": text, "vertical": v, "levelGap": level_gap, "manual": manual} for _, v, text in jobs]
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(layout_js() + RUNNER)
    try:
        proc = subprocess.run([node, f.name], input=json.dumps(payload).encode("utf-8"), capture_output=True)
    finally:
        os.remove(f.name)
    if proc.returncode:
        raise RuntimeError(proc.stderr.decode())
    return {(path, v): m for (path, v, _), m in zip(jobs, json.loads(proc.stdout))}


def main(argv):
    verbose, manual = "-v" in argv, "--manual" in argv
    paths = [a for a in argv if a not in ("-v", "--manual")] or DEFAULT_BOARDS
    print(f"{'board':<20} {'flow':<6} {'arrows':>6} {'cross':>6} {'behind':>6} {'straight':>8} {'bends':>6} {'length':>8}")
    for (path, vertical), m in measure(paths, manual=manual).items():
        flow = "down" if vertical else "right"
        print(f"{os.path.basename(path):<20} {flow:<6} {m['arrows']:>6} {m['crossings']:>6} {m['behind']:>6} {m['straight']:>8} {m['bends']:>6} {m['length']:>8}")
        if verbose:
            for b in m["detail"]["behind"]:
                print(f"    {b['from']} -> {b['to']} runs under {', '.join(b['cards'])}")
            for a, b in m["detail"]["crossings"]:
                print(f"    {a.replace('>', ' -> ')} crosses {b.replace('>', ' -> ')}")


if __name__ == "__main__":
    main(sys.argv[1:])
