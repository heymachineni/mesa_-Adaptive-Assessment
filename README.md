# MESA Adaptive Assessment

An exam platform with rule-based adaptive difficulty, server-authoritative
state, interruption-proof resume, an admin dashboard with question upload, and
browser-level proctoring. **Python 3.9+ standard library only — nothing to
install.**

---

## Run it

```
cp .env.example .env             # optional: overrides for passwords
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

The question screen shows how many questions in they are, a clock that turns
red under five minutes, the question, and four options. Keys 1–4 select, Enter
submits. Submit is disabled until an option is chosen and disables on submit,
so a double-click cannot double-post.

By default the exam has **no fixed length** — students answer as many as they
can before the clock runs out, and are never shown a target number to hit.
Give `maxQuestions` a number in `config.json` and the progress rail and
"Question 3 of 20" counters come back. See **Changing the adaptive rules**.

Students never receive — in any byte of any page — correct answers, difficulty
labels, topics, explanations, their score, or future questions. A test strips
the CSS and JavaScript and scans the remaining markup for all of those on
every student screen.

---

## The admin dashboard

`http://localhost:8000/admin/login` — sign in as an admin. Four tabs plus CSV
export.

**Admin accounts** are the `ADMINS` list in `seed.py`:

```python
ADMINS = [
    ("chandu", "Chandu", "ChanduMesa"),
    ("ankur",  "Ankur",  "AnkurMesa"),
]
```

Edit that list to add, remove or repassword an admin, then re-seed
(`python3 seed.py --fresh`). On a host that rebuilds the database on each cold
start, a redeploy is enough. The list is authoritative: re-seeding applies a
changed password and deletes anyone taken out of it.

Passwords are stored salted and SHA-256 hashed, never in plain text, and the
session is an `HttpOnly` cookie that expires after 8 hours — separate from a
student's exam session, so signing in as a student grants nothing here.

> **These passwords sit in the repository.** Anyone who can read the repo can
> sign in. To keep one out of git, leave a placeholder in the list and set
> `ADMIN_PASSWORD_<USERNAME>` (uppercase) in the environment instead — that
> value wins, and `server.py` warns on startup about any password still coming
> from the file.

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

## Design

The interface follows Apple's Human Interface Guidelines, adapted to MESA's
deep green. Three ideas do most of the work:

**Clarity.** Type is the SF scale with optical tracking — larger text gets
tighter letter-spacing, the way SF Pro Display does. Spacing is a 4pt grid.
Numbers use tabular figures so a running clock doesn't jitter.

**Deference.** The question is the interface. On the exam screen there is no
navigation, no logo bar competing for attention, no decoration — a progress
rail, a clock, the question, four options. Chrome recedes so the content
doesn't have to fight it.

**Depth.** Layered surfaces and hairline separators carry hierarchy instead of
heavy borders or drop shadows.

Colour is semantic rather than literal (`--label-2`, `--separator`,
`--surface`), so **dark mode is a token swap, not a second stylesheet**. Both
appearances ship. Controls clear 44pt, focus rings are always visible for
keyboard users, and `prefers-reduced-motion` removes every transition.

Copy is written the way Apple writes it: second person, active voice, specific
over vague. The instructions screen answers what a student would actually
worry about — *will I lose my work, why is my paper different from my
friend's, what exactly is being recorded* — instead of listing rules. Status
values are translated for humans (`completed` renders as "Finished"); raw
database enums never reach the screen.

---

## Environment variables

Nothing here is required to boot — every value has a default, which is exactly
why the two credentials below must be changed before anyone real logs in.
Locally these live in `.env` (copy `.env.example`, never committed). On a host
like Vercel you set them in the dashboard instead.

| Variable | Default if unset | What it does |
|---|---|---|
| `DEFAULT_STUDENT_PASSWORD` | `mesa-demo-2026` | Password given to every account created by `seed.py`. **Change this**, then re-seed. |
| `ADMIN_PASSWORD_<USERNAME>` | the `ADMINS` list in `seed.py` | Overrides one admin's password, so a real one need not be committed. |
| `DB_DIR` | folder containing `seed.py` | Where `mesa.db` is written. Point it at a mounted volume in production; `api/index.py` sets it to `/tmp` on Vercel. |
| `PORT` | `8000` | Local listen port. Ignored on serverless. |
| `PROCTOR_FOCUS` | `on` | Logs when a student leaves the exam window. A deterrent and audit trail, not lockdown. |
| `MESA_DEBUG` | unset | Serves deploy tracebacks in the response body. For debugging a host only — leave off in production. |

`server.py` warns on startup about any admin password still coming from
`seed.py` rather than the environment.

