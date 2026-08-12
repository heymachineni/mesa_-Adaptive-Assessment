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
import socket
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import seb_support
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
ADMIN_KEY = os.environ.get("ADMIN_KEY", "change-me-admin")
PORT = int(os.environ.get("PORT", "8000"))
DURATION = CONFIG["exam"]["durationMinutes"] * 60
MAXQ = CONFIG["exam"]["maxQuestions"]
TITLE = CONFIG["exam"]["title"]
SESSION_MAX_AGE = 12 * 3600          # survives a closed browser / dead laptop

SEB_MODE = os.environ.get("SEB_MODE", "off").strip().lower()
SEB_BEK = os.environ.get("SEB_BROWSER_EXAM_KEY", "").strip()
SEB_CK = os.environ.get("SEB_CONFIG_KEY", "").strip()
SEB_QUIT_PASSWORD = os.environ.get("SEB_QUIT_PASSWORD", "").strip()
# Browser-level proctoring: log when the exam window loses focus. Works in
# every browser, needs no install. A deterrent and an audit trail — NOT
# lockdown. Students are told about it on the instructions screen.
PROCTOR_FOCUS = os.environ.get("PROCTOR_FOCUS", "on").strip().lower() != "off"

STYLE = """
<style>
:root{
  --ink:#16181c; --muted:#6b7280; --line:#e3e2dd; --paper:#ffffff;
  --bg:#f5f6f7; --green:#14402e; --green-soft:#eef3f0; --accent:#7357d2;
  --warn:#a8341f;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
  font-size:16px;line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:720px;margin:0 auto;padding:24px 20px 64px}
.topbar{display:flex;align-items:center;justify-content:space-between;
  padding:18px 0 22px}
.brand{font-weight:700;letter-spacing:.16em;font-size:13px;color:var(--green)}
.who{font-size:13px;color:var(--muted)}
.card{background:var(--paper);border:1px solid var(--line);border-radius:16px;
  padding:34px 32px}
@media(max-width:560px){.card{padding:24px 20px;border-radius:14px}}
h1{font-family:var(--serif);font-size:28px;line-height:1.25;margin:0 0 10px;
  font-weight:600;letter-spacing:-.01em}
h2{font-size:15px;margin:26px 0 10px;font-weight:600}
p{margin:0 0 14px}
.lede{color:var(--muted);margin-bottom:26px}
.rule{height:1px;background:var(--line);border:0;margin:26px 0}

/* progress rail: shows position only, never level */
.rail{display:flex;gap:3px;margin:0 0 6px}
.tick{flex:1;height:4px;border-radius:2px;background:#e6e6e2}
.tick.done{background:var(--green)}
.tick.now{background:var(--accent)}
.status{display:flex;justify-content:space-between;align-items:baseline;
  font-size:13px;color:var(--muted);margin-bottom:26px}
.clock{font-variant-numeric:tabular-nums;font-feature-settings:"tnum";
  letter-spacing:.02em}
.clock.low{color:var(--warn);font-weight:600}

.prompt{font-family:var(--serif);font-size:21px;line-height:1.45;
  margin:0 0 22px;letter-spacing:-.005em}
.opts{display:flex;flex-direction:column;gap:10px;margin-bottom:26px}
.opt{display:flex;gap:13px;align-items:flex-start;border:1px solid var(--line);
  border-radius:12px;padding:14px 16px;cursor:pointer;background:var(--paper);
  transition:border-color .12s,background .12s}
.opt:hover{border-color:#c9c7c0}
.opt input{position:absolute;opacity:0;pointer-events:none}
.key{flex:none;width:22px;height:22px;border-radius:6px;background:#f0efec;
  color:var(--muted);font-size:12px;font-weight:600;display:flex;
  align-items:center;justify-content:center;margin-top:1px}
.opt.sel{border-color:var(--green);background:var(--green-soft)}
.opt.sel .key{background:var(--green);color:#fff}
.opt:focus-within{outline:2px solid var(--accent);outline-offset:2px}

.btn{display:inline-flex;align-items:center;justify-content:center;
  background:var(--green);color:#fff;border:0;border-radius:11px;
  padding:14px 28px;font-size:16px;font-family:inherit;font-weight:500;
  cursor:pointer;min-height:48px}
.btn:hover{background:#1b5540}
.btn:disabled{background:#d6d5d0;color:#8b8a85;cursor:not-allowed}
.btn.wide{width:100%}
.btn.ghost{background:transparent;color:var(--muted);border:1px solid var(--line)}
.btn.ghost:hover{background:#faf9f7;color:var(--ink)}
.hint{font-size:13px;color:var(--muted);margin-top:12px;text-align:center}

.field{margin-bottom:16px}
label.lbl{display:block;font-size:13px;color:var(--muted);margin-bottom:6px}
input[type=text],input[type=password]{width:100%;padding:13px 14px;
  border:1px solid var(--line);border-radius:11px;font-size:16px;
  font-family:inherit;background:var(--paper);min-height:48px}
input:focus{outline:2px solid var(--accent);outline-offset:1px;border-color:transparent}
.err{background:#fdf1ee;border:1px solid #f0d4cc;color:var(--warn);
  border-radius:10px;padding:12px 14px;font-size:14px;margin-bottom:18px}

ol.steps{margin:0 0 8px;padding-left:20px}
ol.steps li{margin-bottom:10px;color:#33363c}
.note{background:#f7f7f5;border-radius:11px;padding:14px 16px;font-size:14px;
  color:var(--muted);margin:20px 0 0}

table.data{border-collapse:collapse;width:100%;font-size:14px;margin:14px 0}
table.data th,table.data td{border-bottom:1px solid var(--line);
  padding:9px 10px;text-align:left}
table.data th{font-size:12px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--muted);font-weight:600}
table.data tr:hover td{background:#fafaf8}
.rank{font-variant-numeric:tabular-nums;color:var(--muted);width:38px}
img.figure{max-width:100%;border:1px solid var(--line);border-radius:12px;
  margin:0 0 20px}
.done-mark{width:52px;height:52px;border-radius:50%;background:var(--green-soft);
  color:var(--green);display:flex;align-items:center;justify-content:center;
  font-size:26px;margin-bottom:20px}
a{color:var(--accent)}

/* ---- admin dashboard ---- */
.wide-wrap{max-width:1080px}
.tabs{display:flex;gap:2px;border-bottom:1px solid var(--line);margin-bottom:26px;
  overflow-x:auto}
.tabs a{padding:11px 16px;font-size:14px;color:var(--muted);text-decoration:none;
  border-bottom:2px solid transparent;white-space:nowrap}
.tabs a:hover{color:var(--ink)}
.tabs a.on{color:var(--green);border-bottom-color:var(--green);font-weight:600}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
  gap:1px;background:var(--line);border:1px solid var(--line);border-radius:14px;
  overflow:hidden;margin-bottom:28px}
.stat{background:var(--paper);padding:18px 20px}
.stat .n{font-size:27px;font-weight:600;letter-spacing:-.02em;
  font-variant-numeric:tabular-nums;line-height:1.1}
.stat .l{font-size:12px;color:var(--muted);margin-top:4px;letter-spacing:.02em}
.stat.alert .n{color:var(--warn)}
.bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:18px}
.bar select,.bar input[type=text]{padding:9px 11px;border:1px solid var(--line);
  border-radius:9px;font-size:14px;font-family:inherit;background:var(--paper);
  min-height:auto;width:auto}
.btn.sm{padding:9px 16px;min-height:auto;font-size:14px;border-radius:9px}
.spacer{flex:1}
.pill{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;
  background:#f0efec;color:var(--muted)}
.pill.live{background:#e8f2ec;color:var(--green)}
.pill.warn{background:#fdf1ee;color:var(--warn)}
.flash{border-radius:11px;padding:13px 16px;font-size:14px;margin-bottom:20px;
  background:var(--green-soft);color:var(--green)}
.flash.bad{background:#fdf1ee;color:var(--warn)}
.panel{border:1px solid var(--line);border-radius:14px;padding:22px;
  margin-bottom:20px;background:var(--paper)}
.panel h3{margin:0 0 4px;font-size:15px;font-weight:600}
.panel .sub{font-size:13px;color:var(--muted);margin-bottom:16px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:700px){.grid2{grid-template-columns:1fr}}
textarea{width:100%;padding:12px;border:1px solid var(--line);border-radius:11px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;
  min-height:120px;background:var(--paper)}
input[type=file]{font-size:14px;font-family:inherit}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px}
.muted{color:var(--muted)}
.right{text-align:right}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
</style>"""


