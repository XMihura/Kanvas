#!/usr/bin/env python3
"""
canvas-tool.py — CLI tool to manage an Obsidian Canvas workflow board.

This is the ONLY way AI agents should modify .canvas files. It enforces all
workflow rules (valid color transitions, dependency cycle detection, blocked
state management) so agents cannot make invalid changes.

Usage: python canvas-tool.py "<file>.canvas" <command> [args]
       python canvas-tool.py init [TARGET_DIR] [--no-plugin]

Setup:
  init [TARGET_DIR] [--no-plugin]   Initialize Kanvas in a project directory

Read-only commands:
  status                          Board overview (groups, states, anomalies)
  show <TASK-ID>                  Full task detail with dependencies
  list [STATE|GROUP]              List tasks (all, or filtered)
  blocked                         Gray tasks and what blocks them
  blocking                        Non-green tasks that block others
  ready                           Red tasks with all deps met
  dump                            Raw canvas JSON

Task lifecycle:
  start <TASK-ID>                 Red → Orange (rejects if deps unmet)
  finish <TASK-ID>                Orange → Cyan
  pause <TASK-ID>                 Orange → Red

Proposals (creates purple cards):
  propose <GROUP> "<TITLE>" "<DESC>" [--depends-on ID ...]
  propose-group "<LABEL>"
  batch                           Bulk-add from JSON on stdin

Editing:
  edit <TASK-ID> "<TEXT>"          Replace description (must be orange)
  edit <TASK-ID> --append "<TEXT>" Append to existing description
  edit <TASK-ID> --stdin           Read new text from stdin
  edit <TASK-ID> --file <PATH>    Read new text from a file
  add-dep <FROM-ID> <TO-ID>       Add dependency (rejects if cycle)

Maintenance:
  normalize                       Assign IDs, update blocked states

Batch JSON format (pipe to stdin):
  {
    "groups": ["GroupA", "GroupB"],
    "tasks": [
      {"group": "GroupA", "title": "...", "desc": "...", "depends_on": ["ID or title"]}
    ]
  }
  Tasks can reference earlier batch items by title (case-insensitive).

Rules enforced:
  - Only purple cards can be created by agents
  - Only red→orange, orange→cyan, orange→red transitions allowed
  - Only orange tasks can be edited
  - No delete operations exist
  - No way to set cards green (human only)
  - Circular dependencies are rejected
  - Blocked states auto-managed on every write

See 'Canvas Workflow Rules.md' for the full protocol.
"""

import json
import sys
import os
import re
import math
import argparse
import uuid
import shutil
from collections import defaultdict

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COLOR_MAP = {
    "0": "gray",
    "1": "red",
    "2": "orange",
    "3": "yellow",
    "4": "green",
    "5": "cyan",
    "6": "purple",
}

STATE_TO_COLOR = {v: k for k, v in COLOR_MAP.items()}

TASK_ID_RE = re.compile(r"^##\s+([A-Z]{1,3})-(\d{2})\s+(.*)$", re.MULTILINE)

NON_TASK_IDS = {"canvas-errors", "canvas-warnings", "legend"}

# ---------------------------------------------------------------------------
# Canvas I/O
# ---------------------------------------------------------------------------


def load_canvas(path):
    """Load and return the canvas dict from a JSON file."""
    if not os.path.isfile(path):
        error(f"File not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_canvas(path, canvas):
    """Save the canvas dict back to disk with tab indentation."""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(canvas, f, indent="\t", ensure_ascii=False)
        f.write("\n")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def error(msg):
    """Print error to stderr and exit 1."""
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg):
    """Print warning to stderr."""
    print(f"Warning: {msg}", file=sys.stderr)


def color_name(node):
    """Return the human-readable state name for a node."""
    c = node.get("color", "0")
    return COLOR_MAP.get(c, f"unknown({c})")


def parse_task_id(node):
    """Extract (prefix, number, title) from the node text, or None."""
    text = node.get("text", "")
    m = TASK_ID_RE.search(text)
    if m:
        return m.group(1), int(m.group(2)), m.group(3).strip()
    return None


def task_id_str(node):
    """Return the 'XX-NN' task ID string, or None."""
    parsed = parse_task_id(node)
    if parsed:
        prefix, num, _ = parsed
        return f"{prefix}-{num:02d}"
    return None


def is_task(node):
    """Determine if a node is a task card (not a managed non-task)."""
    if node.get("type") != "text":
        return False
    if node.get("id") in NON_TASK_IDS:
        return False
    text = node.get("text", "")
    # Has a proper task ID pattern
    if TASK_ID_RE.search(text):
        return True
    # Task-like: starts with ## but is not a managed card
    if text.startswith("## "):
        c = node.get("color", "")
        # Non-task cards typically have color 0 or no color
        if c == "0" or c == "":
            return False
        return True
    return False


def get_tasks(canvas):
    """Return list of task nodes."""
    return [n for n in canvas.get("nodes", []) if is_task(n)]


def get_groups(canvas):
    """Return list of group nodes."""
    return [n for n in canvas.get("nodes", []) if n.get("type") == "group"]


def find_task(canvas, task_id):
    """Find a task node by its XX-NN ID (case-insensitive). Returns node or None."""
    task_id_upper = task_id.upper()
    for n in get_tasks(canvas):
        tid = task_id_str(n)
        if tid and tid.upper() == task_id_upper:
            return n
    return None


def find_node_by_id(canvas, node_id):
    """Find any node by its internal node id."""
    for n in canvas.get("nodes", []):
        if n.get("id") == node_id:
            return n
    return None


def get_group_for_node(canvas, node):
    """Find which group geometrically contains a node. Picks the smallest containing group."""
    nx, ny = node.get("x", 0), node.get("y", 0)
    nw, nh = node.get("width", 0), node.get("height", 0)
    ncx, ncy = nx + nw / 2, ny + nh / 2

    best = None
    best_area = float("inf")
    for g in get_groups(canvas):
        gx, gy = g.get("x", 0), g.get("y", 0)
        gw, gh = g.get("width", 0), g.get("height", 0)
        if gx <= ncx <= gx + gw and gy <= ncy <= gy + gh:
            area = gw * gh
            if area < best_area:
                best = g
                best_area = area
    return best


def get_dependencies(canvas, node):
    """Return list of nodes that block this node (fromNode → toNode means fromNode blocks toNode)."""
    node_id = node.get("id")
    blockers = []
    for e in canvas.get("edges", []):
        if e.get("toNode") == node_id:
            blocker = find_node_by_id(canvas, e.get("fromNode"))
            if blocker:
                blockers.append(blocker)
    return blockers


def get_dependents(canvas, node):
    """Return list of nodes that this node blocks."""
    node_id = node.get("id")
    blocked = []
    for e in canvas.get("edges", []):
        if e.get("fromNode") == node_id:
            dep = find_node_by_id(canvas, e.get("toNode"))
            if dep:
                blocked.append(dep)
    return blocked