**A variable that exists but is blank counts as unset.** `os.environ.get()`
only falls back when a key is *absent*, so a dashboard variable created with
an empty value returns `""` — which is how a blank `PORT` became `int("")`
and killed a Vercel deploy at import. Every read goes through `seed.env`,
`env_int` and `env_flag`, which treat blank as missing, so a stray empty
variable can't take the app down or silently blank out a password.

---

## Deploying to Vercel

```
vercel deploy --prod        # or just push to the connected GitHub repo
```

`api/index.py` is the entrypoint. Vercel's Python runtime looks for a class
named `handler` that subclasses `BaseHTTPRequestHandler`, which is what
`server.Handler` already is, so it re-exports that class and `vercel.json`
routes every path to it. Without those two files Vercel finds no framework
and no `index.html`, treats the repo as a static site with nothing to serve,
and answers **404 on every route**.

`vercel.json` uses zero-config — `functions` for the bundle and `rewrites`
for routing:

```json
{ "functions": { "api/index.py": { "includeFiles": "**" } },
  "rewrites": [{ "source": "/(.*)", "destination": "/api/index" }] }
```

`includeFiles` keeps `questions.json`, `config.json` and `assets/` in the
bundle. `rewrites` passes the original URL through, so the server still sees
`/exam` rather than `/api/index`.

