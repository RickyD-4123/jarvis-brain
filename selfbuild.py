"""Self-build: Jarvis changes his own code, the way Claude Code does, but never straight onto the real files.

    "Upgrade yourself: add a button that ..."   -> a build starts
    "How's the build going?"                    -> status
    "Change the build: make it blue instead"    -> a revision of the waiting build
    "Apply the build" / the Apply button        -> applied to the code folder and committed
    "Discard the build" / the Discard button    -> thrown away

1. A throwaway git worktree is made from the code folder, carrying over any uncommitted changes, so the
   build starts from exactly what is on disk now. Your data is never in it.
2. Claude Code runs in that copy with file tools only (read, search, edit, write: no shell, no web, no
   mail), with your words as the request. It cannot reach your email or documents, so nothing it reads
   from outside can steer it.
3. Jarvis then checks the copy himself: every changed Python file compiles, the unit tests pass, a test
   server starts on a spare port (with a scratch copy of your data, so your real reminders and memory are
   untouched), the page loads with no script errors, preflight passes (failures that were already there
   before the change don't count against it), and no personal data is in the code. Failures go back to
   Claude for up to two repairs.
4. A desk card shows what changed, the checks, and the full diff. Nothing reaches the code folder until
   you apply it; a change to his safety code (security, the hooks, this file...) is flagged on the card.
5. Apply re-checks that the files it touches haven't changed since the build started, copies the tested
   files over and commits only those files (the pre-commit personal-data hook runs). If the commit is
   refused, the originals are put back. Viewer changes show on a page reload; Python changes need a restart.

Only your own typed or spoken words at the desk can start, change or apply a build: it is not a tool the
chat brain can call, so an email or a web page can never trigger one.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request

AGENT_TIMEOUT = 30 * 60          # seconds Claude may spend on one build or repair
MAX_REPAIRS = 2
MAX_REQUEST = 2000
PATCH_SHOWN = 60_000             # characters of the diff kept on the card
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Files whose change gets a warning on the card: his safety rules and the build machinery itself.
PROTECTED = ("secure.py", "selfbuild.py", "localhand.py", "gtools.py", "memory.py", "scripts/",
             "internship_apply/safety.py", "internship_apply/form_filler.py", ".gitignore", "desktop/src/")

AGENT_RULES = """You are improving Jarvis, the personal-assistant app in the current folder, at its owner's request.
This folder is a throwaway copy: the owner reviews your change before anything is applied, and Jarvis runs the
tests and preflight after you finish, so you cannot run commands (you have file tools only).

How to work:
- Read README.md and the files you'll touch first. Match the surrounding code: naming, comment density, idiom.
- Make the smallest change that fully does what was asked. No unrelated refactors.
- viewer/index.html is ONE big script inside an IIFE: a duplicate const/let name or a syntax error silently
  breaks the whole page. Grep for every new top-level name before adding it.
- Everything from outside (model answers, notes, email, documents) must go through esc() before it becomes HTML.
- Keep every security check (server.py gate() and Origin checks, secure.py, the tool rules in gtools.py,
  localhand.py limits, the internship safety rules) unless the request explicitly asks to change them.
- Never put personal data in code: no names, emails, phone numbers, home-folder paths or keys. Never touch
  config.json, secrets/, data/ or notes/.
- No new dependencies, no network fonts or CDNs beyond the ones already loaded.
- Don't change desktop/ unless the request is about the desktop app (its tests can't run here).
- In Python string literals, Windows paths need doubled backslashes.

When you're done, end your reply with one line:
SUMMARY: <two to four plain sentences for the owner: what changed and how to use it>"""


class BuildError(Exception):
    pass


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_python():
    """A real Python for the checks: this one, unless we're the packaged backend exe."""
    if not getattr(sys, "frozen", False):
        return sys.executable
    return shutil.which("python") or shutil.which("py")