def all_deps_green(canvas, node):
    """Check if all dependencies of a node are green (color 4)."""
    deps = get_dependencies(canvas, node)
    return all(d.get("color") == "4" for d in deps)


def task_title(node):
    """Return task title line (XX-NN Title) or first line."""
    parsed = parse_task_id(node)
    if parsed:
        prefix, num, title = parsed
        return f"{prefix}-{num:02d} {title}"
    text = node.get("text", "").split("\n")[0]
    return text.lstrip("# ").strip()


def task_description(node):
    """Return the description body (everything after the ## heading line)."""
    text = node.get("text", "")
    lines = text.split("\n")
    if lines and lines[0].startswith("## "):
        return "\n".join(lines[1:]).strip()
    return text


def build_adj(canvas):
    """Build adjacency list: adj[from_id] = [to_id, ...]."""
    adj = defaultdict(list)
    for e in canvas.get("edges", []):
        adj[e.get("fromNode")].append(e.get("toNode"))
    return adj


def compute_depths(canvas):
    """Compute global longest-path depth for every task node.

    depth(root) = 0; depth(t) = max(depth(parent) + 1 for each parent).
    Only edges between task nodes are considered.
    Returns a dict {node_id: depth}.
    """
    tasks = get_tasks(canvas)
    task_ids = {t["id"] for t in tasks}
    parents = defaultdict(list)
    for e in canvas.get("edges", []):
        fn, tn = e.get("fromNode"), e.get("toNode")
        if fn in task_ids and tn in task_ids:
            parents[tn].append(fn)

    depths = {}

    def _depth(nid):
        if nid in depths:
            return depths[nid]
        depths[nid] = 0  # mark in-progress (cycle guard)
        if parents[nid]:
            depths[nid] = max(_depth(p) for p in parents[nid]) + 1
        return depths[nid]

    for t in tasks:
        _depth(t["id"])
    return depths


def has_cycle_with_edge(canvas, from_id, to_id):
    """Check if adding edge from_id -> to_id would create a cycle.

    Only checks whether to_id can already reach from_id through existing
    edges.  If it can, adding from_id -> to_id would close a loop.
    """
    if from_id == to_id:
        return True
    adj = build_adj(canvas)
    visited = set()
    stack = [to_id]
    while stack:
        nid = stack.pop()
        if nid == from_id:
            return True
        if nid in visited:
            continue
        visited.add(nid)
        stack.extend(adj.get(nid, []))
    return False


def next_edge_id(canvas):
    """Generate next sequential edge ID like edge-001, edge-002, etc."""
    max_num = 0
    for e in canvas.get("edges", []):
        eid = e.get("id", "")
        m = re.match(r"edge-(\d+)", eid)
        if m:
            max_num = max(max_num, int(m.group(1)))
    return f"edge-{max_num + 1:03d}"


def pick_sides(from_node, to_node):
    """Pick fromSide/toSide for a dependency arrow (from=blocker, to=blocked).

    Preference: blocker exits from its bottom/right, blocked receives at top/left.
    We choose the axis with the larger gap so arrows don't cross card bodies.
    """
    fx = from_node.get("x", 0)
    fy = from_node.get("y", 0)
    fw = from_node.get("width", 280)
    fh = from_node.get("height", 160)
    tx = to_node.get("x", 0)
    ty = to_node.get("y", 0)
    tw = to_node.get("width", 280)
    th = to_node.get("height", 160)

    fcx, fcy = fx + fw / 2, fy + fh / 2
    tcx, tcy = tx + tw / 2, ty + th / 2

    dx = tcx - fcx
    dy = tcy - fcy

    # Vertical gap: space between bottom of from and top of to (positive = no overlap)
    v_gap = ty - (fy + fh)
    # Horizontal gap
    h_gap = tx - (fx + fw) if dx > 0 else fx - (tx + tw)

    # Prefer vertical (bottom→top) when target is clearly below
    if dy > 0 and (v_gap >= 0 or abs(dy) >= abs(dx)):
        return "bottom", "top"
    if dy < 0 and (v_gap >= 0 or abs(dy) >= abs(dx)):
        return "top", "bottom"
    if dx > 0:
        return "right", "left"
    return "left", "right"


# ---------------------------------------------------------------------------
# Placement helpers
# ---------------------------------------------------------------------------


def _cards_overlap(x1, y1, w1, h1, x2, y2, w2, h2, margin=10):
    """Check if two rectangles overlap (with margin)."""
    return not (
        x1 + w1 + margin <= x2
        or x2 + w2 + margin <= x1
        or y1 + h1 + margin <= y2
        or y2 + h2 + margin <= y1
    )


def _occupied_rects(canvas, group):
    """Return list of (x, y, w, h) for all task cards inside a group."""
    rects = []
    for n in canvas.get("nodes", []):
        if n.get("type") != "text":
            continue
        if n.get("id") in NON_TASK_IDS:
            continue
        if get_group_for_node(canvas, n) == group:
            rects.append((n.get("x", 0), n.get("y", 0), n.get("width", 280), n.get("height", 160)))
    return rects


def _avoid_collisions(x, y, w, h, occupied, group, margin=20):
    """Nudge (x, y) downward until it doesn't collide with occupied rects.
    Stays within the group's x-bounds."""
    gx = group.get("x", 0)
    gw = group.get("width", 380)
    max_attempts = 50
    for _ in range(max_attempts):
        if not any(_cards_overlap(x, y, w, h, *r) for r in occupied):
            return x, y
        y += h + margin
    return x, y


def _group_rects(canvas):
    """Return list of (x, y, w, h) for all group nodes."""
    return [
        (g.get("x", 0), g.get("y", 0), g.get("width", 380), g.get("height", 700))
        for g in get_groups(canvas)
    ]