**Don't switch this to `builds`.** It looks like the more explicit option and
it isn't: `builds` turns zero-config off, silently ignores `rewrites`, and its
`routes` rewrite the request path instead of passing the original URL — which
served a platform 404 on every route when it was tried. Two deploy failures
that looked like config problems (*"the pattern `api/index.py` … doesn't match
any Serverless Functions"* and *"Could not find a top-level app, application,
or handler"*) were both the same bug: `handler` had been nested inside an
`if/else` in `api/index.py`, so Vercel's build-time scan couldn't see it, and
a file with no visible entrypoint is not a Serverless Function. Fixing the
entrypoint fixed both. `test_deploy.py` now guards the entrypoint and rejects
mixed routing shapes.

Set these in **Project → Settings → Environment Variables**:

| Variable | Why |
| --- | --- |
| `ADMIN_PASSWORD_CHANDU`, `ADMIN_PASSWORD_ANKUR` | Keeps the real admin passwords out of the repo. |
| `DEFAULT_STUDENT_PASSWORD` | Seeded student password. |
| `DB_DIR` | Optional; already defaults to `/tmp`, the only writable path. |

Runs on any Python from 3.9 up, so whichever runtime Vercel picks will work.
(It didn't always: `server.py` had a backslash inside an f-string expression,
which is a syntax error before 3.12 and crashed the function with
`FUNCTION_INVOCATION_FAILED` on Vercel's older runtime. Fixed.)

`handler` must stay at the **top level** of `api/index.py`. Vercel finds it by
parsing the file at build time and walking only the module's direct children,
so a class defined inside an `if` or `try` is invisible to it and the build
fails with *"Could not find a top-level app, application, or handler"*.
`test_deploy.py` asserts this, along with the Python 3.9 syntax rule and the
`vercel.json` wiring — all three are mistakes that only surface after a push.

**If a deploy fails**, the function reports why instead of dying silently. The
traceback always goes to **Runtime Logs**; set `MESA_DEBUG=1` to also see it
in the response body. Turn that back off afterwards — tracebacks leak paths
and config.

> **Read this before running a real exam on Vercel.** Serverless gives each
> instance its own ephemeral `/tmp`, so the SQLite database is created and
> seeded fresh on every cold start and is **not shared between concurrent
> instances**. In practice: a student can be bounced to a new instance
> mid-exam and land on an empty database, and two students can be served by
> two different databases at once. Vercel is fine for a demo or a link you
> want to show someone; the design intent of this project — all state on the
> server, every interruption resumes exactly where you left off — only holds
> on a single long-running process (`python3 server.py` on a VM or container)
> or after moving storage to a networked database such as Postgres. See
> **Known limitations**.

---

## Browsers

Chrome, Safari, Firefox, Edge — desktop and mobile. Plain HTML forms and CSS:
no framework, no build step, no web fonts, no browser storage. JavaScript only
enhances (live clock, keyboard shortcuts, selection highlight, focus logging);
with JavaScript off the exam still completes, and a test proves it. Responsive
to phone width, 48px tap targets, visible keyboard focus, reduced motion
respected.

---

## Tests

```
python3 -m unittest -v     # 87 tests
python3 simulate.py        # 5 student archetypes through the real engine
python3 loadtest.py 120    # cohort load (server must be running)
```

Adaptive transitions and boundaries · selection and fallback ladder · database
non-repetition · HTTP security (wrong question, replay, expiry) · session
persistence and resume · the no-results guarantee ·
no-JavaScript fallback · focus events · leaderboard ranking and access control ·
concurrent students · question add, upload, validation, retire, templates ·
student add, password reset, deactivation and re-activation, access control ·
a four-level ladder built from config alone · the legacy config shape ·
capped vs open-ended exams · and the deployment guards in `test_deploy.py`.

---

## Push to git, and deploy

A `.gitignore` is included: the database, `.env`, and caches are never
committed — so student answers and your `.env` stay off GitHub.

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

**Hosting.** `api/index.py` + `vercel.json` make this run on Vercel — see
**Deploying to Vercel** above. That is the right choice for a demo or a link
you want to show someone, but Vercel's storage is ephemeral, so exam attempts
do not survive a cold start. For a real exam the app needs a persistent
process with a disk — a VM, a container, or a host like Railway, Render or
Fly.io — with `DB_DIR` pointed at a mounted volume and real admin passwords.

---

## Architecture

| File | Role |
|---|---|
| `adaptive_engine.py` | Pure engine: `initial_state`, `record_answer`, `select_next`. No I/O. |
| `config.json` | The difficulty ladder, exam length and topic blueprint. **The only file to edit to change adaptive behaviour**, including adding levels. |
| `questions.json` | Seed bank: 90 questions across seven topics; MCQ, image and dataset types. |
| `seed.py` | Schema and seeding; `UNIQUE(attempt_id, question_id)` guarantees non-repetition. |
| `server.py` | All screens and state. The server owns the current question, clock and score. |
| `loadtest.py` | Cohort load test. |

---

## Changing the adaptive rules

**Levels are data, not code.** The ladder lives in `config.json`, easiest
first, and everything else derives from it — the state machine, the selection
fallback, the SQLite `CHECK` constraint, the admin dropdowns and filters, the
CSV upload validator.

```json
"adaptive": {
  "startingLevel": "easy",
  "levels": [
    { "name": "easy",   "promoteAfterCorrect": 3 },
    { "name": "medium", "promoteAfterCorrect": 2, "demoteAfterWrong": 2 },
    { "name": "hard",                             "demoteAfterWrong": 1 }
  ]
}
```

`promoteAfterCorrect` is how many right answers in a row move a student up one
rung; `demoteAfterWrong` is how many wrong answers move them down one. Both
counters reset on any level change, and a right answer clears the wrong streak
(and vice versa). Omitting a key pins that direction — a level with no
`promoteAfterCorrect` is never promoted out of, however long the streak runs.
The key is ignored on the top level going up and the bottom level going down,
because there is nowhere to go.

**To add a fourth level**, add it to the list and give some questions that
difficulty:

```json
{ "name": "hard",   "promoteAfterCorrect": 2, "demoteAfterWrong": 1 },
{ "name": "expert",                           "demoteAfterWrong": 1 }
```

Then `python3 seed.py --fresh` and restart. Re-seeding is required because the
`difficulty` column's `CHECK` constraint is generated from this list — an
existing database keeps its old constraint and will reject the new value.
There is nothing else to change. Levels can be renamed or reordered the same
way; names are arbitrary strings, so `foundation / core / stretch` works just
as well as `easy / medium / hard`.

Rule changes that don't touch the level names — different thresholds, a
different `startingLevel` — just need a restart, no reseed.

Configs written before levels were data (`startingDifficulty` plus the four
`easyToMediumCorrectThreshold`-style keys) are still read, so an old
`config.json` keeps working unchanged.

### Exam length

`"maxQuestions": null` means the exam has **no fixed length**: students answer
as many as they can until the clock runs out or the bank is exhausted, and no
screen ever tells them a target number. Set it to a number to cap the exam
instead — the progress rail and "Question 3 of 20" counters come back
automatically.

Topic quotas stay proportional either way. With no fixed length they are
measured against the whole question bank, so the blueprint weights hold at any
exam length.

---

## Known limitations

- **One attempt per student.** Re-testing needs `python3 seed.py --fresh`.
- **Image and dataset questions** are seeded from `questions.json`; the admin
  form creates text MCQs.
- **Plain HTTP on a LAN** — fine for a supervised classroom, needs HTTPS for
  internet-facing use.
- **POC-grade security**: no CSRF tokens, no rate limiting, SHA-256+salt
  hashing. Admin passwords live in `seed.py`, which is in git, unless
  overridden by `ADMIN_PASSWORD_<USERNAME>`.
- **Focus tracking is a deterrent**, not lockdown: it records that a student
  left the window; it cannot stop them.
- **SQLite.** Verified to 120 concurrent; beyond a few hundred, move to Postgres.
- **Vercel is demo-only.** Serverless `/tmp` is ephemeral and per-instance, so
  attempts do not survive a cold start and concurrent students may hit
  different databases. Run a persistent process for a real exam.
- TAO Community Edition integration remains parked (`PHASE2_RUNBOOK.md`), not
  disproven.
