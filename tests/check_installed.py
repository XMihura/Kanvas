"""Check an installed `kanvas` command the way a user runs it.

    uv build && uv tool install dist/*.whl
    python tests/check_installed.py [COMMAND]

COMMAND defaults to `kanvas` on PATH. In a fresh temporary project the script
checks the version, the help, the error outside a board, `init`, commands run
from a subfolder, `export`, `serve`, the copy of kanvas.py that `init` installs
for agents, and the hint when that copy is out of date. It stops with exit
code 1 at the first failure.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kanvas import VERSION  # noqa: E402

TITLE = "Installed check é"
ENV = dict(os.environ, KANVAS_AGENT="check")
ENV.pop("KANVAS_BOARD", None)


class CheckFailed(Exception):
    pass


def ok(what):
    print(f"ok   {what}")


def run(cmd, cwd):
    proc = subprocess.run(cmd, cwd=cwd, env=ENV, capture_output=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise CheckFailed(f"{' '.join(cmd)} exited with {proc.returncode}\n{proc.stdout}{proc.stderr}")
    return proc.stdout


def fails(cmd, cwd):
    proc = subprocess.run(cmd, cwd=cwd, env=ENV, capture_output=True, encoding="utf-8", errors="replace")
    if proc.returncode == 0:
        raise CheckFailed(f"{' '.join(cmd)} should have failed\n{proc.stdout}{proc.stderr}")
    return proc.stderr


def expect(text, needle, what):
    if needle not in text:
        raise CheckFailed(f"{what}: expected {needle!r} in:\n{text}")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url):
    with urllib.request.urlopen(url, timeout=2) as response:
        return response.read().decode("utf-8")


def check_serve(kanvas, cwd, log_path):
    port = free_port()
    with open(log_path, "w", encoding="utf-8") as log:
        server = subprocess.Popen(kanvas + ["serve", "--no-open", "--port", str(port)],
                                  cwd=cwd, env=dict(ENV, PYTHONUNBUFFERED="1"), stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 20
        while True:
            try:
                board = json.loads(get(f"http://127.0.0.1:{port}/api/board"))
                break
            except OSError as e:
                if server.poll() is not None or time.time() > deadline:
                    state = "exited with " + str(server.returncode) if server.poll() is not None else "still running"
                    with open(log_path, encoding="utf-8", errors="replace") as f:
                        raise CheckFailed(f"serve did not answer on port {port} ({state}); last error: {e!r}\n"
                                          f"serve printed:\n{f.read()}")
                time.sleep(0.2)
        if TITLE not in [t["title"] for t in board["tasks"]]:
            raise CheckFailed(f"serve returned a different board: {board}")
        expect(get(f"http://127.0.0.1:{port}/"), "kanvas-snapshot", "the page from serve")
    finally:
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()


def check(kanvas, project):
    out = run(kanvas + ["--version"], project).strip()
    if out != f"Kanvas {VERSION}":
        raise CheckFailed(f"--version printed {out!r}, expected 'Kanvas {VERSION}' as in kanvas.py")
    ok(out)
    alias = shutil.which("kanvas-board", path=os.path.dirname(kanvas[0]))
    if alias:
        expect(run([alias, "--version"], project), VERSION, "kanvas-board --version")
        ok("kanvas-board --version")

    out = run(kanvas + ["--help"], project)
    if not out.startswith("usage: kanvas "):
        raise CheckFailed(f"--help should start with 'usage: kanvas ':\n{out}")
    ok("--help names the kanvas command")
    expect(fails(kanvas + ["status"], project), "`kanvas init`", "status outside a board")
    ok("status outside a board says to run kanvas init")

    out = run(kanvas + ["init"], project)
    expect(out, "created   kanvas.json", "init")
    for rel in ("kanvas.json", ".agents/skills/kanvas/kanvas.py", ".agents/skills/kanvas/kanvas.html",
                ".agents/skills/kanvas/SKILL.md", "AGENTS.md"):
        if not os.path.isfile(os.path.join(project, rel)):
            raise CheckFailed(f"init did not create {rel}")
    expect(out, "\n  kanvas serve\n", "the next step init prints")
    ok("init installs the board, the agent skill and AGENTS.md, and suggests kanvas serve")

    sub = os.path.join(project, "src", "deep")
    os.makedirs(sub)
    expect(run(kanvas + ["propose", "-", TITLE], sub), "G-01", "propose from a subfolder")
    expect(run(kanvas + ["status"], sub), TITLE, "status from a subfolder")
    expect(run(kanvas + ["check"], sub), "OK", "check from a subfolder")
    ok("propose, status and check from a subfolder")

    run(kanvas + ["export", "snapshot.html"], sub)
    with open(os.path.join(sub, "snapshot.html"), encoding="utf-8") as f:
        expect(f.read(), json.dumps(TITLE, ensure_ascii=False), "the exported snapshot")
    ok("export")

    check_serve(kanvas, sub, os.path.join(project, "serve.log"))
    ok("serve answers with the page and the board")

    copy = os.path.join(project, ".agents", "skills", "kanvas", "kanvas.py")
    expect(run([sys.executable, copy, "status"], project), TITLE, "the project copy of kanvas.py")
    ok("the project copy runs for agents")

    with open(copy, encoding="utf-8") as f:
        text, count = re.subn(r'^VERSION = "[^"]+"', 'VERSION = "0.0.1"', f.read(), count=1, flags=re.M)
    if not count:
        raise CheckFailed(f"{copy} has no VERSION line")
    with open(copy, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    hint = f"This project's agent copy is 0.0.1; run `kanvas init` to update it to {VERSION}."
    expect(run(kanvas + ["status"], project), hint, "status with an old agent copy")
    ok("status says when the agent copy is out of date")


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "kanvas"
    exe = shutil.which(name)
    if not exe:
        print(f"{name} is not on PATH; install it with `uv tool install dist/*.whl` first", file=sys.stderr)
        return 1
    print(f"Checking {exe}")
    project = tempfile.mkdtemp(prefix="kanvas-installed-")
    try:
        check([exe], project)
    except CheckFailed as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(project, ignore_errors=True)
    print("All checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