def compute_group_placement(canvas, width=380, height=700):
    """Compute (x, y) for a new group, avoiding overlap with existing groups.

    Strategy:
    1. Try placing to the right of all existing groups (aligned to their median y).
    2. If that would collide (shouldn't normally), nudge right.
    3. For an empty canvas, start at (0, 0).
    """
    groups = get_groups(canvas)
    gap = 60

    if not groups:
        return 0, 0

    # Align y to the median y of existing groups for a tidy row
    ys = sorted(g.get("y", 0) for g in groups)
    median_y = ys[len(ys) // 2]

    # Start to the right of the rightmost group
    max_right = max(g.get("x", 0) + g.get("width", 380) for g in groups)
    new_x = max_right + gap
    new_y = median_y

    # Collision check — nudge right if overlapping (e.g. groups arranged vertically)
    existing = _group_rects(canvas)
    max_attempts = 20
    for _ in range(max_attempts):
        if not any(_cards_overlap(new_x, new_y, width, height, *r, margin=gap) for r in existing):
            break
        new_x += width + gap

    return int(new_x), int(new_y)


def _card_size(text):
    """Estimate card (width, height) from markdown card text.

    Card text format:
        ## ID Title
        [blank line]
        Description body, possibly multi-line.

    Rendering assumptions (Obsidian Canvas, default zoom):
        - ~8px per character (proportional font approximation)
        - H2 title: ~22px line height
        - Body text: ~18px line height
        - Horizontal padding: 16px each side (32px total)
        - Vertical padding: 40px total (top+bottom)
        - Gap between title and body: 12px
    """
    CHAR_W   = 8
    H_PAD    = 32   # left + right padding
    V_PAD    = 40   # top + bottom padding
    TITLE_LH = 22   # H2 line height
    BODY_LH  = 18   # body line height
    GAP      = 12   # space between title block and body

    MIN_W, MAX_W = 280, 520
    MIN_H        = 120

    lines = text.split('\n')
    title = ''
    body_paragraphs = []
    for line in lines:
        if line.startswith('## '):
            title = line[3:].strip()
        elif title:
            body_paragraphs.append(line)

    # Width driven by title length; body wraps inside
    card_w = max(MIN_W, min(MAX_W, len(title) * CHAR_W + H_PAD + 16))

    chars_per_line = max(1, (card_w - H_PAD) // CHAR_W)

    # Title lines
    title_lines = math.ceil(len(title) / chars_per_line) if title else 1
    title_h = title_lines * TITLE_LH

    # Body lines (paragraph-aware; blank lines count as 1)
    body_line_count = 0
    for para in body_paragraphs:
        if para.strip():
            body_line_count += math.ceil(len(para) / chars_per_line)
        else:
            body_line_count += 1
    body_h = body_line_count * BODY_LH

    card_h = max(MIN_H, V_PAD + title_h + (GAP + body_h if body_h else 0))

    return int(card_w), int(card_h)


def compute_placement(canvas, group, depends_on_nodes=None, card_w=360, card_h=200):
    """Compute (x, y) for a new card inside a group.

    Rules:
    - If the card has dependencies in the same group, place it below them.
    - If deps are in other groups, place at the top of this group.
    - If no deps, place below existing cards.
    - Avoid overlapping existing cards.
    - Expand the group if the card doesn't fit.
    """
    gx, gy = group.get("x", 0), group.get("y", 0)
    gw, gh = group.get("width", 380), group.get("height", 700)
    margin = 20
    occupied = _occupied_rects(canvas, group)

    if depends_on_nodes:
        same_group = [d for d in depends_on_nodes if get_group_for_node(canvas, d) == group]
        if same_group:
            # Place below the lowest same-group dependency, aligned to their average x
            max_bottom = max(d.get("y", 0) + d.get("height", 160) for d in same_group)
            avg_x = sum(d.get("x", 0) + d.get("width", 280) / 2 for d in same_group) / len(same_group)
            new_x = avg_x - card_w / 2
            new_y = max_bottom + margin
        else:
            # Deps in other groups → place at top of this group
            new_x = gx + margin
            new_y = gy + 40
    else:
        if occupied:
            max_bottom = max(r[1] + r[3] for r in occupied)
            new_y = max_bottom + margin
            new_x = gx + margin
        else:
            new_x = gx + margin
            new_y = gy + 40

    # Clamp x within group bounds
    new_x = max(gx + margin, min(new_x, gx + gw - card_w - margin))

    # Avoid collisions
    new_x, new_y = _avoid_collisions(int(new_x), int(new_y), card_w, card_h, occupied, group, margin)

    # Expand group if card doesn't fit
    needed_bottom = new_y + card_h + margin
    if needed_bottom > gy + gh:
        group["height"] = int(needed_bottom - gy)

    # Expand width if card is wider than group allows
    needed_right = new_x + card_w + margin
    if needed_right > gx + gw:
        group["width"] = int(needed_right - gx)

    return int(new_x), int(new_y)


def group_prefix(canvas, group):
    """Determine the prefix used by tasks in a group. If none, derive from label."""
    tasks_in_group = [t for t in get_tasks(canvas) if get_group_for_node(canvas, t) == group]
    prefixes = set()
    for t in tasks_in_group:
        parsed = parse_task_id(t)
        if parsed:
            prefixes.add(parsed[0])
    if len(prefixes) == 1:
        return prefixes.pop()
    if len(prefixes) > 1:
        # Pick the most common
        counts = defaultdict(int)
        for t in tasks_in_group:
            parsed = parse_task_id(t)
            if parsed:
                counts[parsed[0]] += 1
        return max(counts, key=counts.get)

    # Derive from group label
    label = group.get("label", "X")
    words = label.split()
    # Try initials
    if len(words) >= 2:
        prefix = "".join(w[0].upper() for w in words[:3])
    else:
        prefix = label[:2].upper()

    # Make sure it's unambiguous (not used by another group)
    all_prefixes = set()
    for g in get_groups(canvas):
        if g.get("id") != group.get("id"):
            p = _existing_prefix(canvas, g)
            if p:
                all_prefixes.add(p)

    # If collision, extend
    if prefix in all_prefixes:
        for length in range(2, len(label) + 1):
            candidate = label[:length].upper().replace(" ", "")[:3]
            if candidate not in all_prefixes:
                prefix = candidate
                break

    return prefix[:3].upper()


def _existing_prefix(canvas, group):
    """Get existing prefix used in a group, or None."""
    for n in canvas.get("nodes", []):
        if not is_task(n):
            continue
        if get_group_for_node(canvas, n) == group:
            parsed = parse_task_id(n)
            if parsed:
                return parsed[0]
    return None


def next_task_number(canvas, prefix):
    """Find the next available number for a given prefix."""
    max_num = 0
    for t in get_tasks(canvas):
        parsed = parse_task_id(t)
        if parsed and parsed[0] == prefix:
            max_num = max(max_num, parsed[1])
    return max_num + 1


# ---------------------------------------------------------------------------
# Normalize
# ---------------------------------------------------------------------------


def normalize(canvas):
    """
    1. Assign display IDs to task-like cards without them.
    2. Update blocked states: red with unmet deps → gray, gray with all deps met → red.
    Returns list of change descriptions.
    """
    changes = []

    # 1. Assign IDs to task-like cards without proper IDs
    for n in canvas.get("nodes", []):
        if n.get("type") != "text":
            continue
        if n.get("id") in NON_TASK_IDS:
            continue
        text = n.get("text", "")
        if not text.startswith("## "):
            continue
        c = n.get("color", "")
        if c == "0" or c == "":
            continue
        # Check if already has a task ID
        if TASK_ID_RE.search(text):
            continue
        # This is a task-like card without an ID — assign one
        group = get_group_for_node(canvas, n)
        if group:
            prefix = group_prefix(canvas, group)
        else:
            prefix = "XX"
        num = next_task_number(canvas, prefix)
        # Extract title from ## line
        first_line = text.split("\n")[0]
        title = first_line.lstrip("# ").strip()
        rest = "\n".join(text.split("\n")[1:])
        new_heading = f"## {prefix}-{num:02d} {title}"
        n["text"] = new_heading + "\n" + rest if rest else new_heading
        changes.append(f"Assigned ID {prefix}-{num:02d} to '{title}'")

    # 2. Update blocked states
    for n in get_tasks(canvas):
        c = n.get("color", "1")
        deps = get_dependencies(canvas, n)
        tid = task_id_str(n) or n.get("id")

        if c == "1":  # red
            if deps and not all(d.get("color") == "4" for d in deps):
                n["color"] = "0"
                changes.append(f"{tid}: red → gray (blocked)")
        elif c == "0":  # gray
            if not deps or all(d.get("color") == "4" for d in deps):
                n["color"] = "1"
                changes.append(f"{tid}: gray → red (unblocked)")

    return changes


# ---------------------------------------------------------------------------
# Commands: Read-only
# ---------------------------------------------------------------------------


def cmd_status(canvas, _args):
    """Board overview."""
    groups = get_groups(canvas)
    tasks = get_tasks(canvas)

    # Groups with task counts
    print("=== Groups ===")
    for g in groups:
        label = g.get("label", "(unnamed)")
        count = sum(1 for t in tasks if get_group_for_node(canvas, t) == g)
        print(f"  {label}: {count} tasks")

    # Tasks by state
    states = defaultdict(list)
    for t in tasks:
        c = t.get("color", "1")
        sname = COLOR_MAP.get(c, "unknown")
        states[sname].append(t)

    print("\n=== Tasks by State ===")

    # Ready (red with deps met)
    ready = [t for t in states.get("red", []) if all_deps_green(canvas, t)]
    if ready:
        print(f"  Ready ({len(ready)}):")
        for t in ready:
            print(f"    {task_id_str(t) or t['id']}  {task_title(t)}")

    # Doing
    doing = states.get("orange", [])
    if doing:
        print(f"  Doing ({len(doing)}):")
        for t in doing:
            print(f"    {task_id_str(t) or t['id']}  {task_title(t)}")

    # Review
    review = states.get("cyan", [])
    if review:
        print(f"  Review ({len(review)}):")
        for t in review:
            print(f"    {task_id_str(t) or t['id']}  {task_title(t)}")

    # Blocked
    blocked = states.get("gray", [])
    if blocked:
        print(f"  Blocked ({len(blocked)}):")
        for t in blocked:
            tid = task_id_str(t) or t["id"]
            blockers = get_dependencies(canvas, t)
            non_green = [b for b in blockers if b.get("color") != "4"]
            blocker_strs = ", ".join(
                f"{task_id_str(b) or b['id']}({color_name(b)})" for b in non_green
            )
            print(f"    {tid}  {task_title(t)}  blocked by: {blocker_strs}")

    # Proposed
    proposed = states.get("purple", [])
    if proposed:
        print(f"  Proposed ({len(proposed)}):")
        for t in proposed:
            print(f"    {task_id_str(t) or t['id']}  {task_title(t)}")

    # Green / done
    done = states.get("green", [])
    if done:
        print(f"  Done ({len(done)}):")
        for t in done:
            print(f"    {task_id_str(t) or t['id']}  {task_title(t)}")

    # Anomalies
    anomalies = []
    for t in tasks:
        c = t.get("color", "1")
        if c == "1":  # red but has unmet deps
            deps = get_dependencies(canvas, t)
            if deps and not all(d.get("color") == "4" for d in deps):
                anomalies.append(f"{task_id_str(t) or t['id']}: red but has unmet dependencies (should be gray)")
        if c == "0":  # gray but all deps met
            deps = get_dependencies(canvas, t)
            if not deps or all(d.get("color") == "4" for d in deps):
                anomalies.append(f"{task_id_str(t) or t['id']}: gray but all dependencies met (should be red)")

    if anomalies:
        print("\n=== Anomalies ===")
        for a in anomalies:
            print(f"  {a}")


def cmd_show(canvas, args):
    """Show full detail for a task."""
    if not args.task_id:
        error("Usage: show <TASK-ID>")
    node = find_task(canvas, args.task_id)
    if not node:
        error(f"Task '{args.task_id}' not found")

    tid = task_id_str(node) or node.get("id")
    state = color_name(node)
    group = get_group_for_node(canvas, node)
    group_label = group.get("label", "(none)") if group else "(none)"

    print(f"Task:  {tid}")
    print(f"State: {state}")
    print(f"Group: {group_label}")
    print(f"\n--- Text ---")
    print(node.get("text", ""))
    print(f"--- End ---")

    deps = get_dependencies(canvas, node)
    if deps:
        print(f"\nDependencies:")
        for d in deps:
            dtid = task_id_str(d) or d.get("id")
            mark = "\u2713" if d.get("color") == "4" else " "
            print(f"  [{mark}] {dtid} ({color_name(d)}) {task_title(d)}")

    dependents = get_dependents(canvas, node)
    if dependents:
        print(f"\nDependents:")
        for d in dependents:
            dtid = task_id_str(d) or d.get("id")
            mark = "\u2713" if d.get("color") == "4" else " "
            print(f"  [{mark}] {dtid} ({color_name(d)}) {task_title(d)}")


def cmd_list(canvas, args):
    """List tasks, optionally filtered by state or group."""
    tasks = get_tasks(canvas)
    filter_val = args.filter if hasattr(args, "filter") and args.filter else None

    # Determine if filter is a state or group
    filter_by_state = None
    filter_by_group = None
    if filter_val:
        lower = filter_val.lower()
        if lower in STATE_TO_COLOR:
            filter_by_state = lower
        else:
            # Check group labels
            for g in get_groups(canvas):
                if g.get("label", "").lower() == lower:
                    filter_by_group = g
                    break
            if not filter_by_group:
                error(f"Unknown state or group: '{filter_val}'")

    # Group tasks by group
    groups = get_groups(canvas)
    grouped = defaultdict(list)
    ungrouped = []
    for t in tasks:
        g = get_group_for_node(canvas, t)
        if g:
            grouped[g.get("id")].append(t)
        else:
            ungrouped.append(t)

    def print_task(t):
        tid = task_id_str(t) or t.get("id")
        state = color_name(t)
        title = task_title(t)
        print(f"  {tid} [{state}] {title}")

    def should_include(t):
        if filter_by_state:
            return color_name(t) == filter_by_state
        return True

    for g in groups:
        if filter_by_group and g.get("id") != filter_by_group.get("id"):
            continue
        group_tasks = grouped.get(g.get("id"), [])
        visible = [t for t in group_tasks if should_include(t)]
        if not visible:
            continue
        print(f"[{g.get('label', '(unnamed)')}]")
        for t in visible:
            print_task(t)

    if not filter_by_group:
        visible_ungrouped = [t for t in ungrouped if should_include(t)]
        if visible_ungrouped:
            print("[Ungrouped]")
            for t in visible_ungrouped:
                print_task(t)


def cmd_blocked(canvas, _args):
    """Show all gray/blocked tasks and what blocks them."""
    tasks = get_tasks(canvas)
    blocked = [t for t in tasks if t.get("color") == "0"]
    if not blocked:
        print("No blocked tasks.")
        return
    for t in blocked:
        tid = task_id_str(t) or t.get("id")
        deps = get_dependencies(canvas, t)
        non_green = [d for d in deps if d.get("color") != "4"]
        print(f"{tid}  {task_title(t)}")
        for b in non_green:
            btid = task_id_str(b) or b.get("id")
            print(f"  blocked by: {btid} [{color_name(b)}] {task_title(b)}")
        if not non_green and not deps:
            print("  (no dependencies — anomaly: should not be gray)")


def cmd_blocking(canvas, _args):
    """Show all non-green tasks that have dependents."""
    tasks = get_tasks(canvas)
    found = False
    for t in tasks:
        if t.get("color") == "4":
            continue
        dependents = get_dependents(canvas, t)
        if not dependents:
            continue
        found = True
        tid = task_id_str(t) or t.get("id")
        print(f"{tid} [{color_name(t)}] {task_title(t)}")
        for d in dependents:
            dtid = task_id_str(d) or d.get("id")
            print(f"  blocks: {dtid} [{color_name(d)}] {task_title(d)}")
    if not found:
        print("No blocking tasks.")


def cmd_ready(canvas, _args):
    """Show all red tasks whose dependencies are all green (or no dependencies)."""
    tasks = get_tasks(canvas)
    ready = [t for t in tasks if t.get("color") == "1" and all_deps_green(canvas, t)]
    if not ready:
        print("No ready tasks.")
        return
    for t in ready:
        tid = task_id_str(t) or t.get("id")
        print(f"{tid}  {task_title(t)}")


def cmd_dump(canvas, _args):
    """Print raw canvas JSON."""
    sys.stdout.buffer.write(
        json.dumps(canvas, indent="\t", ensure_ascii=False).encode("utf-8")
    )
    sys.stdout.buffer.write(b"\n")


# ---------------------------------------------------------------------------
# Commands: Lifecycle (write)
# ---------------------------------------------------------------------------


def cmd_start(canvas, args, path):
    """Start a task: red → orange."""
    if not args.task_id:
        error("Usage: start <TASK-ID>")
    node = find_task(canvas, args.task_id)
    if not node:
        error(f"Task '{args.task_id}' not found")
    if node.get("color") != "1":
        error(f"Task {args.task_id} is {color_name(node)}, must be red to start")
    if not all_deps_green(canvas, node):
        deps = get_dependencies(canvas, node)
        non_green = [d for d in deps if d.get("color") != "4"]
        blocker_strs = ", ".join(f"{task_id_str(d) or d['id']}({color_name(d)})" for d in non_green)
        error(f"Task {args.task_id} has unmet dependencies: {blocker_strs}")

    node["color"] = "2"
    changes = normalize(canvas)
    save_canvas(path, canvas)
    tid = task_id_str(node) or node.get("id")
    print(f"Started {tid} (red → orange)")
    for c in changes:
        print(f"  normalize: {c}")


def cmd_finish(canvas, args, path):
    """Finish a task: orange → cyan."""
    if not args.task_id:
        error("Usage: finish <TASK-ID>")
    node = find_task(canvas, args.task_id)
    if not node:
        error(f"Task '{args.task_id}' not found")
    if node.get("color") != "2":
        error(f"Task {args.task_id} is {color_name(node)}, must be orange to finish")

    node["color"] = "5"
    save_canvas(path, canvas)
    tid = task_id_str(node) or node.get("id")
    print(f"Finished {tid} (orange → cyan)")


def cmd_pause(canvas, args, path):
    """Pause a task: orange → red."""
    if not args.task_id:
        error("Usage: pause <TASK-ID>")
    node = find_task(canvas, args.task_id)
    if not node:
        error(f"Task '{args.task_id}' not found")
    if node.get("color") != "2":
        error(f"Task {args.task_id} is {color_name(node)}, must be orange to pause")

    node["color"] = "1"
    save_canvas(path, canvas)
    tid = task_id_str(node) or node.get("id")
    print(f"Paused {tid} (orange → red)")


# ---------------------------------------------------------------------------
# Commands: Propose (write)
# ---------------------------------------------------------------------------


def _create_proposed_task(canvas, group, title, desc, depends_on_ids=None):
    """Create a purple proposed task in a group. Returns (new_node, task_id, edges_added).

    This is the shared logic used by both `propose` and `batch`.
    """
    prefix = group_prefix(canvas, group)
    num = next_task_number(canvas, prefix)
    task_id = f"{prefix}-{num:02d}"

    # Resolve dependency nodes
    dep_nodes = []
    if depends_on_ids:
        for did in depends_on_ids:
            dnode = find_task(canvas, did)
            if not dnode:
                error(f"Dependency task '{did}' not found")
            dep_nodes.append(dnode)

    node_id = uuid.uuid4().hex[:8]
    text = f"## {task_id} {title}\n{desc}"
    card_w, card_h = _card_size(text)
    new_x, new_y = compute_placement(canvas, group, dep_nodes or None, card_w, card_h)

    new_node = {
        "id": node_id,
        "type": "text",
        "text": text,
        "x": new_x,
        "y": new_y,
        "width": card_w,
        "height": card_h,
        "color": "6",
    }
    canvas.setdefault("nodes", []).append(new_node)

    # Add dependency edges
    edges_added = []
    for dnode in dep_nodes:
        from_nid = dnode.get("id")
        to_nid = node_id

        # Check for cycles
        if has_cycle_with_edge(canvas, from_nid, to_nid):
            warn(f"Skipping dep {task_id_str(dnode)} → {task_id}: would create cycle")
            continue

        edge_id = next_edge_id(canvas)
        from_side, to_side = pick_sides(dnode, new_node)
        new_edge = {
            "id": edge_id,
            "fromNode": from_nid,
            "fromSide": from_side,
            "toNode": to_nid,
            "toSide": to_side,
        }
        canvas.setdefault("edges", []).append(new_edge)
        edges_added.append((task_id_str(dnode) or from_nid, edge_id))

    return new_node, task_id, edges_added


def cmd_propose(canvas, args, path):
    """Add a purple proposed task to a group."""
    group_label = args.group
    title = args.title
    desc = args.desc
    depends_on = args.depends_on or []

    # Find group by label (case-insensitive)
    group = None
    for g in get_groups(canvas):
        if g.get("label", "").lower() == group_label.lower():
            group = g
            break
    if not group:
        error(f"Group '{group_label}' not found")

    _node, task_id, edges = _create_proposed_task(canvas, group, title, desc, depends_on)
    normalize(canvas)
    save_canvas(path, canvas)

    parts = [f"Proposed {task_id} '{title}' in {group.get('label')} (purple)"]
    if edges:
        dep_strs = ", ".join(f"{did}" for did, _ in edges)
        parts.append(f"  depends on: {dep_strs}")
    print("\n".join(parts))


def cmd_batch(canvas, args, path):
    """Batch-add groups, tasks, and dependencies from JSON on stdin.

    Expected JSON format:
    {
      "groups": ["Testing", "QA"],
      "tasks": [
        {"group": "Development", "title": "Write tests", "desc": "...", "depends_on": ["DV-01"]},
        {"group": "Development", "title": "Fix bugs", "desc": "...", "depends_on": ["DV-02"]}
      ]
    }

    Tasks are processed in order. A task can depend on tasks defined earlier
    in the same batch by using the title as a reference (matched case-insensitively)
    in addition to existing task IDs on the board.
    """
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        error(f"Invalid JSON on stdin: {e}")

    results = []

    # 1. Create groups first (each placement accounts for previously created groups)
    for glabel in data.get("groups", []):
        new_group = _create_group(canvas, glabel)
        results.append(f"+ group '{glabel}' at ({new_group['x']}, {new_group['y']})")

    # 2. Create tasks — track title→task_id for intra-batch references
    batch_title_map = {}  # lowercase title → task_id

    for tdef in data.get("tasks", []):
        group_label = tdef.get("group", "")
        title = tdef.get("title", "")
        desc = tdef.get("desc", "")
        raw_deps = tdef.get("depends_on", [])

        if not group_label or not title:
            warn(f"Skipping task with missing group or title: {tdef}")
            continue

        # Find group
        group = None
        for g in get_groups(canvas):
            if g.get("label", "").lower() == group_label.lower():
                group = g
                break
        if not group:
            warn(f"Group '{group_label}' not found, skipping task '{title}'")
            continue

        # Resolve depends_on: try task ID first, then batch title map
        resolved_deps = []
        for dep_ref in raw_deps:
            # Try as task ID (e.g. "DV-01")
            if find_task(canvas, dep_ref):
                resolved_deps.append(dep_ref)
            # Try as batch title
            elif dep_ref.lower() in batch_title_map:
                resolved_deps.append(batch_title_map[dep_ref.lower()])
            else:
                warn(f"Dependency '{dep_ref}' not found for task '{title}', skipping this dep")

        _node, task_id, edges = _create_proposed_task(canvas, group, title, desc, resolved_deps)
        batch_title_map[title.lower()] = task_id

        dep_str = f" (depends on: {', '.join(resolved_deps)})" if resolved_deps else ""
        results.append(f"+ {task_id} '{title}' in {group_label}{dep_str}")

    # 3. Normalize blocked states
    changes = normalize(canvas)
    save_canvas(path, canvas)

    print(f"Batch complete: {len(results)} items")
    for r in results:
        print(f"  {r}")
    for c in changes:
        print(f"  normalize: {c}")


def _create_group(canvas, label, width=380, height=700):
    """Create a new group node and append it to the canvas. Returns the new group dict."""
    new_x, new_y = compute_group_placement(canvas, width, height)

    short = re.sub(r"[^a-z]", "", label.lower())[:6]
    group_id = f"group-{short}"
    existing_ids = {n.get("id") for n in canvas.get("nodes", [])}
    while group_id in existing_ids:
        group_id = f"group-{short}{uuid.uuid4().hex[:3]}"

    new_group = {
        "id": group_id,
        "type": "group",
        "x": new_x,
        "y": new_y,
        "width": width,
        "height": height,
        "label": label,
    }
    canvas.setdefault("nodes", []).append(new_group)
    return new_group


def cmd_propose_group(canvas, args, path):
    """Create a new empty group."""
    label = args.label
    new_group = _create_group(canvas, label)
    save_canvas(path, canvas)
    print(f"Created group '{label}' (id: {new_group['id']}) at x={new_group['x']}, y={new_group['y']}")


# ---------------------------------------------------------------------------
# Commands: Edit (write)
# ---------------------------------------------------------------------------


def cmd_edit(canvas, args, path):
    """Edit a task's description body."""
    if not args.task_id:
        error("Usage: edit <TASK-ID> \"<NEW-TEXT>\"")
    node = find_task(canvas, args.task_id)
    if not node:
        error(f"Task '{args.task_id}' not found")
    if node.get("color") != "2":
        error(f"Task {args.task_id} is {color_name(node)}, must be orange to edit")

    # Resolve new text from positional arg, --stdin, or --file
    sources = [args.new_text, args.stdin, args.file]
    provided = sum(1 for s in sources if s)
    if provided == 0:
        error("Provide new text as a positional argument, --stdin, or --file <path>")
    if provided > 1:
        error("Use only one of: positional text, --stdin, --file")

    if args.stdin:
        import io as _io
        new_text = sys.stdin.read()
    elif args.file:
        try:
            with open(args.file, "r", encoding="utf-8") as fh:
                new_text = fh.read()
        except OSError as exc:
            error(f"Cannot read file '{args.file}': {exc}")
    else:
        new_text = args.new_text

    new_text = new_text.rstrip("\n\r")

    text = node.get("text", "")
    lines = text.split("\n")
    if lines and lines[0].startswith("## "):
        heading = lines[0]
    else:
        heading = lines[0] if lines else ""

    if args.append:
        # Preserve existing body and append
        body = "\n".join(lines[1:]) if len(lines) > 1 else ""
        body = body.rstrip()
        if body:
            node["text"] = f"{heading}\n{body}\n\n{new_text}"
        else:
            node["text"] = f"{heading}\n{new_text}"
    else:
        node["text"] = f"{heading}\n{new_text}"

    save_canvas(path, canvas)
    tid = task_id_str(node) or node.get("id")
    mode = "Appended to" if args.append else "Updated"
    print(f"{mode} description for {tid}")


def cmd_add_dep(canvas, args, path):
    """Add a dependency edge: from_id blocks to_id."""
    from_id = args.from_id
    to_id = args.to_id

    from_node = find_task(canvas, from_id)
    if not from_node:
        error(f"Task '{from_id}' not found")
    to_node = find_task(canvas, to_id)
    if not to_node:
        error(f"Task '{to_id}' not found")

    from_nid = from_node.get("id")
    to_nid = to_node.get("id")

    # Check for existing edge
    for e in canvas.get("edges", []):
        if e.get("fromNode") == from_nid and e.get("toNode") == to_nid:
            error(f"Dependency {from_id} → {to_id} already exists")

    # Check for cycles
    if has_cycle_with_edge(canvas, from_nid, to_nid):
        error(f"Adding {from_id} → {to_id} would create a circular dependency")

    edge_id = next_edge_id(canvas)
    from_side, to_side = pick_sides(from_node, to_node)

    new_edge = {
        "id": edge_id,
        "fromNode": from_nid,
        "fromSide": from_side,
        "toNode": to_nid,
        "toSide": to_side,
    }

    canvas.setdefault("edges", []).append(new_edge)
    changes = normalize(canvas)
    save_canvas(path, canvas)
    print(f"Added dependency: {from_id} → {to_id} (edge {edge_id})")
    for c in changes:
        print(f"  normalize: {c}")


# ---------------------------------------------------------------------------
# Commands: Maintenance (write)
# ---------------------------------------------------------------------------


def cmd_layout(canvas, _args, path):
    """Reposition all tasks using DAG depth for top-to-bottom dependency flow.

    Within each group tasks are compacted into rows by depth. Same-depth
    tasks are arranged in columns (max MAX_COLS per row, wrapping to the
    next sub-row). Groups are repositioned after resize to prevent overlap.
    Edge attachment sides are recomputed after nodes move.
    """
    tasks = get_tasks(canvas)
    if not tasks:
        print("No tasks to lay out.")
        return

    depths = compute_depths(canvas)
    groups = get_groups(canvas)
    node_by_id = {n["id"]: n for n in canvas.get("nodes", [])}

    CARD_W    = 360   # fallback card width (used when text is unavailable)
    CARD_H    = 200   # fallback card height
    ROW_GAP   = 80    # vertical gap between rows
    COL_GAP   = 60    # horizontal gap between cards in a row
    TOP_PAD   = 140   # space reserved for the group label
    LEFT_PAD  = 120   # left inner margin
    RIGHT_PAD = 120   # right inner margin
    BOT_PAD   = 120   # bottom inner margin
    MAX_COLS  = 3     # max cards per row before wrapping
    GROUP_GAP = 150   # gap between groups after repositioning

    # Assign each task to a group. Tasks geometrically outside all groups
    # (strays from a prior layout run) fall back to the nearest group by
    # center distance so they are always recaptured.
    def _assign_group(t):
        g = get_group_for_node(canvas, t)
        if g is not None:
            return g
        tx = t.get("x", 0) + t.get("width", 280) / 2
        ty = t.get("y", 0) + t.get("height", 160) / 2
        return min(groups, key=lambda g: (
            (g.get("x", 0) + g.get("width", 380) / 2 - tx) ** 2 +
            (g.get("y", 0) + g.get("height", 700) / 2 - ty) ** 2
        ), default=None)

    # Phase 1: assign all tasks to groups BEFORE repositioning anything.
    # This must happen in one pass so that tasks moved by an earlier group's
    # layout don't get re-claimed by a later group whose bounds they now overlap.
    nodes_by_group_id = {}
    for group in groups:
        nodes_by_group_id[group["id"]] = [t for t in tasks if _assign_group(t) == group]

    moved = 0

    # Phase 2: reposition tasks within each group
    for group in groups:
        group_tasks = nodes_by_group_id.get(group["id"], [])
        if not group_tasks:
            continue

        gx, gy = int(group.get("x", 0)), int(group.get("y", 0))

        # Bucket by depth; sort each bucket by current x then task ID (stable)
        by_depth = defaultdict(list)
        for t in group_tasks:
            by_depth[depths[t["id"]]].append(t)
        for layer in by_depth.values():
            layer.sort(key=lambda t: (t.get("x", 0), task_id_str(t) or t["id"]))

        cur_y = gy + TOP_PAD
        max_right = gx + LEFT_PAD

        for d in sorted(by_depth.keys()):
            layer = by_depth[d]
            for chunk in [layer[i:i + MAX_COLS] for i in range(0, len(layer), MAX_COLS)]:
                # Size each card dynamically from its text content
                for t in chunk:
                    w, h = _card_size(t.get("text", ""))
                    t["width"] = w
                    t["height"] = h
                row_h = max(int(t.get("height", CARD_H)) for t in chunk)
                col_x = gx + LEFT_PAD
                for t in chunk:
                    t["x"] = col_x
                    t["y"] = cur_y
                    w = int(t.get("width", CARD_W))
                    max_right = max(max_right, col_x + w)
                    col_x += w + COL_GAP
                    moved += 1
                cur_y += row_h + ROW_GAP

        group["width"]  = max_right - gx + RIGHT_PAD
        group["height"] = cur_y - gy + BOT_PAD

    # Reposition groups to eliminate overlap using collision detection.
    # Sort by original x so we preserve the intended left-to-right order.
    placed = []
    for group in sorted(groups, key=lambda g: g.get("x", 0)):
        if not nodes_by_group_id.get(group["id"]):
            continue
        gx = int(group.get("x", 0))
        gy = int(group.get("y", 0))
        gw = int(group.get("width", 400))
        gh = int(group.get("height", 200))
        new_x = gx
        for prev in placed:
            px, py = int(prev.get("x", 0)), int(prev.get("y", 0))
            pw, ph = int(prev.get("width", 400)), int(prev.get("height", 200))
            if _cards_overlap(new_x, gy, gw, gh, px, py, pw, ph, margin=GROUP_GAP):
                new_x = max(new_x, px + pw + GROUP_GAP)
        dx = new_x - gx
        if dx:
            group["x"] = new_x
            for t in nodes_by_group_id[group["id"]]:
                t["x"] = int(t.get("x", 0)) + dx
        placed.append(group)

    # Recompute edge attachment sides now that nodes have moved
    for e in canvas.get("edges", []):
        fn = node_by_id.get(e.get("fromNode"))
        tn = node_by_id.get(e.get("toNode"))
        if fn and tn:
            fs, ts = pick_sides(fn, tn)
            e["fromSide"] = fs
            e["toSide"] = ts

    save_canvas(path, canvas)
    print(f"Layout applied: {moved} tasks repositioned across {len(groups)} groups.")


def cmd_normalize(canvas, _args, path):
    """Run normalization and save."""
    changes = normalize(canvas)
    save_canvas(path, canvas)
    if changes:
        print("Normalized:")
        for c in changes:
            print(f"  {c}")
    else:
        print("Nothing to normalize.")


# ---------------------------------------------------------------------------
# CLI Setup
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Init command — sets up Kanvas in a target project directory
# ---------------------------------------------------------------------------

def _install_skill(target, script_dir, skill_dir, installed):
    """Install the Kanvas skill into a single platform directory.

    Skips if the skill is already installed. Never overwrites.
    """
    kanvas_dir = os.path.join(target, skill_dir, "skills", "kanvas")
    if os.path.isdir(kanvas_dir):
        print(f"  ● {skill_dir}/skills/kanvas/ already exists — skipped")
        return

    scripts_dst = os.path.join(kanvas_dir, "scripts")
    os.makedirs(scripts_dst, exist_ok=True)

    # Copy SKILL.md
    src_skill = os.path.join(script_dir, "skill", "SKILL.md")
    if os.path.isfile(src_skill):
        shutil.copy2(src_skill, os.path.join(kanvas_dir, "SKILL.md"))

    # Copy canvas-tool.py into scripts/
    src_tool = os.path.join(script_dir, "canvas-tool.py")
    shutil.copy2(src_tool, os.path.join(scripts_dst, "canvas-tool.py"))

    installed.append(f"{skill_dir}/skills/kanvas/")


def cmd_init(target_dir, install_plugin=True):
    """Initialize Kanvas in a project directory.

    Installs Kanvas as an agent skill (additive, never overwrites) and
    optionally installs the Obsidian Canvas Watcher plugin.
    """
    target = os.path.abspath(target_dir)
    script_dir = os.path.dirname(os.path.abspath(__file__))

    if not os.path.isdir(target):
        error(f"Target directory does not exist: {target}")

    print(f"Initializing Kanvas in: {target}\n")
    installed = []

    # 1. Install skill into platform directories
    #    Always: .agents/ (open standard — Codex, Gemini, Cursor)
    #    Plus any platform-specific dirs that already exist
    platform_dirs = [".agents", ".claude"]
    for pdir in (".cursor", ".gemini"):
        if os.path.isdir(os.path.join(target, pdir)):
            platform_dirs.append(pdir)

    for pdir in platform_dirs:
        _install_skill(target, script_dir, pdir, installed)

    # 2. Copy blank canvas template if no .canvas file exists yet
    existing_canvas = [f for f in os.listdir(target) if f.endswith(".canvas")]
    if not existing_canvas:
        src_blank = os.path.join(script_dir, "examples", "blank.canvas")
        if os.path.isfile(src_blank):
            shutil.copy2(src_blank, os.path.join(target, "Project.canvas"))
            installed.append("Project.canvas (from blank template)")

    # 3. Install Canvas Watcher plugin if .obsidian/ exists and not skipped
    obsidian_dir = os.path.join(target, ".obsidian")
    if not install_plugin:
        print("  Skipping plugin install (--no-plugin).\n")
    elif os.path.isdir(obsidian_dir):
        plugin_src = os.path.join(script_dir, "canvas-watcher-plugin")
        plugin_dst = os.path.join(obsidian_dir, "plugins", "canvas-watcher")

        if os.path.isdir(plugin_dst):
            print("  ● Canvas Watcher plugin already installed — skipped")
        else:
            os.makedirs(plugin_dst, exist_ok=True)

            for fname in ("main.js", "manifest.json"):
                src = os.path.join(plugin_src, fname)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(plugin_dst, fname))

            # Register in community-plugins.json
            cp_path = os.path.join(obsidian_dir, "community-plugins.json")
            plugins = []
            if os.path.isfile(cp_path):
                with open(cp_path, "r", encoding="utf-8") as f:
                    try:
                        plugins = json.load(f)
                    except json.JSONDecodeError:
                        plugins = []
            if "canvas-watcher" not in plugins:
                plugins.append("canvas-watcher")
                with open(cp_path, "w", encoding="utf-8") as f:
                    json.dump(plugins, f, indent=2)

            installed.append("Canvas Watcher plugin")
    else:
        print("  Note: No .obsidian/ folder found — skipping plugin install.")
        print("  Open the folder in Obsidian first, then re-run init to install the plugin.\n")

    if installed:
        print("  Installed:")
        for item in installed:
            print(f"    ✓ {item}")
    else:
        print("  Everything already installed — nothing to do.")
    print("\nDone. Open the folder in Obsidian and start planning!")


def build_parser():
    """Build the argparse parser."""
    parser = argparse.ArgumentParser(
        description="Manage an Obsidian Canvas file with task workflow."
    )
    parser.add_argument("canvas_file", help="Path to the .canvas file")

    sub = parser.add_subparsers(dest="command", help="Command to run")

    # Read-only
    sub.add_parser("status", help="Board overview")

    p_show = sub.add_parser("show", help="Show task detail")
    p_show.add_argument("task_id", help="Task ID (e.g. RS-01)")

    p_list = sub.add_parser("list", help="List tasks")
    p_list.add_argument("filter", nargs="?", default=None, help="State name or group label")

    sub.add_parser("blocked", help="Show blocked tasks")
    sub.add_parser("blocking", help="Show blocking tasks")
    sub.add_parser("ready", help="Show ready tasks")
    sub.add_parser("dump", help="Dump raw JSON")

    # Lifecycle
    p_start = sub.add_parser("start", help="Start a task (red → orange)")
    p_start.add_argument("task_id", help="Task ID")

    p_finish = sub.add_parser("finish", help="Finish a task (orange → cyan)")
    p_finish.add_argument("task_id", help="Task ID")

    p_pause = sub.add_parser("pause", help="Pause a task (orange → red)")
    p_pause.add_argument("task_id", help="Task ID")

    # Propose
    p_propose = sub.add_parser("propose", help="Propose a new task")
    p_propose.add_argument("group", help="Group label")
    p_propose.add_argument("title", help="Task title")
    p_propose.add_argument("desc", help="Task description")
    p_propose.add_argument("--depends-on", nargs="*", default=[], metavar="ID",
                           help="Task IDs this depends on (e.g. --depends-on DV-01 DV-02)")

    p_propose_group = sub.add_parser("propose-group", help="Create a new group")
    p_propose_group.add_argument("label", help="Group label")

    sub.add_parser("batch", help="Batch-add groups/tasks from JSON on stdin")

    # Edit
    p_edit = sub.add_parser("edit", help="Edit task description")
    p_edit.add_argument("task_id", help="Task ID")
    p_edit.add_argument("new_text", nargs="?", default=None,
                        help="New description text (positional)")
    p_edit.add_argument("--stdin", action="store_true", default=False,
                        help="Read new text from stdin (avoids shell quoting issues)")
    p_edit.add_argument("--file", default=None, metavar="PATH",
                        help="Read new text from a file")
    p_edit.add_argument("--append", action="store_true", default=False,
                        help="Append text below existing description instead of replacing")

    p_dep = sub.add_parser("add-dep", help="Add dependency edge")
    p_dep.add_argument("from_id", help="Blocker task ID")
    p_dep.add_argument("to_id", help="Blocked task ID")

    # Maintenance
    sub.add_parser("layout", help="Reposition tasks by DAG depth (top-to-bottom flow)")
    sub.add_parser("normalize", help="Run normalization")

    return parser


def main():
    # Ensure stdout/stderr can handle Unicode on Windows
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

    # Handle 'init' before normal parsing (it doesn't need a canvas file)
    if len(sys.argv) >= 2 and sys.argv[1] == "init":
        init_args = sys.argv[2:]
        no_plugin = "--no-plugin" in init_args
        positional = [a for a in init_args if not a.startswith("--")]
        target = positional[0] if positional else "."
        cmd_init(target, install_plugin=not no_plugin)
        return

    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    canvas_path = args.canvas_file
    canvas = load_canvas(canvas_path)

    # Read-only commands
    read_only = {
        "status": cmd_status,
        "show": cmd_show,
        "list": cmd_list,
        "blocked": cmd_blocked,
        "blocking": cmd_blocking,
        "ready": cmd_ready,
        "dump": cmd_dump,
    }

    # Write commands (receive path as extra arg)
    write_cmds = {
        "start": cmd_start,
        "finish": cmd_finish,
        "pause": cmd_pause,
        "propose": cmd_propose,
        "propose-group": cmd_propose_group,
        "batch": cmd_batch,
        "edit": cmd_edit,
        "add-dep": cmd_add_dep,
        "layout": cmd_layout,
        "normalize": cmd_normalize,
    }

    if args.command in read_only:
        read_only[args.command](canvas, args)
    elif args.command in write_cmds:
        write_cmds[args.command](canvas, args, canvas_path)
    else:
        error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
