"""Cohort load test — can this server handle your 120 students?

Usage:
  Terminal 1:  python3 seed.py --fresh --students 120 && python3 server.py
  Terminal 2:  python3 loadtest.py 120

Each simulated student logs in, reads instructions, starts, and answers all 30
questions as fast as the server allows — far harsher than real students, who
think for 20-60 seconds between questions. Reports request latency, errors,
and verifies no student ever saw a repeated question.
"""
import http.client
import json
import os
import sqlite3
import statistics
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from seed import env, env_int          # blank env vars count as unset

HOST = env("LOAD_HOST", "127.0.0.1")
PORT = env_int("PORT", 8000)
PASSWORD = env("DEFAULT_STUDENT_PASSWORD", "mesa-demo-2026")
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mesa.db")

lock = threading.Lock()
latencies = []
errors = []


def call(method, path, body=None, cookie=None):
    t0 = time.time()
    c = http.client.HTTPConnection(HOST, PORT, timeout=30)
    headers = {"Connection": "close"}
    if body is not None:
        body = urllib.parse.urlencode(body)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if cookie:
        headers["Cookie"] = f"mesa_session={cookie}"
    c.request(method, path, body, headers)
    r = c.getresponse()
    data = r.read()
    sc = r.getheader("Set-Cookie", "")
    c.close()
    dt = time.time() - t0
    with lock:
        latencies.append(dt)
    return r.status, data, sc


def answer_key(qid):
    con = sqlite3.connect(DB, timeout=20)
    row = con.execute("SELECT answer_index FROM questions WHERE id=?",
                      (qid,)).fetchone()
    con.close()
    return row[0]


def current_qid(username):
    con = sqlite3.connect(DB, timeout=20)
    row = con.execute(
        "SELECT a.current_question_id FROM attempts a JOIN students s "
        "ON s.id=a.student_id WHERE s.username=? AND a.status='active'",
        (username,)).fetchone()
    con.close()
    return row[0] if row else None


def one_student(username):
    seen = []
    try:
        status, _, sc = call("POST", "/login",
                             {"username": username, "password": PASSWORD})
        if status != 303:
            raise RuntimeError(f"login failed ({status})")
        token = sc.split("mesa_session=")[1].split(";")[0]
        call("GET", "/home", cookie=token)
        call("GET", "/instructions", cookie=token)
        call("POST", "/start", {}, cookie=token)
        for _ in range(30):
            qid = current_qid(username)
            if not qid:
                break
            call("GET", "/exam", cookie=token)
            status, _, _ = call("POST", "/answer",
                                {"qid": qid, "answer": str(answer_key(qid))},
                                cookie=token)
            if status not in (303, 409):
                raise RuntimeError(f"answer rejected ({status})")
            seen.append(qid)
        call("GET", "/done", cookie=token)
    except Exception as e:                       # noqa: BLE001
        with lock:
            errors.append(f"{username}: {e}")
    return username, seen


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    users = [f"student{i:03d}" for i in range(1, n + 1)]
    print(f"Driving {n} concurrent students against http://{HOST}:{PORT} …")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(one_student, users))
    elapsed = time.time() - t0

    repeats = [u for u, seen in results if len(seen) != len(set(seen))]
    finished = [u for u, seen in results if len(seen) == 30]
    lat = sorted(latencies)

    def pct(p):
        return lat[min(len(lat) - 1, int(len(lat) * p))] * 1000

    print(f"\n  students          {n}")
    print(f"  wall time         {elapsed:.1f}s")
    print(f"  requests          {len(lat)}")
    print(f"  completed 30 q    {len(finished)}/{n}")
    print(f"  repeated question {len(repeats)}  (must be 0)")
    print(f"  errors            {len(errors)}")
    print(f"  latency  median   {statistics.median(lat) * 1000:.0f} ms")
    print(f"           p95      {pct(0.95):.0f} ms")
    print(f"           max      {max(lat) * 1000:.0f} ms")
    for e in errors[:10]:
        print("   !", e)
    verdict = "PASS" if not errors and not repeats and len(finished) == n else "REVIEW"
    print(f"\n  verdict: {verdict}")
    print("  note: real students pause 20-60s per question, so this is a much "
          "heavier burst than exam day.")


if __name__ == "__main__":
    main()
