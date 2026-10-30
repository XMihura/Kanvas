"""Smoke tests of kanvas.html in headless Chrome against `kanvas.py serve`. Run with the suite:

    python -m unittest discover -s tests -v

They look for Chrome or Chromium on PATH and in the usual install folders, and skip without one. KANVAS_TEST_CHROME
names the browser to use (a command or a path); when it is set, a missing browser fails the tests instead.
"""

import base64
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import kanvas  # noqa: E402

SCRIPT = os.path.join(ROOT, "kanvas.py")
CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


def chrome_paths():
    yield "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    yield "/Applications/Chromium.app/Contents/MacOS/Chromium"
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        if os.environ.get(var):
            yield os.path.join(os.environ[var], "Google", "Chrome", "Application", "chrome.exe")


def find_chrome(test):
    wanted = os.environ.get("KANVAS_TEST_CHROME")
    if wanted:
        found = shutil.which(wanted)
        if not found:
            test.fail(f"KANVAS_TEST_CHROME is {wanted!r}, which is not a program")
        return found
    found = next(filter(None, map(shutil.which, CHROME_NAMES)), None) or next(filter(os.path.isfile, chrome_paths()), None)
    if not found:
        test.skipTest(f"Chrome is not installed (looked for {', '.join(CHROME_NAMES)} and the usual install folders); "
                      "set KANVAS_TEST_CHROME to run the browser tests")
    return found


def read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def stop(proc):
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def described(details):
    return details.get("exception", {}).get("description") or details["text"]


