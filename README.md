# Jarvis

A local personal assistant for Windows: a 3D "knowledge galaxy" of your Markdown notes, a voice desk
in the browser, Gmail/Calendar/Drive tools, reminders and a morning briefing, file tools limited to
your workspace folders, and `internship_apply/` (finds, scores and drafts internship applications,
never submitting without your approval). Claude runs through your own Claude Code subscription
(the `claude` CLI): no API key, nothing pay-per-use.

- `python build.py` indexes `notes/**/*.md` into `viewer/graph-data.js` (the notes here are sample data).
- `python server.py` serves the desk on http://127.0.0.1:4700; `start_jarvis.pyw` does the same and opens a window.
- `python preflight.py` runs the live checks against a running server.
- `python -m unittest discover -s internship_apply/tests -t .` runs the internship tests (no network).
- `python -m unittest discover -s tests -t .` runs the memory, self-build and app-update tests (no network).
- See [internship_apply/README.md](internship_apply/README.md) for the internship module.
- `desktop/` is the Windows desktop app (below). The web version above keeps working unchanged.
- Spoken answers: whole sentences up to about 70 words, then "The rest is on screen." Say "go on" to hear
  the rest. The screen shows the full answer, formatted (lists, headings, code).

Requirements: Windows, Python 3.11+, Claude Code signed in (`claude` on PATH), Google Chrome, and
`pip install -r requirements.txt` for `internship_apply`.

## Memory

Jarvis learns about you as you talk (`memory.py`): who you are, how you like things done, the shape of
your recurring work (assignment layouts, citation styles, course codes, templates) and what's going on
(projects, deadlines). Every few turns, or after a quiet minute and a half, the recent turns go to the
brain with what's already known, and it answers with strict JSON: what to add, correct or forget. Only
your own words count as a source: what an email, document or web page says is never stored as a fact
about you. The memories go into the chat, tool and attachment prompts as background facts, never as
instructions.

- **See and edit it**: Settings > Memory. Click a line to correct it (it's then yours, and learning never
  rewrites it), × forgets it, the box at the bottom adds one, and the switch turns learning off.
- **By voice**: "what do you know about me?", "forget that I work Tuesdays", "stop learning" / "start learning".
- Stored in `data/memory.json` (git-ignored, owner-only, never served). Preflight's probe questions carry
  `X-Jarvis-Probe: 1` and never teach it anything.

## Conversation memory

Jarvis remembers your conversations, not just facts about you (`conversation.py`): every exchange goes
to a log (`data/history/conversations.jsonl`); the last few stay verbatim, and older ones are folded into
a running summary instead of being dropped, so the thread survives long chats and restarts; and each
question pulls in the most relevant earlier exchanges, so "what did we decide about the essay?" works
weeks later. Chat, Google, attachments and work mode all share it. Preflight's probes are never logged.

## Work mode

Work mode is Jarvis as a Claude Code session you talk to (`agent.py`): say "let's work" or press **Work**,
and every message continues one persistent session with every Claude Code tool (shell, files, web,
sub-agents), the way you'd work with Claude Code itself. It remembers the whole conversation (Claude Code
condenses it when it gets long), keeps its own notes between sessions in `data/agent-memory/` (the same
memory system Claude Code uses: `MEMORY.md` plus one note per fact, `agentmemory.py`), and starts each
session with the conversation summary and what memory knows about you.

- **Live**: his steps stream into the conversation as he works (the current one shows in the status
  line). "Stop" interrupts; anything you say while he's busy is queued and delivered when he finishes.
- **Hear little, see everything**: each reply starts with a one-line `SAY:` headline (at most ~20 words),
  and only that is spoken. Code, output and detail go in a written report on the desk (rendered markdown:
  code blocks, tables, lists, links), never read aloud. Short tones mark started / done / needs you /
  failed. When he needs a decision he asks one short question (`ASK:`) with `OPTIONS:` that appear as
  buttons: click one, or say "option two". Settings > Work mode sets how much he says: Quiet (tones
  only), Brief (headlines, the default) or Full (the report too, minus code). By voice: "read it", "skip",
  "quiet", "brief", "full".
- **Building himself**: he works in his *workbench*, a persistent working copy of his code
  (`%LOCALAPPDATA%\Jarvis\builds\workbench`), and tests there (`scripts/preview.py` lets him see the
  page). After a turn that changed it, every check runs by itself; a failure goes back to him to fix and
  is re-checked. The bar above the message box then offers **Update the app** (commit, rebuild and
  reinstall the desktop app; a failed build goes back to him too), **Try it** and **Discard**. Undo works too.
  When he asks a question with choices, an **Other** bubble lets you type your own answer.
- **Anything else**: your workspace folders are open to him, so documents, research, scripts and file
  organising work the same way. He asks before anything hard to reverse.
- **Not here**: your Google account stays with the normal desk. His file tools are denied your config and
  secrets; no git push. His shell is unrestricted (the full-access choice), so stay in charge of what you
  ask for.