def page(body, who="", title=None, wide=False):
    cls = "wrap wide-wrap" if wide else "wrap"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title or TITLE)}</title>{STYLE}</head><body><div class="{cls}">
<div class="topbar"><span class="brand">MESA</span>
<span class="who">{who}</span></div>{body}</div></body></html>"""


def fmt_clock(seconds):
    m, s = divmod(max(0, int(seconds)), 60)
    return f"{m}:{s:02d}"


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

    # -- SEB --
    def _request_url(self):
        return f"http://{self.headers.get('Host', f'localhost:{PORT}')}{self.path}"

    def _seb_status(self):
        return seb_support.verify_request(self._request_url(), self.headers,
                                          SEB_BEK, SEB_CK)

    def _seb_gate(self, path):
        if SEB_MODE != "enforce":
            return False
        if path.startswith("/admin") or path in ("/seb/config", "/seb/quit"):
            return False
        ok, method = self._seb_status()
        if ok:
            return False
        print(f"[seb] blocked {path} via {method}")
        body = f"""<div class="card"><h1>Safe Exam Browser required</h1>
        <p class="lede">This assessment can only be taken inside Safe Exam Browser.</p>
        <ol class="steps">
        <li>Download the exam file: <a href="/seb/config">mesa_exam.seb</a></li>
        <li>Install Safe Exam Browser if you haven't already.</li>
        <li>Open <b>mesa_exam.seb</b> — it launches the exam for you.</li></ol>
        <p class="note">Verification method in use: {html.escape(method)}.</p></div>"""
        self._send(page(body, title="Safe Exam Browser required"), 403)
        return True

    # -- routing --
    def do_GET(self):
        con = storage.connect()
        try:
            parsed = urllib.parse.urlparse(self.path)
            path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
            if self._seb_gate(path):
                return
            routes = {
                "/": lambda: self.page_login(),
                "/seb/config": lambda: self.serve_seb_config(),
                "/seb/quit": lambda: self._send(page(
                    '<div class="card"><h1>Session ended</h1>'
                    '<p class="lede">You can close this window.</p></div>')),
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
            if self._seb_gate(path):
                return
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

    def serve_seb_config(self):
        host = self.headers.get("Host", f"localhost:{PORT}")
        data = seb_support.build_seb_config(f"http://{host}/",
                                            f"http://{host}/seb/quit",
                                            SEB_QUIT_PASSWORD)
        self.send_response(200)
        self.send_header("Content-Type", "application/seb")
        self.send_header("Content-Disposition",
                         'attachment; filename="mesa_exam.seb"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- auth --
    def page_login(self, error=""):
        body = f"""<div class="card">
        <h1>{html.escape(TITLE)}</h1>
        <p class="lede">Sign in to begin.</p>
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
                                   "Check for stray spaces and try again.")
        if not row["active"]:
            return self.page_login("That account has been deactivated. "
                                   "Contact your administrator.")
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
            <p class="lede">Your answers are saved. The clock kept running.</p>
            <div class="rail">{self._rail(done, MAXQ)}</div>
            <div class="status"><span>{done} of {MAXQ} answered</span>
            <span class="clock">{left} left</span></div>
            <form method="post" action="/start">
            <button class="btn wide">Continue</button></form></div>"""
            return self._send(page(body, html.escape(s["name"])))
        if latest_attempt(con, s["id"]):
            return self._redirect("/done")
        return self._redirect("/instructions")

    def page_instructions(self, con):
        s = self._require_student(con)
        if not s:
            return
        proctor_line = ("<li>Leaving this window is recorded.</li>"
                        if PROCTOR_FOCUS else "")
        body = f"""<div class="card">
        <h1>Before you begin</h1>
        <p class="lede">{MAXQ} questions · {CONFIG['exam']['durationMinutes']} minutes</p>
        <ol class="steps">
        <li>One question at a time. No going back, no skipping.</li>
        <li>Answers lock when submitted.</li>
        <li>Progress saves automatically — you can close this and return.</li>
        <li>Questions adapt to your answers, so everyone's set differs.</li>
        {proctor_line}
        <li>Results are released by your coordinator.</li>
        </ol>
        <hr class="rule">
        <form method="post" action="/start">
        <button class="btn wide">Begin</button></form>
        <p class="hint">Your clock starts now.</p></div>"""
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
        <div class="rail">{self._rail(att['answered_count'], MAXQ)}</div>
        <div class="status"><span>Question {num} of {MAXQ}</span>
        <span class="clock" id="clock">{fmt_clock(remaining)}</span></div>
        <p class="prompt">{html.escape(q['prompt'])}</p>{figure}
        <form method="post" action="/answer" id="f">
        <input type="hidden" name="qid" value="{q['id']}">
        <div class="opts">{opts}</div>
        <button class="btn wide" id="sub" disabled>Submit answer</button></form>
        <p class="hint">1–4 to select · Enter to submit</p>
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
        seb_flag = None
        if SEB_MODE != "off":
            seb_flag = 1 if self._seb_status()[0] else 0
        now = time.time()
        con.execute(
            "UPDATE attempt_questions SET answered_at=?, answer_index=?, is_correct=?,"
            " time_taken=?, seb_verified=?, decision=?, next_difficulty=? "
            "WHERE attempt_id=? AND question_id=?",
            (now, ans, correct, now - row["shown_at"], seb_flag, decision,
             new_state["difficulty"], att["id"], qid))
        con.execute("UPDATE attempts SET adaptive_state_json=?, "
                    "answered_count=answered_count+1, score=score+?, "
                    "current_question_id=NULL WHERE id=?",
                    (json.dumps(new_state), correct, att["id"]))
        con.commit()
        print(f"[answer] {s['username']} attempt={att['id']} q={qid} "
              f"correct={correct} seb={seb_flag} t={now - row['shown_at']:.1f}s "
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
        expired = att and att["status"] == "expired"
        headline = "Time's up" if expired else "All done"
        lede = ("Answers submitted before the clock ran out were recorded."
                if expired else "Your responses have been recorded.")
        n = att["answered_count"] if att else 0
        body = f"""<div class="card">
        <div class="done-mark">✓</div>
        <h1>{headline}</h1>
        <p class="lede">{lede}</p>
        <div class="rail">{self._rail(n, MAXQ, current=False)}</div>
        <div class="status"><span>{n} of {MAXQ} answered</span><span></span></div>
        <p>Results are released by your coordinator.</p>
        <form method="post" action="/logout" style="margin-top:18px">
        <button class="btn ghost wide">Sign out</button></form></div>"""
        self._send(page(body, html.escape(s["name"])))
    # ---------------- admin ----------------
    def _require_admin(self, qs):
        if qs.get("key", [""])[0] != ADMIN_KEY:
            self._send(page('<div class="card"><h1>Admin key required</h1>'
                            '<p class="lede">Add <span class="mono">?key=…</span>'
                            ' to the URL.</p></div>', title="Admin"), 403)
            return False
        return True

    def _tabs(self, key, here):
        items = [("/admin", "Overview"), ("/admin/students", "Students"),
                 ("/admin/leaderboard", "Leaderboard"),
                 ("/admin/questions", "Questions")]
        links = "".join(
            f'<a href="{p}?key={key}"{" class=\'on\'" if p == here else ""}>{n}</a>'
            for p, n in items)
        return (f'<div class="tabs">{links}'
                f'<a href="/admin/export.csv?key={key}">Export results</a></div>')

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
            if r["status"] == "active" and r["deadline"] > now:
                state = f'<span class="pill live">{fmt_clock(r["deadline"] - now)} left</span>'
            else:
                state = f'<span class="pill">{r["status"]}</span>'
            flag = f'<span class="pill warn">{ev}</span>' if ev else ""
            trs += (f"<tr><td><a href='/admin/attempt?key={key}&id={r['id']}'>"
                    f"{html.escape(r['name'])}</a></td>"
                    f"<td>{state}</td>"
                    f"<td class='mono'>{r['answered_count']}/{MAXQ}</td>"
                    f"<td class='mono'>{pct}</td>"
                    f"<td>{json.loads(r['adaptive_state_json'])['difficulty']}</td>"
                    f"<td>{flag}</td></tr>")
        trs = trs or '<tr><td colspan="6" class="muted">No attempts yet.</td></tr>'

        seb_pill = (f'<span class="pill">SEB {SEB_MODE}</span>'
                    if SEB_MODE != "off" else "")
        focus_pill = ('<span class="pill">focus tracking on</span>'
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
        <div class="bar"><h2 style="margin:0">Attempts</h2><span class="spacer"></span>
        {seb_pill} {focus_pill}</div>
        <table class="data"><tr><th>Student</th><th>Status</th><th>Answered</th>
        <th>Accuracy</th><th>Level</th><th>Flags</th></tr>{trs}</table>"""
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
                    f"<td><span class='pill'>{r['status']}</span></td></tr>")
        trs = trs or '<tr><td colspan="6" class="muted">No answers yet.</td></tr>'
        body = f"""{self._tabs(key, '/admin/leaderboard')}
        <div class="bar"><h2 style="margin:0">Leaderboard</h2>
        <span class="spacer"></span>
        <span class="muted" style="font-size:13px">Admin only — students never
        see a score</span></div>
        <table class="data"><tr><th class="rank">#</th><th>Student</th>
        <th>Correct</th><th>Accuracy</th><th>Time</th><th>Status</th></tr>
        {trs}</table>"""
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
            mark = ("—" if r["is_correct"] is None
                    else ("✓" if r["is_correct"] else "✗"))
            seb = ("" if r["seb_verified"] is None
                   else ("<span class='pill live'>SEB</span>" if r["seb_verified"]
                         else "<span class='pill warn'>no SEB</span>"))
            move = r["decision"] or "—"
            move_html = (f"<b>{html.escape(move)}</b>"
                         if not move.startswith("stay") else
                         f"<span class='muted'>{html.escape(move)}</span>")
            trs += (f"<tr><td class='rank'>{r['seq']}</td>"
                    f"<td class='mono'>{r['question_id']}</td>"
                    f"<td>{r['difficulty']}</td>"
                    f"<td class='muted'>{html.escape(json.loads(r['topics_json'])[0])}</td>"
                    f"<td>{mark}</td><td>{seb}</td>"
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
        <div class="bar"><h2 style="margin:0">{html.escape(head['name'])}</h2>
        <span class="spacer"></span>
        <span class="pill">{head['status']}</span></div>
        <div class="stats">
        <div class="stat"><div class="n">{head['answered_count']}</div>
          <div class="l">Answered</div></div>
        <div class="stat"><div class="n">{head['score']}</div><div class="l">Correct</div></div>
        <div class="stat"><div class="n">{pct}</div><div class="l">Accuracy</div></div>
        <div class="stat"><div class="n">{json.loads(head['adaptive_state_json'])['difficulty']}</div>
          <div class="l">Current level</div></div>
        </div>
        <table class="data"><tr><th class="rank">#</th><th>Question</th>
        <th>Level</th><th>Topic</th><th>Correct</th><th></th>
        <th>State before</th><th>Decision</th><th>Selection</th></tr>{trs}</table>
        <p class="muted" style="font-size:13px">Window events — {html.escape(ev_line)}</p>"""
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
        <table class="data"><tr><th>Username</th><th>Name</th><th>Attempts</th>
        <th>Status</th><th></th></tr>{trs or '<tr><td colspan="5" class="muted">No students.</td></tr>'}
        </table>"""
        self._send(page(body, "Admin", "Admin — students", wide=True))

    def act_student_add(self, con):
        f = self._form()
        if f.get("key") != ADMIN_KEY:
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
        if f.get("key") != ADMIN_KEY:
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
        if f.get("key") != ADMIN_KEY:
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
            (d,)).fetchone()["c"] for d in ("easy", "medium", "hard")}
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
        <select name="difficulty" class="bar" style="width:100%;padding:12px;
        border:1px solid var(--line);border-radius:11px">
        <option>easy</option><option>medium</option><option>hard</option></select></div>
        <div class="field"><label class="lbl">Topic</label>
        <select name="topic" style="width:100%;padding:12px;border:1px solid var(--line);
        border-radius:11px">{''.join(f'<option>{t}</option>' for t in topics)}</select></div>
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
        <select name="answer" style="width:100%;padding:12px;border:1px solid var(--line);
        border-radius:11px"><option>1</option><option>2</option><option>3</option>
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
        <select name="difficulty">{opts('levels', ['easy', 'medium', 'hard'], diff)}</select>
        <select name="topic">{opts('topics', topics, topic)}</select>
        <select name="show">
        <option value="active"{' selected' if show == 'active' else ''}>Active</option>
        <option value="retired"{' selected' if show == 'retired' else ''}>Retired</option>
        <option value="all"{' selected' if show == 'all' else ''}>All</option></select>
        <button class="btn ghost sm">Filter</button>
        <span class="spacer"></span><span class="muted"
        style="font-size:13px">{len(out)} shown</span></div>
        <table class="data"><tr><th>ID</th><th>Level</th><th>Topics</th><th>Type</th>
        <th>Prompt</th><th>Served</th><th>% correct</th><th></th></tr>
        {''.join(out) or '<tr><td colspan="8" class="muted">Nothing matches.</td></tr>'}
        </table>"""
        self._send(page(body, "Admin", "Admin — questions", wide=True))

    def _next_qid(self, con):
        row = con.execute("SELECT id FROM questions ORDER BY id DESC LIMIT 1").fetchone()
        n = int(row["id"][1:]) + 1 if row and row["id"][1:].isdigit() else 1
        return f"Q{n:03d}"

    def act_question_add(self, con):
        f = self._form()
        if f.get("key") != ADMIN_KEY:
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
            if q["difficulty"] not in ("easy", "medium", "hard"):
                errors.append(f"Row {i}: difficulty must be easy, medium or hard")
            if len(q["options"]) != 4 or not all(q["options"]):
                errors.append(f"Row {i}: exactly four non-empty options are required")
            if not q["prompt"]:
                errors.append(f"Row {i}: the question text is missing")
            if not 0 <= q["answer_index"] <= 3:
                errors.append(f"Row {i}: the correct option must be 1-4")
        return rows, errors

    def act_question_upload(self, con):
        f = self._parse_multipart()
        if f.get("key") != ADMIN_KEY:
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
        if f.get("key") != ADMIN_KEY:
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
                    "topic", "answer", "correct", "time_taken_s", "seb_verified",
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
                        r["seb_verified"], r["decision"], r["next_difficulty"]])
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
        print("No database found — run:  python3 seed.py --fresh")
        raise SystemExit(1)
    ip = lan_ip()
    print(f"\n{TITLE}")
    print(f"  Students (this machine): http://localhost:{PORT}/")
    if ip:
        print(f"  Students (same wifi):    http://{ip}:{PORT}/")
    print(f"  Admin:                   http://localhost:{PORT}/admin?key={ADMIN_KEY}")
    print(f"  SEB mode: {SEB_MODE}   focus tracking: {'on' if PROCTOR_FOCUS else 'off'}")
    if SEB_MODE == "enforce" and not (SEB_BEK or SEB_CK):
        print("  ! SEB enforce is using the weak User-Agent check — paste a "
              "Browser Exam Key into .env for real verification.")
    print()
    ExamServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
