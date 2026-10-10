# internship_apply

Jarvis finds internship listings, scores them against your profile, drafts tailored materials, fills
application forms up to (never past) the submit button, and tracks every application.

## Setup

1. `pip install -r requirements.txt` (PyYAML, python-docx, fpdf2, Playwright; Playwright uses your installed Chrome,
   so there is no browser download).
2. Copy `internship_apply/profile.example.yaml` to `internship_apply/data/profile.yaml` and fill in your copy.
   Every `TODO:` is a placeholder; the 11 required fields are marked.
   Leave `self_identification` on `decline` unless you want a specific answer chosen.
3. Create `internship_apply/config.local.yaml` (git-ignored; it overrides `config.yaml`, which stays neutral with
   `TODO` values) with your `target_roles`, `locations`, `remote_preference`, and the boards to read
   (`greenhouse_boards`, `lever_companies`: the last part of the board's link; `[]` for none). Links from anywhere
   else go in `data/manual_urls.txt` (start from `manual_urls.example.txt`), one per line. Add any career site
   that needs an account to `manual_only_domains`. A setting still on `TODO` counts as not set, and Jarvis prints
   which ones to fill in.
4. Check it all: `python -m unittest discover -s internship_apply/tests -t .` (headless, no network, ~1 minute).

Your data stays local and git-ignored: `config.local.yaml`, `data/` (profile, database, links), `output/`
(materials), `logs/` (redacted logs).

## First real run: flip one switch at a time

The switches start in their safest position. Change ONE, try it, check the result, then the next:

| Step | `dry_run` | `filling_enabled` | What happens | Check |
|---|---|---|---|---|
| 1 | true | **false** | Find, score and prepare work; any fill is refused as manual_required | listings, scores and `output/` folders look right; read a `fact_check.md` |
| 2 | true | **true** | Filling opens the form and *maps* it, typing nothing | `field_mapping.json`: every field matched to the right answer |
| 3 | **false** | true | Filling types into the form and stops for your review | the open browser, `review.png`, `application_record.json`; press Submit yourself |
| 4 | (optional) `allow_approved_submit: true` | | The terminal-approval route exists | only if you want it; most people never need it |

To stop everything at once, set `filling_enabled: false`. It is re-checked before every field, so a fill in
progress stops too.

## Daily use

1. **Find:** "Jarvis, any new internships?" (or `python -m internship_apply.listings` then `python -m internship_apply.scoring`).
2. **Prepare** the ones you like: "prepare the Initech one". Read `fact_check.md` in its `output/` folder and fix
   anything flagged in your profile, not in the generated files.
3. **Fill**: "fill in the Initech form". The browser opens, fills, and waits. Look at every field, especially
   the ones listed as needing you, then press Submit yourself.
4. **Record it**: "mark the Initech one submitted by me" then "yes, submitted by me"
   (or `python -m internship_apply.tracker submitted <id>`).
5. **Weekly**: "how are my applications doing?" for counts and deadlines; `tracker followups` for anything quiet
   for 14 days; `tracker export` for a spreadsheet.

At most `max_applications_per_day` (5) forms are filled a day, and the same role is never filled twice.

### Claude usage limits

Every Claude call goes through Jarvis's Claude Code brain on your subscription: no API key, nothing
pay-per-use. Each call is counted (in `data/llm_calls.db`) before it is made:

- `max_llm_calls_per_run` (20): one scoring pass, one set of materials, or one request to Jarvis.
- `max_llm_calls_per_day` (60): all internship work together, per calendar day.

Scoring always runs the rules-only pass on every listing first; only listings scoring at least
`model_prescore_threshold` (50) go to Claude, best first. When a cap is reached Jarvis stops asking Claude,
tells you which cap, and finishes by the rules (scores keep their rules-only value; materials are written
from your own words, and are only written with Claude if there are 5 calls left for the whole set).

## The steps (also usable from a terminal)

| Step | Command | What it does |
|---|---|---|
| Find | `python -m internship_apply.listings` | Greenhouse/Lever public APIs + your pasted links; robots.txt respected |
| Score | `python -m internship_apply.scoring [--model]` | 0-100 fit, hard blockers skip a listing; `--model` adds Claude (capped) |
| Materials | `python -m internship_apply.materials <id>` | resume, cover letter, answers, fact-check report |
| Fill | `python -m internship_apply.form_filler fill <id>` | fills the form in a visible browser, stops before submit |
| Track | `python -m internship_apply.tracker queue` | review queue; `approve`, `edit`, `reject`, `submitted`, `export` |

## Jarvis tools

Registered in `gtools.TOOLS` from `internship_apply/tools.py` and run by the same tool loop as the Google and
computer tools.

| Tool | Changes anything? |
|---|---|
| `internships_find` | yes: fetches and scores listings |
| `internships_prepare(listing_id)` | yes: writes materials |
| `internships_fill(listing_id)` | yes: fills a form in your browser |
| `internships_queue` | no |
| `internships_mark(listing_id, status, note)` | yes: changes a status |
| `internships_summary` | no |

Rules, enforced in code (not only in the prompt):

- **Confirmation.** A tool that changes anything never runs on the first call. It returns what it would do;
  Jarvis tells you and asks. It runs only if your *next* message is a yes and Jarvis repeats the exact call.
- **Statuses only you set.** `internships_mark` refuses `approved_for_submit` always (that is
  `tracker approve <id> --token`, in your own terminal), and `submitted_by_me` unless both your request and
  your yes contain the words "submitted by me".
- **No submitting.** No tool can reach `form_filler.submit_approved` or the approval commands (a test walks
  the call graph to prove it). Jarvis never says an application was submitted; such a sentence is struck
  from his answer and corrected.
- **Filling respects** `dry_run`, `filling_enabled`, `manual_only_domains` and `max_applications_per_day`,
  and says so plainly when it refuses.
- **Speakable.** Under 60 words, butler voice, no ids or links. Company names and job titles are scraped text:
  they are cleaned and summarised (anything that looks like an instruction becomes "one employer" /
  "an internship"), and listing descriptions are never passed to Jarvis at all.

## Example conversations

**Finding and preparing**

> **You:** Hey Jarvis, any new internships?
> **Jarvis:** I'll check three job boards and your pasted links for new internships, then score them. Shall I proceed, sir?
> **You:** Yes.
> **Jarvis:** I found eleven new listings, sir. The best matches: Software Engineer internship at Initech, 84; Data Science Intern at Acme Robotics, 78.
> **You:** Prepare the Initech one.
> **Jarvis:** I'll write a tailored resume, cover letter and answers for the Software Engineer internship at Initech, then fact-check every claim against your profile. Shall I proceed, sir?
> **You:** Go ahead.
> **Jarvis:** The materials are ready, sir. The fact check flagged one claim for you to look at before anything is used.

**Filling, and a refusal**

> **You:** Fill in the Initech form.
> **Jarvis:** Form filling is switched off in your settings, sir, so I won't touch that form. Turn on filling enabled if you'd like me to.

**Refusing to mark something submitted that you didn't say explicitly**

> **You:** Jarvis, I sent the Initech one off.
> **Jarvis:** I won't mark anything as submitted unless you say so in those words, sir. If you've submitted it yourself, please say: mark it submitted by me.
> **You:** Mark the Initech one submitted by me.
> **Jarvis:** I'll mark the Software Engineer internship at Initech as submitted by you. To confirm, please say: yes, submitted by me.
> **You:** Yes.
> **Jarvis:** I need the exact words, sir. Please say: yes, submitted by me.
> **You:** Yes, submitted by me.
> **Jarvis:** Very good, sir. The Software Engineer internship at Initech is now recorded as submitted by you.

**Never claiming a submission**

> **You:** Did you send my Acme application?
> **Jarvis:** No, sir. The form for Acme is filled and waiting in your browser for your review. Pressing submit is yours alone.