- "Leave work mode" (or the button) goes back to the normal desk; "new session" starts a fresh
  conversation (his notes and the workbench stay). The one-shot "upgrade yourself: ..." builds below
  still work too, in the background.

## Self-build

Jarvis can change his own code the way Claude Code does (`selfbuild.py`), but never straight onto the
real files. Say or type, for example, "upgrade yourself: add a dark-mode switch to the settings".

1. A throwaway git worktree is made from the code folder, including uncommitted changes, plus a scratch
   copy of your data for its test servers (`$JARVIS_TEST_HOME`).
2. Claude Code runs in it like a normal Claude Code session: shell, files, web search and fetch,
   sub-agents, all pre-approved (`--permission-mode bypassPermissions`, effort medium by default), with `CLAUDE.md`,
   the recent conversation, what memory knows about you and the past builds as context. It tests its own
   work as it goes (`scripts/preview.py` screenshots the desk from its copy). If the request is ambiguous
   it asks first: the question is spoken and shown on a card, and your next words (or the card's answer
   box) answer it. Answers, repairs and revisions continue the same session, so it remembers.
3. Guardrails: no `git push`/remote/config changes, and its file tools are denied your config and secrets. Its shell could still reach them (the accepted cost of full access), so every command it ran and
   every page it read is listed on the card, and a change to the real code folder during the build is flagged.
