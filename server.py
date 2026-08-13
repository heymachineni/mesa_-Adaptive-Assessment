"""MESA Adaptive Assessment — exam server (Python stdlib only).

Run:   python3 seed.py --fresh && python3 server.py
       (cohort:  python3 seed.py --fresh --students 120)

Design intent: one thing per screen, nothing on screen the student doesn't
need, no result or correct answers ever shown to the student, and every
interruption (refresh, dead battery, closed lid) resumes exactly where they
left off because ALL state lives on the server.

Server-authoritative: current question, answers, score, timer and history live
only in SQLite. The browser never receives correct answers, difficulty, topic,
explanations, or future questions.
"""
import csv
import html
import io
import json
import os
import random
import secrets
import sys
import socket
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import seed as storage
from adaptive_engine import AdaptiveEngine

BASE = os.path.dirname(os.path.abspath(__file__))


def load_env():
    path = os.path.join(BASE, ".env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()
CONFIG = json.load(open(os.path.join(BASE, "config.json")))
ENGINE = AdaptiveEngine(CONFIG)
# storage.env treats a blank environment variable as unset. A dashboard that
# creates PORT with an empty value would otherwise crash this import.
ADMIN_KEY = storage.env("ADMIN_KEY", "change-me-admin")
PORT = storage.env_int("PORT", 8000)
DURATION = CONFIG["exam"]["durationMinutes"] * 60
# None when config sets maxQuestions to null: the exam has no fixed length and
# students answer as many as they can until the clock or the bank runs out.
MAXQ = ENGINE.max_questions
LEVELS = ENGINE.names             # the difficulty ladder, straight from config
TITLE = CONFIG["exam"]["title"]
SESSION_MAX_AGE = 12 * 3600          # survives a closed browser / dead laptop

# Browser-level proctoring: log when the exam window loses focus. Works in
# every browser, needs no install. A deterrent and an audit trail — NOT
# lockdown. Students are told about it on the instructions screen.
PROCTOR_FOCUS = storage.env_flag("PROCTOR_FOCUS", True)

STYLE = """
<style>
/* ==========================================================================
   MESA design system
   Built on Apple's Human Interface Guidelines: clarity (legible type, ample
   negative space, colour that carries meaning), deference (the question is
   the interface; chrome recedes), and depth (layered surfaces, not shadows
   for decoration). Type follows the SF scale with optical tracking — larger
   text gets tighter letter-spacing, the way SF Pro Display does it.
   Spacing is a 4pt grid. Colour is semantic, never literal, so dark mode is
   a token swap rather than a second stylesheet.
   ========================================================================== */
:root{
  /* MESA brand */
  --brand:#14402e; --brand-press:#0e2f21; --brand-tint:#e8f0eb;
  --on-brand:#ffffff;

  /* surfaces, back to front */
  --bg:#f2f2f7; --surface:#ffffff; --surface-2:#f7f7fa; --surface-3:#eeeef2;

  /* labels, in descending emphasis */
  --label:#1c1c1e; --label-2:#54545a; --label-3:#8a8a8f; --label-4:#b8b8bd;

  --separator:rgba(60,60,67,.16); --separator-strong:rgba(60,60,67,.28);

  /* status */
  --red:#c4281c; --red-tint:#fdeeec;
  --green:#1c6b47; --green-tint:#e7f2ec;

  --focus:#1a6b4a;

  /* type */
  --sans:-apple-system,BlinkMacSystemFont,"SF Pro Text","SF Pro Display",
    "Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;

  /* 4pt spacing grid */
  --s1:4px; --s2:8px; --s3:12px; --s4:16px; --s5:20px; --s6:24px;
  --s7:32px; --s8:40px; --s9:48px; --s10:64px;

  /* continuous-ish corner radii */
  --r-sm:8px; --r-md:12px; --r-lg:16px; --r-xl:20px;

  --lift:0 1px 2px rgba(0,0,0,.04),0 4px 16px rgba(0,0,0,.04);
}
@media(prefers-color-scheme:dark){
  :root{
    --brand:#5cb98d; --brand-press:#7fcaa6; --brand-tint:rgba(92,185,141,.16);
    --on-brand:#04150d;
    --bg:#000000; --surface:#1c1c1e; --surface-2:#2c2c2e; --surface-3:#3a3a3c;
    --label:#ffffff; --label-2:#a1a1a8; --label-3:#7c7c81; --label-4:#5a5a5f;
    --separator:rgba(84,84,88,.6); --separator-strong:rgba(120,120,125,.7);
    --red:#ff6961; --red-tint:rgba(255,105,97,.14);
    --green:#5cb98d; --green-tint:rgba(92,185,141,.14);
    --focus:#7fcaa6;
    --lift:0 1px 2px rgba(0,0,0,.4),0 4px 16px rgba(0,0,0,.3);
  }
}

*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--label);font-family:var(--sans);
  font-size:17px;line-height:1.47;letter-spacing:-.01em;
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
  font-synthesis-weight:none}

.wrap{max-width:680px;margin:0 auto;padding:var(--s5) var(--s5) var(--s10)}
.wide-wrap{max-width:1120px}
@media(max-width:600px){.wrap{padding:var(--s4) var(--s4) var(--s9)}}

/* ---- masthead ---- */
.topbar{display:flex;align-items:center;justify-content:space-between;
  gap:var(--s4);padding:var(--s3) 0 var(--s6)}
.brand{font-weight:600;font-size:15px;letter-spacing:.18em;color:var(--label);
  text-transform:uppercase}
.who{font-size:14px;color:var(--label-3);letter-spacing:-.005em;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* ---- surfaces ---- */
.card{background:var(--surface);border:.5px solid var(--separator);
  border-radius:var(--r-xl);padding:var(--s7);box-shadow:var(--lift)}
@media(max-width:600px){.card{padding:var(--s6) var(--s5);
  border-radius:var(--r-lg)}}
.panel{background:var(--surface);border:.5px solid var(--separator);
  border-radius:var(--r-lg);padding:var(--s6);margin-bottom:var(--s5)}
.panel h3{margin:0 0 var(--s1);font-size:17px;font-weight:600;
  letter-spacing:-.02em}
.panel .sub{font-size:14px;color:var(--label-3);margin-bottom:var(--s5)}

/* ---- type scale (SF, with optical tracking) ---- */
h1{font-size:32px;line-height:1.13;margin:0 0 var(--s3);font-weight:650;
  letter-spacing:-.032em}
h2{font-size:21px;line-height:1.24;margin:var(--s7) 0 var(--s3);
  font-weight:620;letter-spacing:-.022em}
h3{font-size:17px;font-weight:600;letter-spacing:-.015em}
@media(max-width:600px){h1{font-size:28px;letter-spacing:-.028em}}
p{margin:0 0 var(--s4)}
.lede{color:var(--label-2);font-size:17px;margin-bottom:var(--s6);
  letter-spacing:-.012em}
.hint{font-size:14px;color:var(--label-3);margin-top:var(--s4);
  text-align:center;letter-spacing:-.005em}
.note{background:var(--surface-2);border-radius:var(--r-md);
  padding:var(--s4) var(--s5);font-size:15px;color:var(--label-2);
  margin:var(--s5) 0 0}
.muted{color:var(--label-3)}
.mono{font-family:var(--mono);font-size:14px;
  font-variant-numeric:tabular-nums}
.right{text-align:right}
.rule{height:.5px;background:var(--separator);border:0;margin:var(--s6) 0}
a{color:var(--brand);text-decoration:none}
a:hover{text-decoration:underline}

/* ---- instruction rows: a titled row reads faster than a bare list ---- */
.rows{display:flex;flex-direction:column;gap:var(--s5);margin:0 0 var(--s6)}
.row-item{display:flex;gap:var(--s4);align-items:flex-start}
.row-mark{flex:none;width:26px;height:26px;border-radius:50%;
  background:var(--brand-tint);color:var(--brand);display:flex;
  align-items:center;justify-content:center;font-size:14px;font-weight:600;
  margin-top:1px;font-variant-numeric:tabular-nums}
.row-t{font-size:17px;font-weight:600;letter-spacing:-.018em;
  margin:0 0 2px}
.row-d{font-size:15px;color:var(--label-2);margin:0;letter-spacing:-.006em}

/* ---- progress: how far along, never how hard ----
   Deliberately vocabulary-free: a test scans every student page for the
   adaptive engine's terms, and CSS comments ship to the browser too. ---- */
.rail{display:flex;gap:3px;margin:0 0 var(--s2)}
.tick{flex:1;height:4px;border-radius:2px;background:var(--surface-3)}
.tick.done{background:var(--brand)}
.tick.now{background:var(--brand);opacity:.45}
.status{display:flex;justify-content:space-between;align-items:baseline;
  font-size:14px;color:var(--label-3);margin-bottom:var(--s6);
  letter-spacing:-.005em}
.clock{font-variant-numeric:tabular-nums;font-feature-settings:"tnum";
  letter-spacing:0;color:var(--label-2)}
.clock.low{color:var(--red);font-weight:600}

/* ---- the question ---- */
.prompt{font-size:24px;line-height:1.3;margin:0 0 var(--s6);font-weight:600;
  letter-spacing:-.026em}
@media(max-width:600px){.prompt{font-size:21px;letter-spacing:-.022em}}
.opts{display:flex;flex-direction:column;gap:var(--s2);margin-bottom:var(--s6)}
.opt{display:flex;gap:var(--s3);align-items:flex-start;
  border:1px solid var(--separator);border-radius:var(--r-md);
  padding:var(--s4);cursor:pointer;background:var(--surface);
  min-height:44px;
  transition:border-color .15s ease,background .15s ease,transform .1s ease}
.opt:hover{border-color:var(--separator-strong);background:var(--surface-2)}
.opt:active{transform:scale(.994)}
.opt input{position:absolute;opacity:0;pointer-events:none}
.key{flex:none;width:24px;height:24px;border-radius:6px;
  background:var(--surface-3);color:var(--label-3);font-size:13px;
  font-weight:600;display:flex;align-items:center;justify-content:center;
  font-variant-numeric:tabular-nums;transition:background .15s,color .15s}
.opt.sel{border-color:var(--brand);background:var(--brand-tint)}
.opt.sel .key{background:var(--brand);color:var(--on-brand)}
.opt:focus-within{outline:3px solid var(--focus);outline-offset:2px}
img.figure{max-width:100%;border:.5px solid var(--separator);
  border-radius:var(--r-md);margin:0 0 var(--s5);display:block}

/* ---- controls: 44pt minimum, per HIG ---- */
.btn{display:inline-flex;align-items:center;justify-content:center;
  background:var(--brand);color:var(--on-brand);border:0;
  border-radius:var(--r-md);padding:var(--s3) var(--s6);font-size:17px;
  font-family:inherit;font-weight:590;letter-spacing:-.014em;cursor:pointer;
  min-height:48px;transition:background .15s ease,transform .1s ease,
  opacity .15s ease}
.btn:hover{background:var(--brand-press)}
.btn:active{transform:scale(.985)}
.btn:disabled{background:var(--surface-3);color:var(--label-4);
  cursor:not-allowed;transform:none}
.btn:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
.btn.wide{width:100%}
.btn.ghost{background:transparent;color:var(--label-2);
  border:1px solid var(--separator)}
.btn.ghost:hover{background:var(--surface-2);color:var(--label)}
.btn.sm{padding:var(--s2) var(--s4);min-height:36px;font-size:15px;
  border-radius:var(--r-sm)}

/* ---- forms ---- */
.field{margin-bottom:var(--s4)}
label.lbl{display:block;font-size:14px;color:var(--label-2);
  margin-bottom:var(--s2);font-weight:500;letter-spacing:-.005em}
input[type=text],input[type=password],select,textarea{width:100%;
  padding:var(--s3) var(--s4);border:1px solid var(--separator);
  border-radius:var(--r-md);font-size:17px;font-family:inherit;
  background:var(--surface);color:var(--label);min-height:48px;
  letter-spacing:-.01em;transition:border-color .15s}
input:hover,select:hover,textarea:hover{border-color:var(--separator-strong)}
input:focus,select:focus,textarea:focus{outline:3px solid var(--focus);
  outline-offset:1px;border-color:transparent}
textarea{font-family:var(--mono);font-size:14px;min-height:132px;
  line-height:1.5}
input[type=file]{font-size:15px;font-family:inherit}
.err{background:var(--red-tint);border:.5px solid var(--red);
  color:var(--red);border-radius:var(--r-md);padding:var(--s3) var(--s4);
  font-size:15px;margin-bottom:var(--s5)}
.flash{border-radius:var(--r-md);padding:var(--s3) var(--s4);font-size:15px;
  margin-bottom:var(--s5);background:var(--green-tint);color:var(--green)}
.flash.bad{background:var(--red-tint);color:var(--red)}

/* ---- completion ---- */
.done-mark{width:56px;height:56px;border-radius:50%;
  background:var(--brand-tint);color:var(--brand);display:flex;
  align-items:center;justify-content:center;font-size:28px;
  margin-bottom:var(--s5)}

/* ---- admin ---- */
/* navigation and the one action that isn't navigation, kept visually apart */
.tabrow{display:flex;align-items:flex-end;justify-content:space-between;
  gap:var(--s4);border-bottom:.5px solid var(--separator);
  margin-bottom:var(--s6)}
.tabrow .btn{margin-bottom:var(--s2);flex:none}
.tabs{display:flex;gap:var(--s1);overflow-x:auto;scrollbar-width:none;
  min-width:0}
.tabs::-webkit-scrollbar{display:none}
.tabs a{padding:var(--s3) var(--s4);font-size:15px;color:var(--label-3);
  text-decoration:none;border-bottom:2px solid transparent;
  white-space:nowrap;font-weight:500;letter-spacing:-.008em;
  transition:color .15s}
.tabs a:hover{color:var(--label);text-decoration:none}
.tabs a.on{color:var(--brand);border-bottom-color:var(--brand);font-weight:600}
/* 1px, not .5px: sub-pixel grid gaps drop out unevenly at some zoom levels */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));
  gap:1px;background:var(--separator);border:.5px solid var(--separator);
  border-radius:var(--r-lg);overflow:hidden;margin-bottom:var(--s6)}
.stat{background:var(--surface);padding:var(--s5)}
.stat .n{font-size:30px;font-weight:650;letter-spacing:-.032em;
  font-variant-numeric:tabular-nums;line-height:1.1}
.stat .n.word{font-size:24px;letter-spacing:-.024em}   /* a word, not a figure */
.yes{color:var(--green);font-weight:600}
.no{color:var(--red);font-weight:600}
.stat .l{font-size:13px;color:var(--label-3);margin-top:var(--s1);
  letter-spacing:-.003em}
.stat.alert .n{color:var(--red)}
.bar{display:flex;flex-wrap:wrap;gap:var(--s3);align-items:center;
  margin-bottom:var(--s4)}
.bar h2{margin:0}
.bar select,.bar input[type=text]{padding:var(--s2) var(--s3);font-size:15px;
  min-height:36px;width:auto;border-radius:var(--r-sm)}
.spacer{flex:1}
.pill{display:inline-block;padding:3px 10px;border-radius:20px;font-size:13px;
  background:var(--surface-3);color:var(--label-2);font-weight:500;
  letter-spacing:-.003em;white-space:nowrap}
.pill.live{background:var(--green-tint);color:var(--green)}
.pill.warn{background:var(--red-tint);color:var(--red)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:var(--s4)}
@media(max-width:700px){.grid2{grid-template-columns:1fr}}

/* ---- tables ---- */
.scroll-x{overflow-x:auto;-webkit-overflow-scrolling:touch}
table.data{border-collapse:collapse;width:100%;font-size:15px;
  margin:var(--s3) 0}
table.data th,table.data td{border-bottom:.5px solid var(--separator);
  padding:var(--s3);text-align:left;letter-spacing:-.006em}
table.data th{font-size:12px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--label-3);font-weight:600;white-space:nowrap}
table.data tr:last-child td{border-bottom:0}
table.data tbody tr:hover td{background:var(--surface-2)}
.rank{font-variant-numeric:tabular-nums;color:var(--label-3);width:40px}

@media(prefers-reduced-motion:reduce){
  *{transition:none!important;animation:none!important}
  .opt:active,.btn:active{transform:none}
}
</style>"""


def page(body, who="", title=None, wide=False):
    cls = "wrap wide-wrap" if wide else "wrap"
    # viewport-fit + color-scheme: native form controls, scrollbars and the
    # iOS status bar follow the user's appearance setting instead of fighting it.
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>{html.escape(title or TITLE)}</title>{STYLE}</head><body><div class="{cls}">
<div class="topbar"><span class="brand">MESA</span>
<span class="who">{who}</span></div>{body}</div></body></html>"""


def fmt_clock(seconds):
    m, s = divmod(max(0, int(seconds)), 60)
    return f"{m}:{s:02d}"


def admin_key_ok(supplied):
    """Constant-time admin key check that refuses to accept nothing.

    Guards against a blank ADMIN_KEY: an empty configured key compared
    against an absent ?key= would match, opening the dashboard and the
    results export to anyone who found the URL.
    """
    if not ADMIN_KEY or not supplied:
        return False
    return secrets.compare_digest(str(supplied), str(ADMIN_KEY))


def answered_line(done):
    """'12 answered' — or '12 of 30 answered' when a cap is configured."""
    if MAXQ:
        return f"{done} of {MAXQ} answered"
    return f"{done} answered" if done != 1 else "1 answered"


# Database enum values are for the database. Admins read plain English.
STATUS_LABEL = {
    "active": "In progress",
    "completed": "Finished",
    "expired": "Ran out of time",
    "exhausted": "Bank exhausted",
}


def status_label(status):
    return STATUS_LABEL.get(status, str(status).replace("_", " ").capitalize())


# ---------- data helpers ----------

def all_questions(con):
    """Only active questions enter the pool; retired ones stay for history."""
    return [dict(id=r["id"], difficulty=r["difficulty"],
                 topics=json.loads(r["topics_json"]))
            for r in con.execute("SELECT * FROM questions WHERE active=1")]


def get_student(con, token):
    if not token:
        return None
    return con.execute(
        "SELECT s.* FROM sessions x JOIN students s ON s.id=x.student_id "
        "WHERE x.token=?", (token,)).fetchone()


def active_attempt(con, student_id):
    return con.execute("SELECT * FROM attempts WHERE student_id=? AND status='active'",
                       (student_id,)).fetchone()


def latest_attempt(con, student_id):
    return con.execute("SELECT * FROM attempts WHERE student_id=? "
                       "ORDER BY started_at DESC LIMIT 1", (student_id,)).fetchone()


def seen_ids(con, attempt_id):
    return {r["question_id"] for r in con.execute(
        "SELECT question_id FROM attempt_questions WHERE attempt_id=?", (attempt_id,))}


def topic_served(con, attempt_id):
    counts = {}
    for r in con.execute(
        "SELECT q.topics_json t FROM attempt_questions aq JOIN questions q "
        "ON q.id=aq.question_id WHERE aq.attempt_id=?", (attempt_id,)):
        for topic in json.loads(r["t"]):
            counts[topic] = counts.get(topic, 0) + 1
    return counts


def check_expiry(con, attempt):
    if attempt["status"] == "active" and time.time() > attempt["deadline"]:
        con.execute("UPDATE attempts SET status='expired' WHERE id=?", (attempt["id"],))
        con.commit()
        return True
    return False


def log_event(con, attempt_id, kind, detail=""):
    con.execute("INSERT INTO events(attempt_id,ts,kind,detail) VALUES(?,?,?,?)",
                (attempt_id, time.time(), kind, detail))
    con.commit()


def assign_next(con, attempt):
    state = json.loads(attempt["adaptive_state_json"])
    rng = random.Random(attempt["id"] + str(attempt["answered_count"]))
    q, debug = ENGINE.select_next(state, seen_ids(con, attempt["id"]),
                                  all_questions(con),
                                  topic_served(con, attempt["id"]), rng)
    if q is None:
        con.execute("UPDATE attempts SET status='exhausted', current_question_id=NULL "
                    "WHERE id=?", (attempt["id"],))
        con.commit()
        return None
    con.execute(
        "INSERT INTO attempt_questions(attempt_id,question_id,seq,shown_at,difficulty,"
        "state_before_json,selection_debug_json) VALUES(?,?,?,?,?,?,?)",
        (attempt["id"], q["id"], attempt["answered_count"] + 1, time.time(),
         q["difficulty"], json.dumps(state), json.dumps(debug)))
    con.execute("UPDATE attempts SET current_question_id=? WHERE id=?",
                (q["id"], attempt["id"]))
    con.commit()
    return con.execute("SELECT * FROM questions WHERE id=?", (q["id"],)).fetchone()


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "MESA/1.0"
    protocol_version = "HTTP/1.1"

    def _send(self, body, status=200, ctype="text/html; charset=utf-8", cookies=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for c in (cookies or []):
            self.send_header("Set-Cookie", c)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, loc, cookies=None):
        self.send_response(303)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        for c in (cookies or []):
            self.send_header("Set-Cookie", c)
        self.end_headers()

    def _cookie(self, name):
        for part in (self.headers.get("Cookie", "") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == name:
                    return v
        return None

    def _form(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(self.rfile.read(length).decode()).items()}

    def log_message(self, fmt, *args):
        pass   # quiet: real events are logged explicitly below

    # -- routing --
    def do_GET(self):
        con = storage.connect()
        try:
            parsed = urllib.parse.urlparse(self.path)
            path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
            routes = {
                "/": lambda: self.page_login(),
                "/home": lambda: self.page_home(con),
                "/instructions": lambda: self.page_instructions(con),
                "/exam": lambda: self.page_exam(con),
                "/done": lambda: self.page_done(con),
                "/admin": lambda: self.page_admin(con, qs),
                "/admin/students": lambda: self.page_admin_students(con, qs),
                "/admin/leaderboard": lambda: self.page_leaderboard(con, qs),
                "/admin/attempt": lambda: self.page_admin_attempt(con, qs),
                "/admin/questions": lambda: self.page_admin_questions(con, qs),
                "/admin/questions/template.csv": lambda: self.admin_template(con, qs),
                "/admin/questions/export.json": lambda: self.admin_export_bank(con, qs),
                "/admin/export.csv": lambda: self.admin_export(con, qs),
            }
            if path.startswith("/assets/"):
                return self.serve_asset(path)
            handler = routes.get(path)
            if handler:
                return handler()
            self._send(page('<div class="card"><h1>Page not found</h1></div>'), 404)
        finally:
            con.close()

    def do_POST(self):
        con = storage.connect()
        try:
            path = urllib.parse.urlparse(self.path).path
            if path == "/login":
                return self.act_login(con)
            if path == "/logout":
                return self._redirect("/", ["mesa_session=; Max-Age=0; Path=/"])
            if path == "/start":
                return self.act_start(con)
            if path == "/answer":
                return self.act_answer(con)
            if path == "/event":
                return self.act_event(con)
            if path == "/admin/questions/add":
                return self.act_question_add(con)
            if path == "/admin/questions/upload":
                return self.act_question_upload(con)
            if path == "/admin/questions/toggle":
                return self.act_question_toggle(con)
            if path == "/admin/students/add":
                return self.act_student_add(con)
            if path == "/admin/students/reset":
                return self.act_student_reset(con)
            if path == "/admin/students/toggle":
                return self.act_student_toggle(con)
            self._send(page('<div class="card"><h1>Page not found</h1></div>'), 404)
        finally:
            con.close()

    # -- static --
    def serve_asset(self, path):
        name = os.path.basename(path)
        full = os.path.join(BASE, "assets", name)
        if not name.endswith(".svg") or not os.path.isfile(full):
            return self._send("not found", 404, "text/plain")
        with open(full, "rb") as f:
            self._send(f.read(), 200, "image/svg+xml")

    # -- auth --
    def page_login(self, error=""):
        body = f"""<div class="card">
        <h1>{html.escape(TITLE)}</h1>
        <p class="lede">Sign in with the username and password your
        coordinator gave you.</p>
        {'<div class="err">' + html.escape(error) + '</div>' if error else ''}
        <form method="post" action="/login" autocomplete="off">
        <div class="field"><label class="lbl" for="u">Username</label>
        <input type="text" id="u" name="username" autofocus autocapitalize="none"
        autocorrect="off" spellcheck="false"></div>
        <div class="field"><label class="lbl" for="p">Password</label>
        <input type="password" id="p" name="password"></div>
        <button class="btn wide">Sign in</button></form></div>"""
        self._send(page(body))

    def act_login(self, con):
        f = self._form()
        row = con.execute("SELECT * FROM students WHERE username=?",
                          (f.get("username", "").strip().lower(),)).fetchone()
        if not row or storage.hash_pw(f.get("password", ""), row["salt"]) != row["pw_hash"]:
            return self.page_login("That username and password don't match. "
                                   "Check for an extra space at either end, "
                                   "then try again.")
        if not row["active"]:
            return self.page_login("This account has been deactivated. "
                                   "Your coordinator can turn it back on.")
        token = secrets.token_urlsafe(24)
        con.execute("INSERT INTO sessions(token,student_id,created_at) VALUES(?,?,?)",
                    (token, row["id"], time.time()))
        con.commit()
        self._redirect("/home", [f"mesa_session={token}; HttpOnly; Path=/; "
                                 f"SameSite=Lax; Max-Age={SESSION_MAX_AGE}"])

    def _require_student(self, con):
        s = get_student(con, self._cookie("mesa_session"))
        if not s:
            self._redirect("/")
        return s

    # -- student screens --
    def page_home(self, con):
        s = self._require_student(con)
        if not s:
            return
        att = active_attempt(con, s["id"])
        if att and not check_expiry(con, att):
            left = fmt_clock(att["deadline"] - time.time())
            done = att["answered_count"]
            body = f"""<div class="card">
            <h1>Welcome back</h1>
            <p class="lede">Every answer you submitted is saved. Your time
            kept running while you were away.</p>
            {self._rail_block(done)}
            <div class="status"><span>{answered_line(done)}</span>
            <span class="clock">{left} left</span></div>
            <form method="post" action="/start">
            <button class="btn wide">Resume exam</button></form></div>"""
            return self._send(page(body, html.escape(s["name"])))
        if latest_attempt(con, s["id"]):
            return self._redirect("/done")
        return self._redirect("/instructions")

    def page_instructions(self, con):
        s = self._require_student(con)
        if not s:
            return
        mins = CONFIG["exam"]["durationMinutes"]
        # Each rule gets a title you can scan and a line that says what it
        # actually means for you. Anything a student might reasonably worry
        # about — losing work, getting a harder paper than a friend, being
        # watched — is answered here rather than left to guesswork.
        rules = [
            ("One question at a time",
             "No going back, no skipping ahead. Once you move on, that "
             "question is behind you."),
            ("Answer as many as you can" if not MAXQ else "A fixed set",
             f"There's no set number to get through. Keep going for the full "
             f"{mins} minutes and answer as many as you're able — the exam "
             f"ends when your time is up."
             if not MAXQ else
             f"You'll be given {MAXQ} questions."),
            ("Answers are final",
             "Submitting locks that answer in. Take the moment you need "
             "before you commit."),
            ("Nothing gets lost",
             f"Your progress saves automatically, the instant you submit "
             f"each answer. Close the tab, run out of battery, switch "
             f"devices — sign back in and you'll land on the exact question "
             f"you left. The {mins} minutes keep running while you're away."),
            ("The exam adapts to you",
             "Your next question is chosen from how you've answered so far, "
             "so your paper won't match anyone else's. A question that feels "
             "tough isn't a sign you're doing badly."),
        ]
        if PROCTOR_FOCUS:
            rules.append((
                "Leaving this window is recorded",
                "We log the moment the exam window loses focus, and your "
                "coordinator sees it. We can't see your screen, your other "
                "tabs, or anything else on your device."))
        rules.append((
            "Results come later",
            "Nothing is shown here during the exam or at the end — no "
            "marks, no answers. Results are released by your coordinator."))
        rows = "".join(
            f'<div class="row-item"><div class="row-mark">{i}</div><div>'
            f'<p class="row-t">{t}</p><p class="row-d">{d}</p></div></div>'
            for i, (t, d) in enumerate(rules, 1))
        body = f"""<div class="card">
        <h1>Before you begin</h1>
        <p class="lede">{f'{MAXQ} questions, ' if MAXQ else ''}{mins} minutes.
        Here's how it works.</p>
        <div class="rows">{rows}</div>
        <hr class="rule">
        <form method="post" action="/start">
        <button class="btn wide">Start exam</button></form>
        <p class="hint">Your {mins} minutes begin the moment you start.</p>
        </div>"""
        self._send(page(body, html.escape(s["name"])))

    def act_start(self, con):
        s = self._require_student(con)
        if not s:
            return
        att = active_attempt(con, s["id"])
        if not att:
            if latest_attempt(con, s["id"]):
                return self._redirect("/done")      # one attempt per student
            aid = secrets.token_hex(8)
            now = time.time()
            con.execute("INSERT INTO attempts(id,student_id,started_at,deadline,"
                        "status,adaptive_state_json) VALUES(?,?,?,?,'active',?)",
                        (aid, s["id"], now, now + DURATION,
                         json.dumps(ENGINE.initial_state())))
            con.commit()
            att = active_attempt(con, s["id"])
            assign_next(con, att)
        else:
            log_event(con, att["id"], "resume")
        self._redirect("/exam")

    def _rail(self, done, total, current=True):
        out = []
        for i in range(total):
            if i < done:
                out.append('<span class="tick done"></span>')
            elif i == done and current:
                out.append('<span class="tick now"></span>')
            else:
                out.append('<span class="tick"></span>')
        return "".join(out)

    def _rail_block(self, done, current=True):
        """A rail measures progress towards an end. With no fixed length there
        is no end to measure towards, so we show nothing rather than a bar
        that fills up against an invented total."""
        if not MAXQ:
            return ""
        return f'<div class="rail">{self._rail(done, MAXQ, current)}</div>'

    def page_exam(self, con):
        s = self._require_student(con)
        if not s:
            return
        att = active_attempt(con, s["id"])
        if not att or check_expiry(con, att):
            return self._redirect("/done")
        q = con.execute("SELECT * FROM questions WHERE id=?",
                        (att["current_question_id"],)).fetchone()
        if not q:
            return self._redirect("/done")
        num = att["answered_count"] + 1
        remaining = max(0, int(att["deadline"] - time.time()))
        figure = ""
        if q["qtype"] == "image" and q["asset"]:
            figure = (f'<img class="figure" src="/{html.escape(q["asset"])}" '
                      f'alt="Figure for this question">')
        elif q["qtype"] == "dataset" and q["dataset_json"]:
            rows = json.loads(q["dataset_json"])
            head = "".join(f"<th>{html.escape(c)}</th>" for c in rows[0])
            trs = "".join("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in r)
                          + "</tr>" for r in rows[1:])
            figure = f'<table class="data"><tr>{head}</tr>{trs}</table>'
        opts = "".join(
            f'<label class="opt" data-i="{i}"><input type="radio" name="answer" '
            f'value="{i}" required><span class="key">{i + 1}</span>'
            f'<span>{html.escape(o)}</span></label>'
            for i, o in enumerate(json.loads(q["options_json"])))
        proctor_js = """
  let away=0;
  const beacon=k=>{try{navigator.sendBeacon('/event',new Blob(
    ['kind='+k],{type:'application/x-www-form-urlencoded'}));}catch(e){}};
  document.addEventListener('visibilitychange',()=>{
    if(document.hidden){away++;beacon('blur');}else{beacon('return');}});
""" if PROCTOR_FOCUS else ""
        body = f"""<div class="card">
        {self._rail_block(att['answered_count'])}
        <div class="status"><span>Question {num}{f' of {MAXQ}' if MAXQ else ''}</span>
        <span class="clock" id="clock">{fmt_clock(remaining)}</span></div>
        <p class="prompt">{html.escape(q['prompt'])}</p>{figure}
        <form method="post" action="/answer" id="f">
        <input type="hidden" name="qid" value="{q['id']}">
        <div class="opts">{opts}</div>
        <button class="btn wide" id="sub" disabled>Submit answer</button></form>
        <p class="hint">Press 1–4 to choose · Enter to submit</p>
        </div>
<script>
  const f=document.getElementById('f'),sub=document.getElementById('sub');
  const opts=[...document.querySelectorAll('.opt')];
  const mark=()=>{{opts.forEach(o=>o.classList.toggle('sel',
    o.querySelector('input').checked));sub.disabled=!f.querySelector('input:checked');}};
  opts.forEach(o=>o.addEventListener('click',()=>{{
    o.querySelector('input').checked=true;mark();}}));
  document.addEventListener('keydown',e=>{{
    if(e.key>='1'&&e.key<='4'){{const o=opts[+e.key-1];
      if(o){{o.querySelector('input').checked=true;mark();}}}}
    if(e.key==='Enter'&&!sub.disabled){{f.requestSubmit();}}}});
  f.addEventListener('submit',()=>{{sub.disabled=true;sub.textContent='Saving…';}});
  let r={remaining};const c=document.getElementById('clock');
  setInterval(()=>{{r=Math.max(0,r-1);
    c.textContent=Math.floor(r/60)+':'+String(r%60).padStart(2,'0');
    if(r<=300)c.classList.add('low');
    if(r===0)location.href='/done';}},1000);
{proctor_js}
</script>"""
        self._send(page(body, html.escape(s["name"])))

    def act_answer(self, con):
        s = self._require_student(con)
        if not s:
            return
        att = active_attempt(con, s["id"])
        if not att or check_expiry(con, att):
            return self._redirect("/done")
        f = self._form()
        qid = f.get("qid", "")
        if qid != att["current_question_id"]:
            return self._send(page(
                '<div class="card"><h1>That question has moved on</h1>'
                '<p class="lede">This looks like an old tab or a double '
                'submission. Nothing was lost.</p>'
                '<a class="btn" href="/exam">Back to my current question</a></div>'),
                409)
        row = con.execute("SELECT * FROM attempt_questions WHERE attempt_id=? "
                          "AND question_id=?", (att["id"], qid)).fetchone()
        if row["answered_at"] is not None:
            return self._redirect("/exam")
        try:
            ans = int(f.get("answer", "-1"))
        except ValueError:
            ans = -1
        if not 0 <= ans <= 3:
            return self._redirect("/exam")
        q = con.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
        correct = 1 if ans == q["answer_index"] else 0
        state = json.loads(att["adaptive_state_json"])
        new_state, decision = ENGINE.record_answer(state, bool(correct))
        now = time.time()
        con.execute(
            "UPDATE attempt_questions SET answered_at=?, answer_index=?, is_correct=?,"
            " time_taken=?, decision=?, next_difficulty=? "
            "WHERE attempt_id=? AND question_id=?",
            (now, ans, correct, now - row["shown_at"], decision,
             new_state["difficulty"], att["id"], qid))
        con.execute("UPDATE attempts SET adaptive_state_json=?, "
                    "answered_count=answered_count+1, score=score+?, "
                    "current_question_id=NULL WHERE id=?",
                    (json.dumps(new_state), correct, att["id"]))
        con.commit()
        print(f"[answer] {s['username']} attempt={att['id']} q={qid} "
              f"correct={correct} t={now - row['shown_at']:.1f}s "
              f"decision='{decision}' next={new_state['difficulty']}")
        att = con.execute("SELECT * FROM attempts WHERE id=?", (att["id"],)).fetchone()
        if ENGINE.is_exam_complete(att["answered_count"]):
            con.execute("UPDATE attempts SET status='completed' WHERE id=?",
                        (att["id"],))
            con.commit()
            return self._redirect("/done")
        if assign_next(con, att) is None:
            return self._redirect("/done")
        self._redirect("/exam")

    def act_event(self, con):
        """Browser-level proctoring signal (focus lost/regained)."""
        s = get_student(con, self._cookie("mesa_session"))
        if not s:
            return self._send("", 204)
        att = active_attempt(con, s["id"])
        kind = self._form().get("kind", "")
        if att and kind in ("blur", "return", "fullscreen_exit"):
            log_event(con, att["id"], kind)
        self._send("", 204)

    def page_done(self, con):
        s = self._require_student(con)
        if not s:
            return
        att = latest_attempt(con, s["id"])
        if att and att["status"] == "active" and not check_expiry(con, att):
            return self._redirect("/exam")
        status = att["status"] if att else ""
        if status == "expired":
            headline = "Time's up"
            lede = ("Everything you submitted before the clock ran out has "
                    "been saved.")
        elif status == "exhausted":
            headline = "You've answered everything"
            lede = ("You reached the end of the question bank with time to "
                    "spare. Your answers have been saved.")
        else:
            headline = "All done"
            lede = "Your answers have been saved. You can close this window."
        n = att["answered_count"] if att else 0
        body = f"""<div class="card">
        <div class="done-mark">✓</div>
        <h1>{headline}</h1>
        <p class="lede">{lede}</p>
        {self._rail_block(n, current=False)}
        <div class="status"><span>{answered_line(n)}</span><span></span></div>
        <p class="note">No result is shown here, and none was shown during
        the exam. Results are released by your coordinator once everyone has
        finished.</p>
        <form method="post" action="/logout" style="margin-top:var(--s5)">
        <button class="btn ghost wide">Sign out</button></form></div>"""
        self._send(page(body, html.escape(s["name"])))
    # ---------------- admin ----------------
    def _require_admin(self, qs):
        if not admin_key_ok(qs.get("key", [""])[0]):
            self._send(page('<div class="card"><h1>Admin key required</h1>'
                            '<p class="lede">This dashboard needs your admin '
                            'key. Add <span class="mono">?key=…</span> to the '
                            'URL.</p><p class="note">The key sits in the URL, '
                            'so avoid screen-sharing this tab.</p></div>',
                            title="Admin"), 403)
            return False
        return True

    def _tabs(self, key, here):
        items = [("/admin", "Overview"), ("/admin/students", "Students"),
                 ("/admin/leaderboard", "Leaderboard"),
                 ("/admin/questions", "Questions")]
        on = " class='on'"          # hoisted: a backslash inside an f-string
        links = "".join(            # expression needs Python 3.12+
            f'<a href="{p}?key={key}"{on if p == here else ""}>{n}</a>'
            for p, n in items)
        # Export is an action, not a destination — it doesn't belong in the tabs.
        return (f'<div class="tabrow"><div class="tabs">{links}</div>'
                f'<a class="btn ghost sm" href="/admin/export.csv?key={key}">'
                f'Export results</a></div>')

    def _flash(self, qs):
        msg = qs.get("msg", [""])[0]
        if not msg:
            return ""
        bad = msg.startswith("!")
        return (f'<div class="flash{" bad" if bad else ""}">'
                f'{html.escape(msg.lstrip("!"))}</div>')

    def page_admin(self, con, qs):
        if not self._require_admin(qs):
            return
        key = qs["key"][0]
        now = time.time()
        total_students = con.execute("SELECT COUNT(*) c FROM students").fetchone()["c"]
        rows = con.execute(
            "SELECT a.*, s.username, s.name FROM attempts a JOIN students s "
            "ON s.id=a.student_id ORDER BY a.started_at DESC").fetchall()
        started = len(rows)
        completed = sum(1 for r in rows if r["status"] in ("completed", "exhausted"))
        live = sum(1 for r in rows if r["status"] == "active" and r["deadline"] > now)
        answered = sum(r["answered_count"] for r in rows)
        correct = sum(r["score"] for r in rows)
        accuracy = f"{round(100 * correct / answered)}%" if answered else "—"
        flagged = con.execute(
            "SELECT COUNT(DISTINCT attempt_id) c FROM events WHERE kind='blur'"
        ).fetchone()["c"]

        trs = ""
        for r in rows:
            ev = con.execute("SELECT COUNT(*) c FROM events WHERE attempt_id=? "
                             "AND kind='blur'", (r["id"],)).fetchone()["c"]
            pct = (f"{round(100 * r['score'] / r['answered_count'])}%"
                   if r["answered_count"] else "—")
            count_cell = (f"{r['answered_count']}/{MAXQ}" if MAXQ
                          else r["answered_count"])
            if r["status"] == "active" and r["deadline"] > now:
                state = f'<span class="pill live">{fmt_clock(r["deadline"] - now)} left</span>'
            else:
                state = f'<span class="pill">{status_label(r["status"])}</span>'
            flag = (f'<span class="pill warn" title="Left the exam window '
                    f'{ev} time{"s" if ev != 1 else ""}">{ev}</span>'
                    if ev else "")
            trs += (f"<tr><td><a href='/admin/attempt?key={key}&id={r['id']}'>"
                    f"{html.escape(r['name'])}</a></td>"
                    f"<td>{state}</td>"
                    f"<td class='mono'>{count_cell}</td>"
                    f"<td class='mono'>{pct}</td>"
                    f"<td>{json.loads(r['adaptive_state_json'])['difficulty'].capitalize()}</td>"
                    f"<td>{flag}</td></tr>")
        trs = trs or ('<tr><td colspan="6" class="muted">Nobody has started '
                      'yet. Attempts appear here the moment a student signs '
                      'in.</td></tr>')

        focus_pill = ('<span class="pill">Focus tracking on</span>'
                      if PROCTOR_FOCUS else "")
        body = f"""{self._tabs(key, '/admin')}{self._flash(qs)}
        <div class="stats">
        <div class="stat"><div class="n">{started}</div>
          <div class="l">Started of {total_students}</div></div>
        <div class="stat"><div class="n">{live}</div><div class="l">In progress</div></div>
        <div class="stat"><div class="n">{completed}</div><div class="l">Finished</div></div>
        <div class="stat"><div class="n">{accuracy}</div><div class="l">Cohort accuracy</div></div>
        <div class="stat{' alert' if flagged else ''}"><div class="n">{flagged}</div>
          <div class="l">Left the window</div></div>
        </div>
        <div class="bar"><h2>Attempts</h2><span class="spacer"></span>
        {focus_pill}</div>
        <div class="scroll-x">
        <table class="data"><tr><th>Student</th><th>Status</th><th>Answered</th>
        <th>Accuracy</th><th>Level</th><th>Flags</th></tr>{trs}</table></div>"""
        self._send(page(body, "Admin", "Admin — overview", wide=True))

    def page_leaderboard(self, con, qs):
        if not self._require_admin(qs):
            return
        key = qs["key"][0]
        rows = con.execute("""
            SELECT s.username, s.name, a.id, a.status, a.answered_count, a.score,
                   a.started_at,
                   (SELECT MAX(answered_at) FROM attempt_questions
                     WHERE attempt_id=a.id) AS last_at
            FROM attempts a JOIN students s ON s.id=a.student_id
            WHERE a.answered_count > 0
            ORDER BY a.score DESC, a.answered_count DESC, last_at ASC""").fetchall()
        trs = ""
        for i, r in enumerate(rows, 1):
            pct = round(100 * r["score"] / r["answered_count"])
            dur = fmt_clock((r["last_at"] or r["started_at"]) - r["started_at"])
            trs += (f"<tr><td class='rank'>{i}</td>"
                    f"<td><a href='/admin/attempt?key={key}&id={r['id']}'>"
                    f"{html.escape(r['name'])}</a></td>"
                    f"<td class='mono'><b>{r['score']}</b>/{r['answered_count']}</td>"
                    f"<td class='mono'>{pct}%</td><td class='mono'>{dur}</td>"
                    f"<td><span class='pill'>{status_label(r['status'])}"
                    f"</span></td></tr>")
        trs = trs or ('<tr><td colspan="6" class="muted">No answers yet. '
                      'Rankings build as students submit.</td></tr>')
        body = f"""{self._tabs(key, '/admin/leaderboard')}
        <div class="bar"><h2>Leaderboard</h2>
        <span class="spacer"></span>
        <span class="pill">Admin only — students never see a score</span></div>
        <div class="scroll-x">
        <table class="data"><tr><th class="rank">#</th><th>Student</th>
        <th>Correct</th><th>Accuracy</th><th>Time</th><th>Status</th></tr>
        {trs}</table></div>"""
        self._send(page(body, "Admin", "Admin — leaderboard", wide=True))

    def page_admin_attempt(self, con, qs):
        if not self._require_admin(qs):
            return
        key, aid = qs["key"][0], qs.get("id", [""])[0]
        head = con.execute(
            "SELECT a.*, s.name FROM attempts a JOIN students s ON s.id=a.student_id "
            "WHERE a.id=?", (aid,)).fetchone()
        if not head:
            return self._send(page('<div class="card"><h1>Attempt not found</h1></div>',
                                   title="Admin"), 404)
        rows = con.execute(
            "SELECT aq.*, q.topics_json FROM attempt_questions aq "
            "JOIN questions q ON q.id=aq.question_id WHERE aq.attempt_id=? "
            "ORDER BY aq.seq", (aid,)).fetchall()
        trs = ""
        for r in rows:
            sb = json.loads(r["state_before_json"])
            dbg = json.loads(r["selection_debug_json"] or "{}")
            mark = ('<span class="muted">—</span>' if r["is_correct"] is None
                    else ('<span class="yes">✓</span>' if r["is_correct"]
                          else '<span class="no">✗</span>'))
            move = r["decision"] or "—"
            move_html = (f"<b>{html.escape(move)}</b>"
                         if not move.startswith("stay") else
                         f"<span class='muted'>{html.escape(move)}</span>")
            trs += (f"<tr><td class='rank'>{r['seq']}</td>"
                    f"<td class='mono'>{r['question_id']}</td>"
                    f"<td>{r['difficulty'].capitalize()}</td>"
                    f"<td class='muted'>{html.escape(json.loads(r['topics_json'])[0])}</td>"
                    f"<td>{mark}</td>"
                    f"<td class='mono'>{sb['difficulty']} +{sb['consecutive_correct']}"
                    f"/-{sb['consecutive_wrong']}</td>"
                    f"<td>{move_html}</td>"
                    f"<td class='muted mono'>{html.escape(dbg.get('ladder_step') or '')}</td>"
                    f"</tr>")
        evs = con.execute("SELECT kind, COUNT(*) c FROM events WHERE attempt_id=? "
                          "GROUP BY kind", (aid,)).fetchall()
        ev_line = " · ".join(f"{e['kind']} {e['c']}" for e in evs) or "none"
        pct = (f"{round(100 * head['score'] / head['answered_count'])}%"
               if head["answered_count"] else "—")
        body = f"""{self._tabs(key, '')}
        <div class="bar"><h2>{html.escape(head['name'])}</h2>
        <span class="spacer"></span>
        <span class="pill">{status_label(head['status'])}</span></div>
        <div class="stats">
        <div class="stat"><div class="n">{head['answered_count']}</div>
          <div class="l">Answered</div></div>
        <div class="stat"><div class="n">{head['score']}</div><div class="l">Correct</div></div>
        <div class="stat"><div class="n">{pct}</div><div class="l">Accuracy</div></div>
        <div class="stat"><div class="n word">{json.loads(head['adaptive_state_json'])['difficulty'].capitalize()}</div>
          <div class="l">Current level</div></div>
        </div>
        <div class="scroll-x">
        <table class="data"><tr><th class="rank">#</th><th>Question</th>
        <th>Level</th><th>Topic</th><th>Correct</th>
        <th>State before</th><th>Decision</th><th>Selection</th></tr>{trs}</table>
        </div>
        <p class="note">Window events — {html.escape(ev_line)}</p>"""
        self._send(page(body, "Admin", "Admin — attempt", wide=True))

    def page_admin_students(self, con, qs):
        if not self._require_admin(qs):
            return
        key = qs["key"][0]
        rows = con.execute(
            "SELECT s.*, COUNT(a.id) attempts FROM students s "
            "LEFT JOIN attempts a ON a.student_id=s.id "
            "GROUP BY s.id ORDER BY s.username").fetchall()
        
        trs = ""
        for r in rows:
            status = "active" if r["active"] else "deactivated"
            label = '<span class="pill live">Active</span>' if r["active"] else \
                    '<span class="pill">Deactivated</span>'
            trs += (f"<tr><td>{html.escape(r['username'])}</td>"
                    f"<td>{html.escape(r['name'])}</td>"
                    f"<td class='mono'>{r['attempts']}</td>"
                    f"<td>{label}</td>"
                    f"<td class='right'>"
                    f"<form method='post' action='/admin/students/reset' style='display:inline'>"
                    f"<input type='hidden' name='key' value='{key}'>"
                    f"<input type='hidden' name='id' value='{r['id']}'>"
                    f"<button class='btn ghost sm'>Reset password</button></form> "
                    f"<form method='post' action='/admin/students/toggle' style='display:inline'>"
                    f"<input type='hidden' name='key' value='{key}'>"
                    f"<input type='hidden' name='id' value='{r['id']}'>"
                    f"<button class='btn ghost sm'>{'Reactivate' if not r['active'] else 'Deactivate'}</button>"
                    f"</form></td></tr>")

        reset_flash = ""
        if qs.get("newpw"):
            pw = qs["newpw"][0]
            uname = qs.get("uname", ["?"])[0]
            reset_flash = (f'<div class="flash"><b>Password reset.</b> '
                          f'Username: <span class="mono">{html.escape(uname)}</span> '
                          f'New password: <span class="mono">{html.escape(pw)}</span> '
                          f'<button onclick="navigator.clipboard.writeText('
                          f"'{pw.replace(chr(39), chr(92) + chr(39))}')"
                          f'" class="btn ghost sm" style="margin-left:8px">Copy to clipboard</button>'
                          f'</div>')

        body = f"""{self._tabs(key, '/admin/students')}{reset_flash}{self._flash(qs)}
        <div class="stats">
        <div class="stat"><div class="n">{len(rows)}</div><div class="l">Total students</div></div>
        <div class="stat"><div class="n">{sum(1 for r in rows if r['active'])}</div>
          <div class="l">Active</div></div>
        <div class="stat"><div class="n">{sum(1 for r in rows if r['attempts'] > 0)}</div>
          <div class="l">Started exam</div></div>
        </div>

        <div class="panel"><h3>Add a student</h3>
        <p class="sub">They can sign in immediately with this username and password.</p>
        <form method="post" action="/admin/students/add">
        <input type="hidden" name="key" value="{key}">
        <div class="grid2">
        <div class="field"><label class="lbl">Username</label>
        <input type="text" name="username" required autocapitalize="none"></div>
        <div class="field"><label class="lbl">Full name</label>
        <input type="text" name="name" required></div>
        </div>
        <div class="grid2">
        <div class="field"><label class="lbl">Password</label>
        <input type="text" name="password" id="pw" required value="{storage.random_password()}"></div>
        <div class="field" style="margin-top:22px">
        <button class="btn ghost sm" type="button" onclick="document.getElementById('pw').value='{storage.random_password()}';void 0">
        Generate</button></div>
        </div>
        <button class="btn sm">Add student</button></form></div>

        <h2>All students</h2>
        <div class="scroll-x">
        <table class="data"><tr><th>Username</th><th>Name</th><th>Attempts</th>
        <th>Status</th><th></th></tr>{trs or '<tr><td colspan="5" class="muted">No students yet. Add one above.</td></tr>'}
        </table></div>"""
        self._send(page(body, "Admin", "Admin — students", wide=True))

    def act_student_add(self, con):
        f = self._form()
        if not admin_key_ok(f.get("key", "")):
            return self._send(page('<div class="card"><h1>Admin key required</h1></div>'),
                              403)
        username = f.get("username", "").strip().lower()
        name = f.get("name", "").strip()
        password = f.get("password", "").strip()
        msg = ""
        try:
            if not username or not name or not password:
                raise ValueError("all fields are required")
            if len(username) < 3:
                raise ValueError("username must be at least 3 characters")
            if con.execute("SELECT id FROM students WHERE username=?",
                          (username,)).fetchone():
                raise ValueError("that username already exists")
            salt = secrets.token_hex(16)
            pw_hash = storage.hash_pw(password, salt)
            con.execute(
                "INSERT INTO students(username,name,salt,pw_hash,active) "
                "VALUES(?,?,?,?,1)",
                (username, name, salt, pw_hash))
            con.commit()
            msg = f"Added {username}."
        except Exception as e:                     # noqa: BLE001
            msg = f"!Could not add the student: {e}"
        self._redirect(f"/admin/students?key={ADMIN_KEY}&msg={urllib.parse.quote(msg)}")

    def act_student_reset(self, con):
        f = self._form()
        if not admin_key_ok(f.get("key", "")):
            return self._send(page('<div class="card"><h1>Admin key required</h1></div>'),
                              403)
        sid = f.get("id", "")
        row = con.execute("SELECT username FROM students WHERE id=?", (sid,)).fetchone()
        if row:
            newpw = storage.random_password()
            salt = secrets.token_hex(16)
            pw_hash = storage.hash_pw(newpw, salt)
            con.execute("UPDATE students SET salt=?, pw_hash=? WHERE id=?",
                       (salt, pw_hash, sid))
            con.commit()
            params = f"key={ADMIN_KEY}&newpw={urllib.parse.quote(newpw)}&uname={row['username']}"
            self._redirect(f"/admin/students?{params}")
        else:
            self._redirect(f"/admin/students?key={ADMIN_KEY}&msg=!Student not found.")

    def act_student_toggle(self, con):
        f = self._form()
        if not admin_key_ok(f.get("key", "")):
            return self._send(page('<div class="card"><h1>Admin key required</h1></div>'),
                              403)
        sid = f.get("id", "")
        row = con.execute("SELECT active FROM students WHERE id=?", (sid,)).fetchone()
        if row:
            new = 0 if row["active"] else 1
            con.execute("UPDATE students SET active=? WHERE id=?", (new, sid))
            con.commit()
            msg = f"{'Reactivated' if new else 'Deactivated'}."
        else:
            msg = "!Student not found."
        self._redirect(f"/admin/students?key={ADMIN_KEY}&msg={urllib.parse.quote(msg)}")


    def page_admin_questions(self, con, qs):
        if not self._require_admin(qs):
            return
        key = qs["key"][0]
        diff = qs.get("difficulty", [""])[0]
        topic = qs.get("topic", [""])[0]
        show = qs.get("show", ["active"])[0]

        counts = {d: con.execute(
            "SELECT COUNT(*) c FROM questions WHERE difficulty=? AND active=1",
            (d,)).fetchone()["c"] for d in LEVELS}
        topics = sorted({t for r in con.execute("SELECT topics_json FROM questions")
                         for t in json.loads(r["topics_json"])})

        out = []
        for r in con.execute("SELECT * FROM questions ORDER BY id"):
            qt = json.loads(r["topics_json"])
            if diff and r["difficulty"] != diff:
                continue
            if topic and topic not in qt:
                continue
            if show == "active" and not r["active"]:
                continue
            if show == "retired" and r["active"]:
                continue
            st = con.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(is_correct),0) c FROM attempt_questions "
                "WHERE question_id=? AND answered_at IS NOT NULL", (r["id"],)).fetchone()
            rate = f"{round(100 * st['c'] / st['n'])}%" if st["n"] else "—"
            action = ("Retire" if r["active"] else "Restore")
            out.append(
                f"<tr><td class='mono'>{r['id']}</td><td>{r['difficulty']}</td>"
                f"<td class='muted'>{html.escape(', '.join(qt))}</td>"
                f"<td>{r['qtype']}</td>"
                f"<td>{html.escape(r['prompt'][:64])}…</td>"
                f"<td class='mono'>{st['n']}</td><td class='mono'>{rate}</td>"
                f"<td class='right'><form method='post' action='/admin/questions/toggle'>"
                f"<input type='hidden' name='key' value='{key}'>"
                f"<input type='hidden' name='id' value='{r['id']}'>"
                f"<button class='btn ghost sm'>{action}</button></form></td></tr>")

        def opts(name, values, chosen):
            o = f'<option value="">All {name}</option>'
            for v in values:
                sel = " selected" if v == chosen else ""
                o += f'<option value="{v}"{sel}>{v}</option>'
            return o

        body = f"""{self._tabs(key, '/admin/questions')}{self._flash(qs)}
        <div class="stats">
        <div class="stat"><div class="n">{counts['easy']}</div><div class="l">Easy</div></div>
        <div class="stat"><div class="n">{counts['medium']}</div><div class="l">Medium</div></div>
        <div class="stat"><div class="n">{counts['hard']}</div><div class="l">Hard</div></div>
        <div class="stat"><div class="n">{sum(counts.values())}</div>
          <div class="l">Active total</div></div>
        </div>

        <div class="panel"><h3>Add a question</h3>
        <p class="sub">Appears in the pool immediately.</p>
        <form method="post" action="/admin/questions/add">
        <input type="hidden" name="key" value="{key}">
        <div class="field"><label class="lbl">Question</label>
        <textarea name="prompt" required style="min-height:70px"></textarea></div>
        <div class="grid2">
        <div class="field"><label class="lbl">Difficulty</label>
        <select name="difficulty">
        {''.join(f'<option>{html.escape(lv)}</option>' for lv in LEVELS)}</select></div>
        <div class="field"><label class="lbl">Topic</label>
        <select name="topic">{''.join(f'<option>{t}</option>' for t in topics)}</select></div>
        </div>
        <div class="grid2">
        <div class="field"><label class="lbl">Option 1</label>
        <input type="text" name="o1" required></div>
        <div class="field"><label class="lbl">Option 2</label>
        <input type="text" name="o2" required></div>
        <div class="field"><label class="lbl">Option 3</label>
        <input type="text" name="o3" required></div>
        <div class="field"><label class="lbl">Option 4</label>
        <input type="text" name="o4" required></div>
        </div>
        <div class="grid2">
        <div class="field"><label class="lbl">Correct option</label>
        <select name="answer"><option>1</option><option>2</option><option>3</option>
        <option>4</option></select></div>
        <div class="field"><label class="lbl">Explanation (admin only)</label>
        <input type="text" name="explanation"></div>
        </div>
        <button class="btn sm">Add question</button></form></div>

        <div class="panel"><h3>Upload a batch</h3>
        <p class="sub">CSV or JSON. Every row is checked before anything is saved —
        one bad row rejects the file and tells you which line.</p>
        <form method="post" action="/admin/questions/upload"
              enctype="multipart/form-data">
        <input type="hidden" name="key" value="{key}">
        <div class="field"><input type="file" name="file" accept=".csv,.json"></div>
        <div class="field"><label class="lbl">…or paste CSV / JSON here</label>
        <textarea name="pasted" placeholder="id,difficulty,topic,prompt,option1,option2,option3,option4,answer,explanation"></textarea></div>
        <button class="btn sm">Upload</button>
        <a class="btn ghost sm" href="/admin/questions/template.csv?key={key}"
           style="margin-left:8px">Download CSV template</a>
        <a class="btn ghost sm" href="/admin/questions/export.json?key={key}"
           style="margin-left:8px">Export bank</a>
        </form></div>

        <form method="get" action="/admin/questions" class="bar">
        <input type="hidden" name="key" value="{key}">
        <select name="difficulty">{opts('levels', LEVELS, diff)}</select>
        <select name="topic">{opts('topics', topics, topic)}</select>
        <select name="show">
        <option value="active"{' selected' if show == 'active' else ''}>Active</option>
        <option value="retired"{' selected' if show == 'retired' else ''}>Retired</option>
        <option value="all"{' selected' if show == 'all' else ''}>All</option></select>
        <button class="btn ghost sm">Filter</button>
        <span class="spacer"></span><span class="pill">{len(out)} shown</span></div>
        <div class="scroll-x">
        <table class="data"><tr><th>ID</th><th>Level</th><th>Topics</th><th>Type</th>
        <th>Prompt</th><th>Served</th><th>% correct</th><th></th></tr>
        {''.join(out) or '<tr><td colspan="8" class="muted">No questions match those filters.</td></tr>'}
        </table></div>"""
        self._send(page(body, "Admin", "Admin — questions", wide=True))

    def _next_qid(self, con):
        row = con.execute("SELECT id FROM questions ORDER BY id DESC LIMIT 1").fetchone()
        n = int(row["id"][1:]) + 1 if row and row["id"][1:].isdigit() else 1
        return f"Q{n:03d}"

    def act_question_add(self, con):
        f = self._form()
        if not admin_key_ok(f.get("key", "")):
            return self._send(page('<div class="card"><h1>Admin key required</h1></div>'),
                              403)
        try:
            options = [f.get(f"o{i}", "").strip() for i in range(1, 5)]
            if not all(options) or not f.get("prompt", "").strip():
                raise ValueError("every field is required")
            qid = self._next_qid(con)
            con.execute(
                "INSERT INTO questions(id,difficulty,qtype,prompt,options_json,"
                "answer_index,explanation,topics_json,tags_json,active) "
                "VALUES(?,?,'mcq',?,?,?,?,?,'[]',1)",
                (qid, f.get("difficulty", "easy"), f["prompt"].strip(),
                 json.dumps(options), int(f.get("answer", "1")) - 1,
                 f.get("explanation", "").strip(),
                 json.dumps([f.get("topic", "business")])))
            con.commit()
            msg = f"Added {qid}."
        except Exception as e:                     # noqa: BLE001
            msg = f"!Could not add the question: {e}"
        self._redirect(f"/admin/questions?key={ADMIN_KEY}&msg={urllib.parse.quote(msg)}")

    def _parse_multipart(self):
        """Minimal multipart/form-data reader (the stdlib cgi module is gone in 3.13)."""
        ctype = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length)
        if "boundary=" not in ctype:
            return {k: v[0] for k, v in
                    urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}
        boundary = ctype.split("boundary=")[1].strip('"').encode()
        fields = {}
        for part in raw.split(b"--" + boundary):
            if b"\r\n\r\n" not in part:
                continue
            head, body = part.split(b"\r\n\r\n", 1)
            head_s = head.decode("utf-8", "replace")
            if 'name="' not in head_s:
                continue
            name = head_s.split('name="')[1].split('"')[0]
            fields[name] = body.rstrip(b"\r\n-").decode("utf-8", "replace")
        return fields

    @staticmethod
    def parse_question_upload(text):
        """Return (rows, errors). Accepts a JSON array or a CSV with a header.

        CSV columns: id (optional), difficulty, topic, prompt,
                     option1..option4, answer (1-4), explanation (optional)
        """
        text = text.strip()
        rows, errors = [], []
        if not text:
            return rows, ["The file was empty."]
        if text[0] in "[{":
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                return [], [f"JSON could not be read: {e}"]
            data = data if isinstance(data, list) else [data]
            for i, q in enumerate(data, 1):
                try:
                    rows.append({
                        "id": q.get("id"),
                        "difficulty": q["difficulty"].strip().lower(),
                        "topics": q.get("topics") or [q.get("topic", "business")],
                        "prompt": q["prompt"].strip(),
                        "options": [str(o).strip() for o in q["options"]],
                        "answer_index": int(q["answer_index"]),
                        "explanation": q.get("explanation", ""),
                    })
                except Exception as e:             # noqa: BLE001
                    errors.append(f"Item {i}: {e}")
        else:
            reader = csv.DictReader(io.StringIO(text))
            for i, r in enumerate(reader, 2):      # line 1 is the header
                try:
                    r = {(k or "").strip().lower(): (v or "").strip()
                         for k, v in r.items()}
                    options = [r[f"option{n}"] for n in range(1, 5)]
                    answer = int(r["answer"])
                    if not 1 <= answer <= 4:
                        raise ValueError("answer must be 1-4")
                    rows.append({
                        "id": r.get("id") or None,
                        "difficulty": r["difficulty"].lower(),
                        "topics": [t.strip() for t in r.get("topic", "business").split("|")],
                        "prompt": r["prompt"],
                        "options": options,
                        "answer_index": answer - 1,
                        "explanation": r.get("explanation", ""),
                    })
                except Exception as e:             # noqa: BLE001
                    errors.append(f"Line {i}: {e}")
        for i, q in enumerate(rows, 1):
            if q["difficulty"] not in LEVELS:
                errors.append(f"Row {i}: difficulty must be one of "
                              f"{', '.join(LEVELS)}")
            if len(q["options"]) != 4 or not all(q["options"]):
                errors.append(f"Row {i}: exactly four non-empty options are required")
            if not q["prompt"]:
                errors.append(f"Row {i}: the question text is missing")
            if not 0 <= q["answer_index"] <= 3:
                errors.append(f"Row {i}: the correct option must be 1-4")
        return rows, errors

    def act_question_upload(self, con):
        f = self._parse_multipart()
        if not admin_key_ok(f.get("key", "")):
            return self._send(page('<div class="card"><h1>Admin key required</h1></div>'),
                              403)
        text = (f.get("file") or "").strip() or (f.get("pasted") or "")
        rows, errors = self.parse_question_upload(text)
        if errors:
            msg = f"!Nothing was saved. {len(errors)} problem(s): " + \
                  "; ".join(errors[:3]) + ("…" if len(errors) > 3 else "")
        else:
            added = 0
            for q in rows:
                qid = q["id"] or self._next_qid(con)
                con.execute(
                    "INSERT OR REPLACE INTO questions(id,difficulty,qtype,prompt,"
                    "options_json,answer_index,explanation,topics_json,tags_json,active)"
                    " VALUES(?,?,'mcq',?,?,?,?,?,'[]',1)",
                    (qid, q["difficulty"], q["prompt"], json.dumps(q["options"]),
                     q["answer_index"], q["explanation"], json.dumps(q["topics"])))
                added += 1
            con.commit()
            msg = f"Added {added} question{'s' if added != 1 else ''}."
        self._redirect(f"/admin/questions?key={ADMIN_KEY}&msg={urllib.parse.quote(msg)}")

    def act_question_toggle(self, con):
        f = self._form()
        if not admin_key_ok(f.get("key", "")):
            return self._send(page('<div class="card"><h1>Admin key required</h1></div>'),
                              403)
        qid = f.get("id", "")
        row = con.execute("SELECT active FROM questions WHERE id=?", (qid,)).fetchone()
        if row:
            new = 0 if row["active"] else 1
            con.execute("UPDATE questions SET active=? WHERE id=?", (new, qid))
            con.commit()
            msg = f"{qid} {'restored' if new else 'retired'}."
        else:
            msg = f"!{qid} not found."
        self._redirect(f"/admin/questions?key={ADMIN_KEY}&msg={urllib.parse.quote(msg)}")

    def admin_template(self, con, qs):
        if not self._require_admin(qs):
            return
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "difficulty", "topic", "prompt", "option1", "option2",
                    "option3", "option4", "answer", "explanation"])
        w.writerow(["", "easy", "marketing", "What does CAC stand for?",
                    "Customer Acquisition Cost", "Cost After Conversion",
                    "Customer Activity Count", "Channel Ad Charge", "1",
                    "CAC is the average cost to win one customer."])
        w.writerow(["", "hard", "finance|business",
                    "Fixed costs ₹90,000, price ₹45, variable ₹15. Breakeven volume?",
                    "2,000 units", "3,000 units", "6,000 units", "667 units", "2",
                    "90,000 / (45-15) = 3,000."])
        self._send(buf.getvalue(), 200, "text/csv")

    def admin_export_bank(self, con, qs):
        if not self._require_admin(qs):
            return
        out = []
        for r in con.execute("SELECT * FROM questions ORDER BY id"):
            out.append({"id": r["id"], "difficulty": r["difficulty"],
                        "topics": json.loads(r["topics_json"]), "type": r["qtype"],
                        "prompt": r["prompt"], "options": json.loads(r["options_json"]),
                        "answer_index": r["answer_index"],
                        "explanation": r["explanation"], "active": bool(r["active"])})
        self._send(json.dumps(out, indent=1), 200, "application/json")

    def admin_export(self, con, qs):
        if not self._require_admin(qs):
            return
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["student", "name", "attempt", "seq", "question", "difficulty",
                    "topic", "answer", "correct", "time_taken_s",
                    "decision", "next_difficulty"])
        for r in con.execute(
            "SELECT s.username, s.name, aq.*, q.topics_json FROM attempt_questions aq "
            "JOIN attempts a ON a.id=aq.attempt_id JOIN students s ON s.id=a.student_id "
            "JOIN questions q ON q.id=aq.question_id ORDER BY s.username, aq.seq"):
            w.writerow([r["username"], r["name"], r["attempt_id"], r["seq"],
                        r["question_id"], r["difficulty"],
                        json.loads(r["topics_json"])[0], r["answer_index"],
                        r["is_correct"],
                        round(r["time_taken"], 1) if r["time_taken"] else "",
                        r["decision"], r["next_difficulty"]])
        self._send(buf.getvalue(), 200, "text/csv")



class ExamServer(ThreadingHTTPServer):
    """Tuned for a whole cohort arriving at once.

    The stdlib default listen backlog is 5, which drops connections when ~120
    students press Sign in within the same second (observed as 'connection
    reset by peer' in loadtest.py). 256 absorbs the burst.
    """
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256


def main():
    if not os.path.exists(storage.DB_PATH):
        # Cloud deploys start with an empty volume — seed it once, automatically.
        if os.environ.get("AUTO_SEED", "1") != "0":
            print(f"No database at {storage.DB_PATH} — seeding it now.")
            argv = sys.argv
            n = os.environ.get("SEED_STUDENTS", "")
            sys.argv = ["seed.py"] + (["--students", n] if n.isdigit() else [])
            storage.main()
            sys.argv = argv
        else:
            print("No database found — run:  python3 seed.py --fresh")
            raise SystemExit(1)

    ip = lan_ip()
    print(f"\n{TITLE}")
    print(f"  Students (this machine): http://localhost:{PORT}/")
    if ip:
        print(f"  Students (same wifi):    http://{ip}:{PORT}/")
    print(f"  Admin:                   http://localhost:{PORT}/admin?key={ADMIN_KEY}")
    print(f"  Focus tracking: {'on' if PROCTOR_FOCUS else 'off'}")
    if ADMIN_KEY in ("change-me-admin", "mesa-admin-dev"):
        print("  !! ADMIN_KEY is still the default. Anyone who guesses it sees "
              "every answer and can edit the question bank.")
        print("     Set a real one before putting this on a public URL.")
    print()
    ExamServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
