# MESA Adaptive Assessment

An exam platform with rule-based adaptive difficulty, server-authoritative
state, interruption-proof resume, an admin dashboard with question upload, and
browser-level proctoring. **Python 3.8+ standard library only — nothing to
install.**

---

## Run it

```
cp .env.example .env             # set ADMIN_KEY before any real use
python3 seed.py --fresh          # or: --students 120 for a full cohort
python3 server.py
```

The server prints three URLs; the second is how students on the same wifi
reach your laptop.

Demo logins: `chandu`, `student001`… · password `mesa-demo-2026`.

---

## The student flow

Four screens, one job each.

**Sign in → Instructions → Questions → Done.** If they were interrupted, a
**Welcome back** screen appears instead of Instructions, showing their exact
progress and remaining time.

The question screen shows a progress rail (position only, never level), a
clock that turns red under five minutes, the question, and four options. Keys
1–4 select, Enter submits. Submit is disabled until an option is chosen and
disables on submit, so a double-click cannot double-post.

Students never receive — in any byte of any page — correct answers, difficulty
labels, topics, explanations, their score, or future questions. A test strips
the CSS and JavaScript and scans the remaining markup for all of those on
every student screen.

---

## The admin dashboard

`http://localhost:8000/admin?key=…` — four tabs plus CSV export.

**Students** — add new students, reset their passwords, deactivate accounts
(they can't sign in anymore). Each student row shows how many exam attempts
they've started. Passwords are randomly generated; when reset, they're shown
once in a modal with a copy-to-clipboard button.

**Overview** — live counters (started, in progress, finished, cohort accuracy,
how many left the exam window) above the attempts table, with time remaining
for anyone still sitting the exam and a flag count for window switches.

**Leaderboard** — ranked by correct answers, then completion, then finishing
time, with per-student accuracy. Admin-only.

**Questions** — the question manager:

- *Add a question* — a form; it enters the pool immediately.
- *Upload a batch* — CSV or JSON, by file or paste. Every row is validated
  before anything is written: one bad row rejects the whole file and names the
  line. Download the CSV template to see the exact columns.
- *Retire / Restore* — retiring stops a question being served without deleting
  it, so past results stay intact.
- Filters by level, topic and status; per-question serve count and % correct.
- *Export bank* — the whole bank as JSON.

**CSV template columns:** `id` (blank to auto-number), `difficulty`, `topic`
(use `finance|business` for several), `prompt`, `option1`–`option4`, `answer`
(1–4, human numbering), `explanation` (admin-only, never shown to students).

Clicking any student opens their attempt detail: every question, the state
before it, and the promotion/demotion decision — this is how you prove the
adaptive engine is working.

---

## Verify it yourself

**Adaptive movement** — sign in, answer 3 correctly, then open the attempt in
admin: row 3's decision reads *promote easy->medium*. Answer 2 wrong →
*demote medium->easy*. Reach hard (3 then 2 correct) and answer 1 wrong →
*demote hard->medium*. None of this was visible to the student.

**Resume** — mid-exam, quit the browser entirely. Reopen, sign in: "Welcome
back", same question, clock still counting from where it really is.

**No repeats** — in the attempt detail, no question ID appears twice. The
database enforces this with `UNIQUE(attempt_id, question_id)`.

**Focus tracking** — switch tabs a few times mid-exam; the count appears in
the attempt detail and as a flag on the overview.

---

## 120 students at once

```
Terminal 1:  python3 seed.py --fresh --students 120 && python3 server.py
Terminal 2:  python3 loadtest.py 120
```

Measured on one laptop, all 120 answering all 30 questions with no think time:

```
completed 30 q  120/120   errors 0   repeated question 0
latency  median 312 ms · p95 451 ms · max 1738 ms     verdict: PASS
```

That test found a real bug: the stdlib default TCP backlog of 5 dropped
connections when the whole cohort signed in at once. `ExamServer` now uses 256.

---

## Browsers

Chrome, Safari, Firefox, Edge — desktop and mobile. Plain HTML forms and CSS:
no framework, no build step, no web fonts, no browser storage. JavaScript only
enhances (live clock, keyboard shortcuts, selection highlight, focus logging);
with JavaScript off the exam still completes, and a test proves it. Responsive
to phone width, 48px tap targets, visible keyboard focus, reduced motion
respected.

---

## Safe Exam Browser

**SEB cannot be installed by this or any web platform** — it needs admin
rights on the machine. Moodle, TAO and commercial platforms have the same
constraint. Options:

| Situation | Approach |
|---|---|
| Lab / college machines | IT pre-installs SEB once; `SEB_MODE=enforce` |
| Students' own laptops | Skip SEB; `PROCTOR_FOCUS=on` (default) — no install, works everywhere |
| High stakes, own laptops | Send the installer a day early with a setup window |

Implemented and tested here: `.seb` config generation (`/seb/config`),
verification of SEB's `X-SafeExamBrowser-RequestHash`, three modes (`off` /
`detect` / `enforce`), and per-answer SEB stamping in admin. With a Browser
Exam Key in `.env`, a normal browser gets a 403 and a faked SEB user-agent is
still rejected. Not verified here: the SEB desktop app locking a machine.