4. Jarvis then checks it himself: changed Python compiles, both unit-test suites pass, a test server
   serves a page with no script errors (`scripts/page_check.py`), preflight passes (failures that were
   already there before the change don't count), and no personal data is in the code. Failures go back
   to the same session for up to two repairs. Then before/after screenshots.
5. A desk card shows the summary, screenshots, files, checks, what the builder did and the full diff, with
   **Try it** (the changed Jarvis, live in Chrome, on a scratch copy of your data; Google is real),
   **Apply** and **Discard**. Changes to his safety code are flagged.
6. Apply re-checks that the files haven't changed since, copies them in and commits just those files
   (the pre-commit hook still runs; if it refuses, the originals are put back). Page changes reload the
   desk; Python changes need a restart. **Undo** (or "undo the last build") reverts that commit.

Also: "how's the build going?", "change the build: ..." (revises the waiting build), "apply the build",
"discard the build" / "cancel the build" (also stops one that's running). Only your own words at the desk
can start, answer, change, apply or undo a build: it isn't a tool the chat brain can call, so an email or
web page can never trigger one. The web version finds its code folder itself; the desktop app needs
`"source_dir"` in its config.json (an app update sets it). Each build logs to
`%LOCALAPPDATA%\Jarvis\builds\<id>.log`; history is in `data/build_history.json`.

**Updating the desktop app by voice** (`appupdate.py`): "update the app" bumps the patch version, runs
`npm run dist` (backend exe, installer, build check), commits the version and puts up a card with
**Install**. Install starts a helper outside Jarvis that waits for him to close, runs the installer
silently over the old version and starts the new one; from the desktop app he closes himself, from the
web version close the app first. Your data in `%APPDATA%\Jarvis\home` is untouched.

## Desktop app

`desktop/` wraps Jarvis as an installable Windows app (Electron). It starts the backend itself on a
free port bound to 127.0.0.1 (4700 when it's free), waits for `GET /health`, then opens the desk, and
stops the backend and everything it started when you close the desk, press the power button or quit.
Launching it a second time just brings the open desk forward.

- **The desk window is Google Chrome in app mode**, with its own profile (`%APPDATA%\Jarvis\chrome-desk`).
  Voice input (`webkitSpeechRecognition`) only works in real Chrome, not in Electron. The app controls that
  window over a private DevTools pipe, not a network port, and enforces these rules: only
  `http://127.0.0.1:<port>` may load in it, any other http(s) link opens in your default browser, other
  schemes are dropped, and popups are closed. Google sign-in therefore finishes in your default browser.
  If Chrome isn't installed, the desk opens in a locked-down Electron window instead (context isolation
  and sandbox on, no Node, same rules, microphone for the local page only). Typing and speech output work
  there, but voice input doesn't.
- **Fullscreen**: `"fullscreen_on_start": true` in config.json (the default) opens the desk fullscreen.
  Esc leaves fullscreen and F11 toggles it.
- **Brain**: the Claude Code CLI already on this computer, signed in with your subscription. Nothing is
  bundled: no CLI, no credentials, no API client. At startup the app finds `claude` (on PATH, the PATH
  in the registry, or the usual install folders, so a Start-menu launch finds it too) and runs
  `claude auth status`. If it isn't installed or signed in, the app shows the setup steps and a Retry button.
- **Your data** lives in `%APPDATA%\Jarvis\home`, never in the install folder, so reinstalling, updating
  or uninstalling never touches it. That covers config.json, .env, secrets/, data/, notes/, and
  internship_apply/ (config.local.yaml, data/profile.yaml, listings.db, output/, logs/). On first run it's
  created from the committed templates, and an existing file is never overwritten.
  `npm run migrate-data` copies your existing files from the repo into it. It only replaces files
  that are still untouched templates. App logs are in `%APPDATA%\Jarvis\desktop.log` and
  `%APPDATA%\Jarvis\home\data\jarvis.log`.
- **Python** is bundled: the installer ships the backend frozen with PyInstaller (Python and the
  internship packages included), so the installed app needs no Python. **Playwright** needs no browser
  download either: the form filler drives your installed Chrome (`channel="chrome"`), and the driver is
  bundled. `jarvis-backend.exe --self-test` (in the install folder's `resources\backend`) checks Claude,
  the internship safety switches and Playwright with Chrome.
- **Safety rules are the same code**: the internship kill switch, dry_run, manual_only_domains, call limits
  and the no-auto-submit rules come from the same `config.yaml` defaults. Your `config.local.yaml`
  overrides it as before, and a test checks the defaults are identical inside the app.
- The installer is **not code-signed**, so Windows SmartScreen will warn on first install: choose
  "More info", then "Run anyway".

One-time setup to build it: Node.js, then

```powershell
cd desktop
npm install
python -m pip install -r requirements-build.txt    # PyInstaller (build only)
```

| Command (in `desktop/`) | What it does |
|---|---|
| `npm run dev` | Runs the app from the repo source with your Python, so edits show up on the next launch. Uses the repo's data, like the web version. If the web version is already running, it opens the desk on it. Set `JARVIS_HOME` to use another data folder. |
| `npm run dist` | Freezes the backend (PyInstaller), builds `desktop/dist/Jarvis-Setup-<version>.exe` (electron-builder, NSIS) and runs the build check. |
| `npm run check:build` | Lists everything inside the newest installer (full list: `dist/Jarvis-Setup-<version>.exe.contents.txt`) and fails on .env, config.json, profile.yaml, config.local.yaml, any .db, output, logs, secrets, or key/token patterns. |
| `npm test` | The desktop tests. `$env:JARVIS_UI_TESTS=1; npm test` also runs the visible fullscreen/Esc test. |
| `npm run migrate-data` | Copies your repo data into `%APPDATA%\Jarvis\home` (see above). |

## Updating Jarvis

1. **Edit with Claude Code in the repo** (`C:\Users\<you>\jarvis-brain`), as with every other step.
2. **Test with `npm run dev`** in `desktop/`: it runs the code you just changed. Also run `npm test`
   there, `python -m unittest discover -s internship_apply/tests -t .` and `python preflight.py` with the
   web version running.
3. **Bump the version** in `desktop/package.json` (`"version": "1.0.1"`; the installer is named after it,
   and `GET /health` reports it).
4. **Build with `npm run dist`**. It fails if the build check finds anything personal or secret in the installer.
5. **Reinstall**: close Jarvis and run `desktop\dist\Jarvis-Setup-<version>.exe`. It installs over the old
   version, and your data in `%APPDATA%\Jarvis\home` stays as it is.

TODO (not built): auto-update. electron-builder can publish to GitHub Releases, and `electron-updater`
could install new versions on its own. That needs a release workflow and, realistically, code signing.

## Using this as a template

Everything personal lives in git-ignored files. The repo only ships templates where every personal
field is `TODO`, and a `TODO` value is treated as not set (Jarvis says what to fill in instead of
using it).

1. Copy the example files and fill in **your copies**:

   | Template (committed)                        | Your copy (git-ignored)                      |
   |---------------------------------------------|----------------------------------------------|
   | `config.example.json`                       | `config.json`: Google OAuth client id/secret, `workspace_root` (the folder Jarvis's file tools work in) |
   | `internship_apply/profile.example.yaml`     | `internship_apply/data/profile.yaml`         |
   | `internship_apply/config.yaml` (defaults)   | `internship_apply/config.local.yaml`: only the settings you change (`target_roles`, `locations`, boards...); it overrides `config.yaml` |
   | `internship_apply/manual_urls.example.txt`  | `internship_apply/data/manual_urls.txt`      |
   | `.env.example`                              | nothing to copy: Jarvis reads no secrets from the environment |

   ```powershell
   Copy-Item config.example.json config.json
   Copy-Item internship_apply\profile.example.yaml internship_apply\data\profile.yaml
   Copy-Item internship_apply\manual_urls.example.txt internship_apply\data\manual_urls.txt
   ```

2. **Never commit them.** `.gitignore` already covers them, plus `data/`, `secrets/`, `listings.db`,
   `internship_apply/output/` and `logs/`, virtualenvs and build folders.
3. Turn on the leak check: list your own personal strings (name, email, phone, school, employers,
   Windows username...) one per line in `personal_strings.local.txt` (git-ignored), then enable the hook:

   ```powershell
   git config core.hooksPath scripts/git-hooks
   python scripts/check_no_personal_data.py      # scan everything git tracks
   ```

   The pre-commit hook refuses a commit that adds any of your strings, a real-looking email address
   or phone number, an API key or token, or a home-folder path such as `C:\Users\<name>`. Tests and
   fixtures use obviously fake data (Jane Doe, jane@example.com, 555-0100). `preflight.py` runs the
   same scan.