def masked(data, key):
    n = len(data)
    return (int.from_bytes(data, "big") ^ int.from_bytes((key * (n // 4 + 1))[:n], "big")).to_bytes(n, "big")


class DevTools:
    """A connection to Chrome's DevTools protocol. Python's standard library has no WebSocket client, and a local
    Chrome needs only a small part of one: masked text frames out, whole or fragmented frames in."""

    def __init__(self, url, timeout=10):
        parts = urllib.parse.urlsplit(url)
        self.sock = socket.create_connection((parts.hostname, parts.port), timeout=timeout)
        self.buf = bytearray()
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        self.sock.sendall(f"GET {parts.path} HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode("ascii"))
        while b"\r\n\r\n" not in self.buf:
            self._recv()
        head, _, rest = bytes(self.buf).partition(b"\r\n\r\n")
        if head.split(b" ")[1:2] != [b"101"]:
            raise ConnectionError(f"Chrome refused the DevTools connection: {head.decode('latin-1')}")
        self.buf = bytearray(rest)
        self.last = 0
        self.handlers = {}

    def close(self):
        self.sock.close()

    def _recv(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("Chrome closed the DevTools connection")
        self.buf += chunk

    def _take(self, n):
        while len(self.buf) < n:
            self._recv()
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def _frame(self, op, payload):
        n = len(payload)
        size = bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 \
            else bytes([0x80 | 127]) + struct.pack(">Q", n)
        key = os.urandom(4)
        self.sock.sendall(bytes([0x80 | op]) + size + key + masked(payload, key))

    def _message(self):
        data = b""
        while True:
            b0, b1 = self._take(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._take(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._take(8))[0]
            key = self._take(4) if b1 & 0x80 else None
            payload = self._take(n)
            if key:
                payload = masked(payload, key)
            op = b0 & 0x0F
            if op == 8:
                raise ConnectionError("Chrome closed the DevTools connection")
            if op == 9:
                self._frame(10, payload)
            elif op in (0, 1, 2):
                data += payload
                if b0 & 0x80:
                    return json.loads(data.decode("utf-8"))

    def post(self, method, **params):
        """Sends a command without waiting for its answer, as event handlers must."""
        self.last += 1
        self._frame(1, json.dumps({"id": self.last, "method": method, "params": params}).encode("utf-8"))
        return self.last

    def send(self, method, **params):
        mine = self.post(method, **params)
        while True:
            msg = self._message()
            if msg.get("id") == mine:
                if "error" in msg:
                    raise RuntimeError(f"{method} failed: {msg['error'].get('message')}")
                return msg.get("result", {})
            if msg.get("method") in self.handlers:
                self.handlers[msg["method"]](msg.get("params", {}))

    def on(self, event, handler):
        self.handlers[event] = handler


class Page(DevTools):
    """A tab, with what it logged: console errors, uncaught exceptions and failed requests."""

    def __init__(self, url):
        super().__init__(url)
        self.errors = []
        self.on("Runtime.consoleAPICalled", self.console)
        self.on("Runtime.exceptionThrown", lambda p: self.errors.append(f"exception: {described(p['exceptionDetails'])}"))
        self.on("Log.entryAdded", self.log)
        self.send("Runtime.enable")
        self.send("Log.enable")

    def console(self, p):
        if p["type"] in ("error", "assert"):
            self.errors.append(f"console.{p['type']}: " + " ".join(str(a.get("value", a.get("description"))) for a in p["args"]))

    def log(self, p):
        e = p["entry"]
        if e["level"] == "error":
            self.errors.append(f"{e['source']}: {e['text']} {e.get('url', '')}".rstrip())

    def logged(self):
        """Everything logged so far: events come before the answer to a later command."""
        self.js("0")
        return self.errors

    def js(self, expression):
        r = self.send("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise AssertionError(f"{expression}\nthrew {described(r['exceptionDetails'])}")
        return r["result"].get("value")


NOTE_BOX = "document.querySelector('#panel .note-form textarea')"
ADD_NOTE = "[...document.querySelectorAll('#panel .note-form button')].find((b) => b.textContent.includes('Add note')).click()"


class BrowserSmokeTests(unittest.TestCase):
    """The page in headless Chrome: it loads cleanly, opens a task, saves notes, keeps a note whose save failed, and
    shows what agents change through the CLI."""

    def setUp(self):
        self.chrome = find_chrome(self)
        self.dir = tempfile.mkdtemp(prefix="kanvas-browser-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        os.mkdir(os.path.join(self.dir, "demo"))
        self.path = os.path.join(self.dir, "demo", "kanvas.json")
        shutil.copyfile(os.path.join(ROOT, "examples", "sample.json"), self.path)
        self.env = dict(os.environ, KANVAS_STATE_DIR=os.path.join(self.dir, "state"), PYTHONUNBUFFERED="1")
        self.env.pop("KANVAS_BOARD", None)
        self.url = self.start_server()
        self.page = self.start_chrome()

    def wait_for(self, what, check, timeout=10, details=lambda: ""):
        deadline = time.monotonic() + timeout
        while True:
            value = check()
            if value:
                return value
            if time.monotonic() > deadline:
                self.fail(f"Waited {timeout} s for {what}. {details()}")
            time.sleep(0.1)

    def until(self, what, expression, timeout=10):
        """Waits until `expression` is true on the page."""
        return self.wait_for(what, lambda: self.page.js(expression), timeout,
                             lambda: f"The page logged: {self.page.errors}. Its panel shows: "
                                     f"{self.page.js('document.querySelector(`#panel`).innerText')!r}")

    def start_server(self):
        log = os.path.join(self.dir, "serve.log")
        with open(log, "w", encoding="utf-8") as out:
            self.server = subprocess.Popen([sys.executable, SCRIPT, "--board", self.path, "serve", "--no-open", "--port", "0"],
                                           cwd=self.dir, env=self.env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        self.addCleanup(stop, self.server)

        def address():
            if self.server.poll() is not None:
                self.fail(f"serve exited with {self.server.returncode}:\n{read(log)}")
            return re.search(r"^Open (http://\S+)", read(log), re.M)
        return self.wait_for("serve to print its address", address, timeout=30, details=lambda: read(log)).group(1)

    def start_chrome(self):
        log, profile = os.path.join(self.dir, "chrome.log"), os.path.join(self.dir, "chrome")
        with open(log, "w", encoding="utf-8") as out:
            chrome = subprocess.Popen([self.chrome, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}",
                                       "--no-first-run", "--no-default-browser-check", "--no-sandbox",
                                       "--disable-background-networking", "--disable-component-update", "--disable-sync",
                                       "--password-store=basic", "--use-mock-keychain", "--window-size=1280,900", "about:blank"],
                                      stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        self.addCleanup(stop, chrome)

        # A browser confined to its own /tmp (Chromium as a snap) writes DevToolsActivePort where this test cannot
        # read it, and still prints the address.
        def devtools_port():
            if chrome.poll() is not None:
                self.fail(f"Chrome exited with {chrome.returncode}:\n{read(log)}")
            active = os.path.join(profile, "DevToolsActivePort")
            try:
                lines = read(active).split()
            except OSError:  # not written yet, or on Windows still open for writing
                lines = []
            if len(lines) == 2:
                return lines[0]
            printed = re.search(r"DevTools listening on ws://[^/]+:(\d+)/", read(log))
            return printed and printed.group(1)
        port = self.wait_for("Chrome's DevTools port", devtools_port, timeout=30, details=lambda: read(log))
        lists = f"http://127.0.0.1:{port}/json/list"

        def tab():
            with kanvas.LOCAL.open(lists, timeout=5) as r:
                return next((t["webSocketDebuggerUrl"] for t in json.loads(r.read()) if t["type"] == "page"), None)
        page = Page(self.wait_for("Chrome's first tab", tab))
        self.addCleanup(page.close)
        self.addCleanup(self.close_chrome, page, chrome)
        return page

    def close_chrome(self, page, chrome):
        """Lets Chrome shut down by itself, so it leaves no process behind holding its profile folder."""
        try:
            page.post("Browser.close")
            chrome.wait(5)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def load(self, hash=""):
        self.page.send("Page.navigate", url=self.url + hash)
        self.until("the board's cards", "document.readyState === 'complete' && !!document.querySelector('[data-id=\"BE-03\"]')")

    def type_note(self, text):
        self.page.js(f"{NOTE_BOX}.focus()")
        self.page.send("Input.insertText", text=text)
        self.page.js(ADD_NOTE)

    def notes(self, tid):
        return [n["text"] for n in kanvas.find_task(kanvas.load_board(self.path), tid)["notes"]]

    def cli(self, *args):
        proc = subprocess.run([sys.executable, SCRIPT, "--board", self.path, *args, "--as", "smoke-test"],
                              env=self.env, stdin=subprocess.DEVNULL, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))

    def test_a_task_opens_saves_a_note_and_shows_agent_changes(self):
        self.load()
        self.assertTrue(self.page.js("document.querySelector('#panel').hidden"))
        self.page.js("location.hash = 'BE-03'")
        self.until("the panel of BE-03", "!document.querySelector('#panel').hidden && "
                                         "document.querySelector('#panel .cid').textContent === 'BE-03'")
        self.assertEqual(self.page.js("document.querySelector('#panel .p-title').value"), "City search endpoint")

        note = "Checked the search: Zürich and São Paulo both resolve."
        self.type_note(note)
        self.wait_for("the note in kanvas.json", lambda: note in self.notes("BE-03"))
        self.until("the saved note in the panel", f"document.querySelector('#panel').innerText.includes({json.dumps(note)})"
                                                  f" && {NOTE_BOX}.value === ''")

        self.cli("note", "BE-03", "Agent note from the CLI")
        self.cli("start", "FE-04")
        self.until("the agent's note and start", "document.querySelector('#panel').innerText.includes('Agent note from the CLI')"
                                                 " && document.querySelector('[data-id=\"FE-04\"]').classList.contains('s-doing')",
                   timeout=5)
        self.assertEqual(self.page.logged(), [])

    def test_a_pasted_image_is_kept_next_to_the_board_and_shows_in_the_note(self):
        self.load("#BE-03")
        self.until("the panel of BE-03", "document.querySelector('#panel .cid')?.textContent === 'BE-03'")
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        self.page.js(f"{NOTE_BOX}.focus(); document.execCommand('insertText', false, 'Before:');")
        self.page.js(f"""(() => {{
            const dt = new DataTransfer(), bytes = Uint8Array.from(atob('{png}'), (c) => c.charCodeAt(0));
            dt.items.add(new File([bytes], 'login form.png', {{ type: 'image/png' }}));
            {NOTE_BOX}.dispatchEvent(new ClipboardEvent('paste', {{ clipboardData: dt, bubbles: true, cancelable: true }}));
        }})()""")
        text = self.until("the image's Markdown in the note box",
                          f"/^Before:\\n!\\[login form\\]\\(kanvas-files\\/[0-9a-f]{{16}}\\.png\\)$/.test({NOTE_BOX}.value) && {NOTE_BOX}.value")
        ref = re.search(r"\((.*)\)", text).group(1)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "demo", ref)))
        self.page.js(ADD_NOTE)
        self.wait_for("the note in kanvas.json", lambda: text in self.notes("BE-03"))
        img = f"document.querySelector('#panel .md img[src=\"{ref}\"]')"
        self.until("the image loaded in the panel", f"{img}?.complete && {img}.naturalWidth === 1")
        self.page.js(f"{img}.click()")
        self.until("the image at full size", "document.querySelector('dialog.lightbox[open] img')?.naturalWidth === 1")
        self.page.js("document.querySelector('dialog.lightbox').click()")
        self.until("the full-size image to close", "!document.querySelector('dialog.lightbox')")
        self.assertEqual(self.page.logged(), [])

    def test_an_image_still_uploading_stays_with_the_task_it_was_pasted_in(self):
        self.load("#BE-03")
        self.until("the panel of BE-03", "document.querySelector('#panel .cid')?.textContent === 'BE-03'")
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        # The upload waits until the test lets it go, after another task has opened.
        self.page.js("""(() => {
            const fetch = window.fetch;
            window.fetch = (url, opts) => String(url).endsWith('api/files')
                ? new Promise((resolve) => { window.releaseUpload = () => resolve(fetch(url, opts)); }) : fetch(url, opts);
        })()""")
        self.page.js(f"""(() => {{
            const dt = new DataTransfer(), bytes = Uint8Array.from(atob('{png}'), (c) => c.charCodeAt(0));
            dt.items.add(new File([bytes], 'login form.png', {{ type: 'image/png' }}));
            {NOTE_BOX}.focus();
            {NOTE_BOX}.dispatchEvent(new ClipboardEvent('paste', {{ clipboardData: dt, bubbles: true, cancelable: true }}));
        }})()""")
        self.until("the upload to start", "typeof window.releaseUpload === 'function'")
        # Leaving the page now would lose the image, which is in no editor yet.
        leave = "(() => { const e = new Event('beforeunload', { cancelable: true }); dispatchEvent(e); return e.defaultPrevented; })()"
        self.assertTrue(self.page.js(leave))
        self.page.js("location.hash = 'FE-04'")
        self.until("the panel of FE-04", "document.querySelector('#panel .cid')?.textContent === 'FE-04'")
        self.page.js("window.releaseUpload()")
        self.until("the upload to finish", "!document.querySelector('textarea.uploading') && "
                                           "[...document.querySelectorAll('.toast')].some((e) => e.textContent.includes('BE-03'))")
        self.assertEqual(self.page.js(f"{NOTE_BOX}.value"), "")
        self.page.js("location.hash = 'BE-03'")
        self.until("the image in BE-03's note box", "document.querySelector('#panel .cid')?.textContent === 'BE-03' && "
                                                    f"/^!\\[login form\\]\\(kanvas-files\\/[0-9a-f]{{16}}\\.png\\)$/.test({NOTE_BOX}.value)")
        self.assertEqual(self.page.logged(), [])

    def test_a_failed_save_gives_the_typed_note_back(self):
        self.load("#BE-03")
        self.until("the panel of BE-03", "document.querySelector('#panel .cid')?.textContent === 'BE-03'")
        self.assertEqual(self.page.logged(), [])
        before = self.notes("BE-03")

        refusal = base64.b64encode(json.dumps({"error": "the disk is full"}).encode("utf-8")).decode("ascii")

        def refuse_saves(p):
            if p["request"]["method"] == "PUT":
                self.page.post("Fetch.fulfillRequest", requestId=p["requestId"], responseCode=500, body=refusal,
                               responseHeaders=[{"name": "Content-Type", "value": "application/json"}])
            else:
                self.page.post("Fetch.continueRequest", requestId=p["requestId"])
        self.page.on("Fetch.requestPaused", refuse_saves)
        self.page.send("Fetch.enable", patterns=[{"urlPattern": "*/api/board"}])
        note = "A note typed while saving fails."
        self.type_note(note)
        self.until("the note back in its box", f"{NOTE_BOX}.value === {json.dumps(note)}")
        self.assertIn("Not saved: the disk is full", self.page.js("document.querySelector('#toasts').innerText"))
        self.assertEqual(self.notes("BE-03"), before)
        self.page.send("Fetch.disable")
        self.page.js(ADD_NOTE)
        self.wait_for("the note in kanvas.json once saving works", lambda: self.notes("BE-03") == before + [note])
        # Stopping the server before its answer arrives would leave the page unsure the note was saved, so it keeps it.
        self.until("the saved note on the page", f"document.querySelector('#panel .p-body').innerText.includes({json.dumps(note)}) && {NOTE_BOX}.value === ''")

        stop(self.server)
        self.until("the lost connection banner", "!document.querySelector('#lost').hidden")
        lost = "A note typed while Kanvas is stopped."
        self.type_note(lost)
        self.until("the note back in its box", f"{NOTE_BOX}.value === {json.dumps(lost)}")
        self.assertIn("Not saved", self.page.js("document.querySelector('#toasts').innerText"))
        self.assertEqual(self.notes("BE-03"), before + [note])
        self.assertEqual([e for e in self.page.logged() if not e.startswith("network: ")], [])


if __name__ == "__main__":
    unittest.main()