---

## Tests

```
python3 -m unittest -v     # 69 tests
python3 simulate.py        # 5 student archetypes through the real engine
python3 loadtest.py 120    # cohort load (server must be running)
```

Adaptive transitions and boundaries · selection and fallback ladder · database
non-repetition · HTTP security (wrong question, replay, expiry) · SEB config
format and hashes · session persistence and resume · the no-results guarantee ·
no-JavaScript fallback · focus events · leaderboard ranking and access control ·
concurrent students · question add, upload, validation, retire, templates ·
student add, password reset, deactivation and re-activation, access control.

---

## Push to git

A `.gitignore` is included: the database, `.env`, and caches are never
committed — so student answers and your admin key stay off GitHub.

```
cd mesa_poc
git init
git add .
git commit -m "MESA adaptive assessment platform"
```

Then create an empty repo on github.com (no README, no .gitignore) and:

```
git remote add origin https://github.com/<you>/<repo>.git
git branch -M main
git push -u origin main
```

Check `git status` before the first commit — `mesa.db` and `.env` should not
be listed. Later changes: `git add -A && git commit -m "…" && git push`.

---

## Architecture

| File | Role |
|---|---|
| `adaptive_engine.py` | Pure engine: `initial_state`, `record_answer`, `select_next`. No I/O. |
| `config.json` | Thresholds and topic blueprint. **The only file to edit to change adaptive behaviour.** |
| `questions.json` | Seed bank: 90 questions across seven topics; MCQ, image and dataset types. |
| `seed.py` | Schema and seeding; `UNIQUE(attempt_id, question_id)` guarantees non-repetition. |
| `server.py` | All screens and state. The server owns the current question, clock and score. |
| `seb_support.py` | `.seb` config generation and SEB header verification. |
| `loadtest.py` | Cohort load test. |

Changing adaptive rules: edit `config.json`, restart. No reseed needed.

---

## Known limitations

- **One attempt per student.** Re-testing needs `python3 seed.py --fresh`.
- **Image and dataset questions** are seeded from `questions.json`; the admin
  form creates text MCQs.
- **Plain HTTP on a LAN** — fine for a supervised classroom, needs HTTPS for
  internet-facing use.
- **POC-grade security**: no CSRF tokens, no rate limiting, SHA-256+salt
  hashing. The admin key travels in the URL — don't screen-share it.
- **Focus tracking is a deterrent**, not lockdown: it records that a student
  left the window; it cannot stop them.
- **SQLite.** Verified to 120 concurrent; beyond a few hundred, move to Postgres.
- TAO Community Edition integration remains parked (`PHASE2_RUNBOOK.md`), not
  disproven.
