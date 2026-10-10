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
- See [internship_apply/README.md](internship_apply/README.md) for the internship module.

Requirements: Windows, Python 3.11+, Claude Code signed in (`claude` on PATH), Google Chrome, and
`pip install -r requirements.txt` for `internship_apply`.

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
