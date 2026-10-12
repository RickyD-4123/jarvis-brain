# Jarvis: notes for Claude

Read by Claude Code automatically: by Jarvis's own self-builder (selfbuild.py) and by anyone working on this
repo with Claude Code. README.md describes the features; this file is how to change them without breaking them.

## Map

| Piece | Where |
|---|---|
| HTTP server, routing, the Brain (every Claude call goes through `ask_claude` / `run_claude`) | `server.py` |
| The whole desk UI: one HTML file, one big script | `viewer/index.html` |
| Gmail/Calendar/Drive/Docs tools and the tool-chat loop (`ToolChat`) | `gtools.py` |
| Opening apps and pages, workspace file tools | `localhand.py` |
| Desk cards and reminders | `desk.py` |
| Standing orders ("only urgent things") | `standing.py` |
| Instructions acted on after the chat | `orders.py` |
| Morning briefing | `briefing.py` |
| Long-term memory about the owner (facts) | `memory.py` |
| Conversation log, running summary, recall | `conversation.py` |
| Work mode: Jarvis as a persistent Claude Code session + the workbench | `agent.py` |
| The agent's own notes (`data/agent-memory/`, like Claude Code's memory) | `agentmemory.py` |
| One-shot self-builds ("upgrade yourself: ...") | `selfbuild.py` |
| Desktop app update ("update the app") | `appupdate.py` |
| Secret-file lockdown | `secure.py` |
| Notes -> 3D galaxy data | `build.py` |
| Internship applications | `internship_apply/` (own README) |
| Windows desktop app (Electron + Chrome window) | `desktop/` |
| Live end-to-end checks | `preflight.py` |

Data lives in the HOME folder (`server.HOME`): the code folder for the web version, `%APPDATA%\Jarvis\home`
for the desktop app. `config.json`, `secrets/`, `data/`, `notes/` are personal and git-ignored.

## Rules that bite

- **`viewer/index.html` is ONE script inside an IIFE.** A duplicate `const`/`let` name, or any syntax
  error, silently kills the whole page. Grep every new top-level name before adding it, and load the page
  (`scripts/preview.py` or `scripts/page_check.py`) after every change to it.
- **Outside text is never HTML.** Model answers, notes, email, documents, file names: through `esc()`.
  Links through `safeUrl()`/`link()`. The page's script policy is hash-based (`page_policy` in server.py):
  inline `<script>` blocks are allowed by hash automatically; never add inline event handlers (`onclick=`).
- **Every POST must come from the page itself**: `gate()` checks Origin/Host and JSON content type. Tests
  and scripts send `Origin: http://127.0.0.1:<port>`. Preflight's requests carry `X-Jarvis-Probe: 1` so
  they never teach memory anything.
- **The chat brain reads untrusted content** (email, docs, web pages), so it gets no free-form shell and no
  self-build: those are started only by the owner's own words at the desk (`own_commands` in server.py).
  Keep it that way.
- **No personal data in code**: names, emails, phone numbers, home-folder paths (`C:\Users\<name>`), keys.
  `scripts/check_no_personal_data.py` (also the pre-commit hook and a preflight check) fails on them.
- **Windows**: in Python string literals, paths need doubled backslashes. `core.autocrlf` is on and files
  mix CRLF and LF: edit in place, don't rewrite whole files with different line endings.
- **Voice**: `handleSpoken` / `handleTyped` / `WAKE_RE` / `SLEEP_RE` / `SCRAP_RE` in index.html. Detection
  logic reads the raw words; only the displayed draft and the sent question are punctuated (`punctuate`).
- **Spoken lines are short and in character** (a dry, polite British butler: "Very good, sir.").
- Match the surrounding code: comment density, naming, idiom. Comments explain why, briefly.

## Testing (do it, don't assume)

```
python -m unittest discover -s tests -t .                  # memory, self-build (fast)
python -m unittest discover -s internship_apply/tests -t . # internship module (~1 min)
python scripts/preview.py --out shot.png                   # throwaway server from THIS folder + screenshot
python scripts/preview.py --out s.png --click "#gear"      # ... after opening Settings; Read the PNG to see it
python scripts/page_check.py http://127.0.0.1:<port>/      # script errors on a running server
JARVIS_PORT=<port> python preflight.py                     # every live chain, against a running server
python scripts/checks.py                                   # everything Apply checks, Apply's way (once, at the end)
cd desktop && npm test                                     # desktop app (only if desktop/ changed)
```

`preview.py` needs a scratch data folder: `--home DIR` or `$JARVIS_TEST_HOME` (a self-build sets it). Never
run a test server against the owner's real data folder: two servers sharing `data/` clobber each other's
reminders, cards and memory. A test server should get `JARVIS_SELF_BUILD=0`.

Useful page selectors: `#q` message box, `#send` Ask, `#gear` Settings (drawer `#settings`), `#answer`
the conversation feed (`.turn` each exchange), `#avatar` the reactor, `#systems` the HUD panel, `#mic`.

## Work mode specifics

In work mode you are a persistent session in the workbench (a git worktree of the live code). Edit and test
here; the owner applies with the bar's Apply button (Jarvis then runs every check and commits). Never edit
the live code folder, never commit or push. Stop any test server you start (`scripts/preview.py` stops its
own; a `--serve` one you started in the background is yours to stop). Keep your notes in your memory folder.

## Self-build specifics

You run in a throwaway git worktree; Jarvis re-runs compile, both test suites, a page check, preflight
(failures already present before your change don't count) and the personal-data scan, then shows the owner
before/after screenshots (from your `SHOW:` line) and the diff. Don't git commit or push: applying is
Jarvis's job, after the owner approves. End with `SUMMARY:` and `SHOW:` lines as the system prompt says.