class Builder:
    def __init__(self, source_dir, home, cards, run_agent, python=None, builds_dir=None, run_checks=None):
        """source_dir: the git folder holding Jarvis's code (None: self-build unavailable).
        home: Jarvis's data folder (copied for the test server, never changed).
        run_agent(prompt, cwd, system_append, timeout) -> (text, error): one Claude Code run with file tools.
        run_checks(worktree, changed) -> [(name, ok, detail)]: replaces the real checks in tests."""
        self.source = os.path.realpath(source_dir) if source_dir else None
        self.home = os.path.realpath(home)
        self.cards, self.run_agent = cards, run_agent
        self.python = python or find_python()
        # Long-form path: Claude Code refuses to work under an 8.3 short name (the PROGRA~1 kind TEMP can use).
        self.root = os.path.realpath(builds_dir or os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
                                                                "Jarvis", "builds"))
        self.state_path = os.path.join(self.home, "data", "build.json")
        self.custom_checks = run_checks
        self.lock = threading.Lock()
        self.thread = None
        try:
            with open(self.state_path, encoding="utf-8") as f:
                self.state = json.load(f)
        except (OSError, ValueError):
            self.state = {}
        if self.state.get("status") == "running":           # the server stopped mid-build
            self.state.update(status="failed", stage="interrupted",
                              error="I was restarted in the middle of the build, so it was abandoned.")
            self._cleanup()
            self._save()

    # ---------------------------------------------------------------- state

    def _save(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, self.state_path)

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)
            self._save()
        if "stage" in kw:
            self.log(f"stage: {kw['stage']}")

    def log(self, text):
        """The build's own log, next to its worktree (kept after the build, for when something goes wrong)."""
        try:
            os.makedirs(self.root, exist_ok=True)
            with open(os.path.join(self.root, f"{self.state.get('id', 'build')}.log"), "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {text}\n")
        except OSError:
            pass

    def status(self):
        with self.lock:
            s = {k: v for k, v in self.state.items() if k not in ("patch_path", "worktree", "test_home")}
        s["available"] = bool(self.source)
        return s

    def brief(self):
        """The few fields the desk polls every two seconds (the HUD's self-build line)."""
        with self.lock:
            return {k: self.state.get(k) for k in ("id", "status", "stage", "started")}

    # ---------------------------------------------------------------- git

    def git(self, cwd, *args, check=True, binary=False):
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, creationflags=NO_WINDOW,
                           **({} if binary else {"text": True, "encoding": "utf-8", "errors": "replace"}))
        if check and p.returncode != 0:
            err = p.stderr if not binary else p.stderr.decode("utf-8", "replace")
            raise BuildError(f"git {args[0]} failed: {err.strip()[-400:]}")
        return p.stdout

    def snapshot(self, wt):
        """The worktree's current files as a git tree id (everything, including new files)."""
        self.git(wt, "add", "-A")
        return self.git(wt, "write-tree").strip()

    def changed(self, base, result):
        rows = self.git(self.state["worktree"], "diff", "--name-status", "--no-renames", base, result).splitlines()
        return [r.split("\t", 1) for r in rows if "\t" in r]   # [status, path], as it's stored in build.json

    # ---------------------------------------------------------------- start / revise

    def start(self, request, source="desk"):
        """Start a build in the background. Returns the line to say."""
        request = " ".join(str(request).split())[:MAX_REQUEST]
        if not self.source:
            return ("I can't find my own source code from here, sir. Set source_dir in config.json to the "
                    "folder that holds it, and ask me again.")
        if not self.python:
            return "I need Python on this computer to test a build, sir, and I can't find it."
        with self.lock:
            if self.state.get("status") == "running":
                return "I'm already building something, sir. Ask me how it's going, or wait for the card."
            if self.state.get("status") == "ready":
                return ("There's a finished build waiting for you, sir. Apply it or discard it first, or say "
                        "\"change the build\" to adjust it.")
            bid = time.strftime("%Y%m%d-%H%M%S")
            self.state = {"id": bid, "status": "running", "stage": "preparing", "request": request,
                          "revisions": [], "source": source, "started": time.time(), "finished": None,
                          "summary": "", "files": [], "checks": [], "protected": [], "error": None}
            self._save()
        self.thread = threading.Thread(target=self._run, args=(request, False), daemon=True)
        self.thread.start()
        return ("On it, sir. I'll build that on a separate copy of myself, test it, and show you the changes "
                "before anything is applied. It usually takes a few minutes.")

    def revise(self, request):
        request = " ".join(str(request).split())[:MAX_REQUEST]
        with self.lock:
            if self.state.get("status") != "ready":
                return ("There's no finished build to change, sir." if self.state.get("status") != "running"
                        else "Still building, sir. Ask me again once the card arrives.")
            self.state.update(status="running", stage="revising", finished=None, error=None)
            self.state["revisions"].append(request)
            self._save()
        self.thread = threading.Thread(target=self._run, args=(request, True), daemon=True)
        self.thread.start()
        return "Very good, sir. Adjusting the build and testing it again."

    def _run(self, request, revision):
        try:
            if not revision:
                self._prepare()
            wt = self.state["worktree"]
            self._set(stage="building")
            if revision:
                prompt = (f"ORIGINAL REQUEST: {self.state['request']}\n\nYou already made a change for it in this "
                          f"folder (git diff {self.state['base'][:12]} shows it). The owner now asks:\n\n{request}")
            else:
                prompt = f"THE OWNER ASKS:\n\n{request}"
            self.log(f"agent prompt:\n{prompt}")
            text, error = self.run_agent(prompt, wt, AGENT_RULES, AGENT_TIMEOUT)
            self.log(f"agent replied (error={error!r}):\n{(text or '')[-3000:]}")
            if error:
                raise BuildError(f"Claude couldn't finish the build: {error[-400:]}")
            summary = self._summary(text)
            for attempt in range(MAX_REPAIRS + 1):
                result = self.snapshot(wt)
                files = self.changed(self.state["base"], result)
                if not files:
                    raise BuildError("Claude finished without changing anything. " + (summary or text or "")[-300:])
                self._set(stage="testing" if attempt == 0 else f"testing repair {attempt}", files=files)
                checks = self.checks(wt, [p for _, p in files])
                for name, ok, detail in checks:
                    self.log(f"check {'PASS' if ok else 'FAIL'}: {name}\n{detail[-3000:] if not ok else detail[-300:]}")
                failed = [c for c in checks if not c[1]]
                if not failed or attempt == MAX_REPAIRS:
                    break
                self._set(stage=f"repairing ({attempt + 1} of {MAX_REPAIRS})", checks=checks)
                report = "\n\n".join(f"CHECK FAILED: {n}\n{d[-3000:]}" for n, _, d in failed)
                text, error = self.run_agent(
                    f"THE OWNER ASKED:\n\n{self.state['request']}\n\n"
                    + "".join(f"THEN: {r}\n\n" for r in self.state["revisions"])
                    + f"You made the change in this folder, but Jarvis's checks failed:\n\n{report}\n\n"
                      "Fix the cause (not the checks). Keep what already works.", wt, AGENT_RULES, AGENT_TIMEOUT)
                self.log(f"repair agent replied (error={error!r}):\n{(text or '')[-3000:]}")
                if error:
                    raise BuildError(f"Claude couldn't repair the build: {error[-400:]}")
                summary = self._summary(text) or summary
            patch = self.git(wt, "diff", self.state["base"], result)
            protected = sorted({p for _, p in files if p.startswith(PROTECTED) or p in PROTECTED})
            ok = all(c[1] for c in checks)
            self._set(status="ready" if ok else "failed", stage="done", result=result, summary=summary,
                      files=files, checks=checks, protected=protected,
                      diff=patch[:PATCH_SHOWN], diff_truncated=len(patch) > PATCH_SHOWN, finished=time.time(),
                      error=None if ok else "Some checks still fail after two repairs, so it can't be applied.")
            self._card()
            if not ok:
                self._cleanup()
        except Exception as e:
            self.log("failed:\n" + traceback.format_exc())
            self._set(status="failed", stage="failed", error=str(e), finished=time.time())
            self._cleanup()
            self._card()

    @staticmethod
    def _summary(text):
        for line in reversed((text or "").splitlines()):
            if line.strip().upper().startswith("SUMMARY:"):
                return line.split(":", 1)[1].strip()
        return (text or "").strip()[-600:]

    def _prepare(self):
        """The worktree: HEAD, plus the uncommitted changes and new files in the code folder."""
        os.makedirs(self.root, exist_ok=True)
        wt = os.path.join(self.root, self.state["id"])
        self.git(self.source, "worktree", "add", "--detach", wt, "HEAD")
        self._set(worktree=wt)
        diff = self.git(self.source, "diff", "--binary", "HEAD", binary=True)
        if diff.strip():
            p = subprocess.run(["git", "apply", "--binary", "--whitespace=nowarn"], cwd=wt, input=diff,
                               capture_output=True, creationflags=NO_WINDOW)
            if p.returncode:
                raise BuildError("couldn't copy the uncommitted changes: " + p.stderr.decode("utf-8", "replace")[-300:])
        for rel in self.git(self.source, "ls-files", "--others", "--exclude-standard").splitlines():
            src, dst = os.path.join(self.source, rel), os.path.join(wt, rel)
            if os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
        strings = os.path.join(self.source, "personal_strings.local.txt")   # git-ignored: lets the scan run
        if os.path.isfile(strings):
            shutil.copy2(strings, os.path.join(wt, "personal_strings.local.txt"))
        self._set(base=self.snapshot(wt))

    # ---------------------------------------------------------------- checks

    def checks(self, wt, changed):
        if self.custom_checks:
            return self.custom_checks(wt, changed)
        out = []
        py = [p for p in changed if p.endswith(".py") and os.path.isfile(os.path.join(wt, p))]
        if py:
            r = self._py(wt, ["-B", "-m", "py_compile", *py], 120)
            out.append(("Changed Python files compile", r[0] == 0, r[1]))
            if r[0] == 0:
                for folder in ("tests", "internship_apply/tests"):
                    if os.path.isdir(os.path.join(wt, folder)):
                        r = self._py(wt, ["-B", "-m", "unittest", "discover", "-s", folder, "-t", "."], 900)
                        out.append((f"Unit tests ({folder})", r[0] == 0, r[1]))
        test_home = self._test_home()
        try:
            port, proc = self._serve(wt, test_home)
            try:
                if port is None:
                    out.append(("Test server starts", False, proc))
                    return out
                r = self._py(wt, ["-B", os.path.join("scripts", "page_check.py"), f"http://127.0.0.1:{port}/"], 180)
                out.append(("Page loads with no script errors", r[0] in (0, 2),
                            r[1] + ("\n(Chrome or Playwright unavailable: not checked)" if r[0] == 2 else "")))
                failing, log = self._preflight(wt, test_home, port)
            finally:
                self._stop(proc)
            if failing:
                # Failures that were already there before the change don't count against it.
                before = self._baseline_failures(wt, test_home)
                new = sorted(failing - before)
                detail = log[-6000:] + (f"\n\nAlready failing before this change: {', '.join(sorted(before & failing))}"
                                        if before & failing else "")
                out.append(("Preflight", not new, detail if new else "passes, apart from checks that were already "
                            f"failing before this change: {', '.join(sorted(failing))}"))
            else:
                out.append(("Preflight", True, log.strip().splitlines()[-1] if log.strip() else "passed"))
        finally:
            shutil.rmtree(test_home, ignore_errors=True)
        r = self._py(wt, ["-B", os.path.join("scripts", "check_no_personal_data.py"), "--include-untracked"], 300)
        out.append(("No personal data in the code", r[0] == 0, r[1]))
        return out

    def _py(self, cwd, args, timeout, env=None):
        try:
            p = subprocess.run([self.python, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout, creationflags=NO_WINDOW,
                               env={**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})})
            return p.returncode, (p.stdout + p.stderr)[-8000:]
        except subprocess.TimeoutExpired:
            return 1, f"timed out after {timeout} s"

    def _test_home(self):
        """A scratch copy of your data, so the test server's reminders, cards and memory are its own."""
        th = os.path.realpath(tempfile.mkdtemp(prefix="jarvis-test-home-"))   # long names: paths must compare equal
        for name in ("config.json", "secrets", "notes", "data", "graph-data.js",
                     os.path.join("internship_apply", "data"), os.path.join("internship_apply", "config.local.yaml"),
                     os.path.join("internship_apply", "profile.yaml")):
            src = os.path.join(self.home, name)
            dst = os.path.join(th, name)
            if os.path.isdir(src):                       # never the build state: the test server isn't building
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.log", "builds", "*.db-journal", "build.json*"))
            elif os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
        return th

    def _serve(self, wt, test_home):
        port = free_port()
        env = {**os.environ, "JARVIS_HOME": test_home, "JARVIS_PORT": str(port), "PYTHONIOENCODING": "utf-8",
               "JARVIS_SELF_BUILD": "0"}                 # a test server never builds (or cleans up) anything itself
        env.pop("CLAUDECODE", None)
        log = open(os.path.join(test_home, "server.log"), "w", encoding="utf-8")
        proc = subprocess.Popen([self.python, "-B", "server.py"], cwd=wt, env=env, stdout=log, stderr=subprocess.STDOUT,
                                creationflags=NO_WINDOW)
        proc.log = log                                    # closed by _stop, so the scratch home can be deleted
        for _ in range(120):
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                    if r.status == 200:
                        return port, proc
            except OSError:
                time.sleep(0.5)
        self._stop(proc)
        with open(os.path.join(test_home, "server.log"), encoding="utf-8", errors="replace") as f:
            return None, "the test server didn't start:\n" + f.read()[-4000:]

    @staticmethod
    def _stop(proc):
        if proc is None or isinstance(proc, str):
            return
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        proc.log.close()

    def _preflight(self, wt, test_home, port):
        code, log = self._py(wt, ["-B", "preflight.py"], 900, env={"JARVIS_HOME": test_home, "JARVIS_PORT": str(port)})
        failing = {line.split("FAIL", 1)[1].strip() for line in log.splitlines() if line.strip().startswith("✘ FAIL")}
        if code and not failing:
            failing = {"preflight did not finish"}
        return failing, log

    def _baseline_failures(self, wt, test_home):
        """Preflight on the code as it was before the change (same worktree, files swapped back for a moment)."""
        result = self.snapshot(wt)
        self.git(wt, "read-tree", "-u", "--reset", self.state["base"])
        try:
            port, proc = self._serve(wt, test_home)
            if port is None:
                return set()
            try:
                return self._preflight(wt, test_home, port)[0]
            finally:
                self._stop(proc)
        finally:
            self.git(wt, "read-tree", "-u", "--reset", result)

    # ---------------------------------------------------------------- apply / discard

    def apply(self):
        """Apply the waiting build to the code folder and commit it. Returns {"ok", "say", "reload", "restart"}."""
        with self.lock:
            s = dict(self.state)
        if s.get("status") != "ready":
            return {"ok": False, "say": "There's no finished build waiting, sir."}
        files = [p for _, p in s["files"]]
        try:
            # The files it touches must be exactly as they were when the build started.
            moved = []
            for status, path in s["files"]:
                full = os.path.join(self.source, path)
                want = self.git(self.source, "rev-parse", "-q", "--verify", f"{s['base']}:{path}", check=False).strip()
                have = self.git(self.source, "hash-object", "--", path).strip() if os.path.isfile(full) else ""
                if want != have:
                    moved.append(path)
            if moved:
                return {"ok": False, "say": "Those files have changed since I started the build, sir, so I won't "
                        f"apply it over them ({', '.join(moved[:4])}). Discard it and ask me again."}
            # Files that already held uncommitted work: it goes into this commit too, so say so.
            dirty = [p for p in files if subprocess.run(["git", "diff", "--quiet", "HEAD", "--", p], cwd=self.source,
                                                        capture_output=True, creationflags=NO_WINDOW).returncode]
            # Copied over file by file from the tested copy (git apply trips on Windows line endings), with the
            # originals held in memory so a refused commit can put everything back exactly.
            wt, originals = s["worktree"], {}
            for status, path in s["files"]:
                full = os.path.join(self.source, path)
                originals[path] = None
                if os.path.isfile(full):
                    with open(full, "rb") as f:
                        originals[path] = f.read()
            for status, path in s["files"]:
                src, full = os.path.join(wt, path), os.path.join(self.source, path)
                if status == "D":
                    os.remove(full)
                else:
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    shutil.copyfile(src, full)
            msg = (f"Jarvis self-build: {s['request'][:60]}{'...' if len(s['request']) > 60 else ''}\n\n"
                   f"{s['summary']}\n\nRequested: {s['request']}\n"
                   + "".join(f"Revised: {r}\n" for r in s.get("revisions", []))
                   + (f"Also includes earlier uncommitted work in: {', '.join(dirty)}\n" if dirty else "")
                   + "\nBuilt and tested by Jarvis (Claude Code), applied on the owner's approval.\n\n"
                     "Co-Authored-By: Claude <noreply@anthropic.com>")
            self.git(self.source, "add", "-A", "--", *files)
            commit = subprocess.run(["git", "commit", "-q", "-F", "-", "--", *files], cwd=self.source, input=msg,
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    creationflags=NO_WINDOW)
            if commit.returncode:
                self.git(self.source, "reset", "-q", "--", *files, check=False)
                for path, data in originals.items():
                    full = os.path.join(self.source, path)
                    if data is None:
                        if os.path.isfile(full):
                            os.remove(full)
                    else:
                        with open(full, "wb") as f:
                            f.write(data)
                return {"ok": False, "say": "The commit was refused, sir, so I took the change back out: "
                        + (commit.stderr or commit.stdout).strip()[-300:]}
        except BuildError as e:
            return {"ok": False, "say": f"I couldn't apply it, sir: {e}"}
        sha = self.git(self.source, "rev-parse", "--short", "HEAD").strip()
        self._set(status="applied", commit=sha, applied=time.time())
        self._cleanup()
        reload = any(p.startswith("viewer/") for p in files)
        restart = any(p.endswith(".py") for p in files)
        say = f"Applied and committed as {sha}, sir."
        if dirty:
            say += f" That commit also holds the unsaved changes that were already in {', '.join(dirty[:3])}."
        if restart:
            say += " The backend part takes effect after a restart: the power button, then start me again."
        elif reload:
            say += " Reloading the page to pick it up."
        say += " The desktop app gets it at its next rebuild."
        return {"ok": True, "say": say, "reload": reload and not restart, "restart": restart, "commit": sha}

    def discard(self):
        with self.lock:
            status = self.state.get("status")
        if status == "running":
            return "It's still building, sir. I'll stop it once it finishes; say discard again then."
        if status != "ready":
            return "There's no build waiting, sir."
        self._set(status="discarded", finished=time.time())
        self._cleanup()
        return "Discarded, sir. Nothing was changed."

    def _cleanup(self):
        wt = self.state.get("worktree")
        here = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
        if wt and os.path.realpath(wt) in (here, self.source):   # never the folder this code is running from
            return
        if wt and self.source:
            self.git(self.source, "worktree", "remove", "--force", wt, check=False)
            shutil.rmtree(wt, ignore_errors=True)
            self.git(self.source, "worktree", "prune", check=False)

    # ---------------------------------------------------------------- desk card

    def _card(self):
        s = self.status()
        if s["status"] == "ready":
            title, text = "Self-build ready", (s["summary"] or "The change is ready.") + " Apply it or discard it."
        elif s["status"] == "failed":
            title, text = "Self-build failed", s.get("error") or "The build failed."
        else:
            return
        self.cards.add("build", title, text, "normal", ref={"build": s})
