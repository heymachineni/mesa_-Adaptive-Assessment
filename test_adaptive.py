"""Automated tests for the MESA POC.  Run: python3 -m unittest -v

Covers the spec's test matrix where it applies to this standalone build:
adaptive transitions + boundaries, selection (non-repetition, difficulty,
topic fallback, exhausted pool), DB-level UNIQUE enforcement, and the
server's security boundaries (wrong-question submission, replay, expiry)
exercised over real HTTP against a temporary instance.
"""
import http.client
import json
import os
import random
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.parse

from adaptive_engine import AdaptiveEngine

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = json.load(open(os.path.join(BASE, "config.json")))
QUESTIONS = json.load(open(os.path.join(BASE, "questions.json")))
BANK = [{"id": q["id"], "difficulty": q["difficulty"], "topics": q["topics"]}
        for q in QUESTIONS]


def eng():
    return AdaptiveEngine(CONFIG)


class TestTransitions(unittest.TestCase):
    def drive(self, answers, engine=None):
        e = engine or eng()
        s = e.initial_state()
        decisions = []
        for ok in answers:
            s, d = e.record_answer(s, ok)
            decisions.append(d)
        return s, decisions

    def test_initial_state(self):
        self.assertEqual(eng().initial_state()["difficulty"], "easy")

    def test_easy_to_medium_after_3_correct(self):
        s, d = self.drive([True, True, True])
        self.assertEqual(s["difficulty"], "medium")
        self.assertIn("promote easy->medium", d[-1])

    def test_easy_stays_below_threshold(self):
        s, _ = self.drive([True, True])
        self.assertEqual(s["difficulty"], "easy")

    def test_wrong_resets_correct_streak_on_easy(self):
        s, _ = self.drive([True, True, False, True, True])
        self.assertEqual(s["difficulty"], "easy")   # streak broken; needs 3 again
        s, _ = self.drive([True, True, False, True, True, True])
        self.assertEqual(s["difficulty"], "medium")

    def test_medium_to_hard_after_2_correct(self):
        s, _ = self.drive([True] * 3 + [True, True])
        self.assertEqual(s["difficulty"], "hard")

    def test_hard_to_medium_after_1_wrong(self):
        s, d = self.drive([True] * 5 + [False])
        self.assertEqual(s["difficulty"], "medium")
        self.assertIn("demote hard->medium", d[-1])

    def test_medium_to_easy_after_2_wrong(self):
        s, _ = self.drive([True] * 3 + [False, False])
        self.assertEqual(s["difficulty"], "easy")

    def test_counters_reset_on_level_change(self):
        s, _ = self.drive([True, True, True])
        self.assertEqual((s["consecutive_correct"], s["consecutive_wrong"]), (0, 0))

    def test_repeated_wrong_on_easy_never_goes_lower(self):
        s, _ = self.drive([False] * 10)
        self.assertEqual(s["difficulty"], "easy")

    def test_repeated_correct_on_hard_stays_hard(self):
        s, _ = self.drive([True] * 20)
        self.assertEqual(s["difficulty"], "hard")

    def test_oscillation_hard_medium(self):
        # reach hard, then wrong (->medium), 2 correct (->hard), wrong (->medium)
        s, _ = self.drive([True] * 5 + [False] + [True, True] + [False])
        self.assertEqual(s["difficulty"], "medium")

    def test_configurable_thresholds(self):
        cfg = json.loads(json.dumps(CONFIG))
        cfg["adaptive"]["easyToMediumCorrectThreshold"] = 1
        e = AdaptiveEngine(cfg)
        s, _ = self.drive([True], engine=e)
        self.assertEqual(s["difficulty"], "medium")


class TestSelection(unittest.TestCase):
    def test_never_repeats_over_full_exam(self):
        e = eng()
        s, seen, served = e.initial_state(), set(), {}
        rng = random.Random(1)
        for _ in range(e.max_questions):
            q, _ = e.select_next(s, seen, BANK, served, rng)
            self.assertIsNotNone(q)
            self.assertNotIn(q["id"], seen)
            seen.add(q["id"])
            for t in q["topics"]:
                served[t] = served.get(t, 0) + 1
            s, _ = e.record_answer(s, True)

    def test_selects_target_difficulty_when_available(self):
        e = eng()
        q, dbg = e.select_next({"difficulty": "hard", "consecutive_correct": 0,
                                "consecutive_wrong": 0}, set(), BANK, {}, random.Random(2))
        self.assertEqual(q["difficulty"], "hard")
        self.assertTrue(dbg["ladder_step"].startswith("1:") or
                        dbg["ladder_step"].startswith("2:"))

    def test_topic_quota_prefers_underserved(self):
        e = eng()
        served = {t: 99 for t in e.topic_targets()}   # everything over quota...
        served["ai"] = 0                              # ...except ai
        q, dbg = e.select_next(e.initial_state(), set(), BANK, served, random.Random(3))
        self.assertIn("ai", q["topics"])
        self.assertIn("under_quota_topic(ai)", dbg["ladder_step"])

    def test_fallback_to_adjacent_difficulty(self):
        e = eng()
        bank = [q for q in BANK if q["difficulty"] != "easy"]   # no easy left
        q, dbg = e.select_next(e.initial_state(), set(), bank, {}, random.Random(4))
        self.assertEqual(q["difficulty"], "medium")
        self.assertTrue(dbg["ladder_step"].startswith("3:adjacent(medium)"))

    def test_exhausted_pool_returns_none(self):
        e = eng()
        seen = {q["id"] for q in BANK}
        q, dbg = e.select_next(e.initial_state(), seen, BANK, {}, random.Random(5))
        self.assertIsNone(q)
        self.assertEqual(dbg["ladder_step"], "5:pool_exhausted")

    def test_empty_bank_returns_none(self):
        q, _ = eng().select_next(eng().initial_state(), set(), [], {}, random.Random(6))
        self.assertIsNone(q)

    def test_blueprint_targets_sum_close_to_max(self):
        total = sum(eng().topic_targets().values())
        self.assertAlmostEqual(total, eng().max_questions, delta=3)


class TestDatabaseGuarantee(unittest.TestCase):
    def test_unique_attempt_question_constraint(self):
        con = sqlite3.connect(":memory:")
        import seed
        con.executescript(seed.SCHEMA)
        con.execute("INSERT INTO students(id,username,name,salt,pw_hash) "
                    "VALUES(1,'t','T','x','y')")
        con.execute("INSERT INTO questions(id,difficulty,qtype,prompt,options_json,"
                    "answer_index,explanation,topics_json,tags_json) "
                    "VALUES('Q1','easy','mcq','p','[]',0,'e','[]','[]')")
        con.execute("INSERT INTO attempts(id,student_id,started_at,deadline,status,"
                    "adaptive_state_json) VALUES('A1',1,0,999,'active','{}')")
        con.execute("INSERT INTO attempt_questions(attempt_id,question_id,seq,shown_at,"
                    "difficulty,state_before_json) VALUES('A1','Q1',1,0,'easy','{}')")
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO attempt_questions(attempt_id,question_id,seq,shown_at,"
                        "difficulty,state_before_json) VALUES('A1','Q1',2,0,'easy','{}')")


class TestServerSecurity(unittest.TestCase):
    """Boots the real server on a temp DB and attacks it over HTTP."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        import seed
        import server
        cls.seed, cls.server_mod = seed, server
        cls.orig_db = seed.DB_PATH
        seed.DB_PATH = os.path.join(cls.tmp, "test.db")
        os.environ["DEFAULT_STUDENT_PASSWORD"] = "testpw"
        import sys
        sys.argv = ["seed.py"]
        seed.main()
        cls.port = 8765
        from http.server import ThreadingHTTPServer
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", cls.port), server.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.seed.DB_PATH = cls.orig_db

    def req(self, method, path, body=None, cookie=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if body is not None:
            body = urllib.parse.urlencode(body)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if cookie:
            headers["Cookie"] = f"mesa_session={cookie}"
        c.request(method, path, body, headers)
        r = c.getresponse()
        data = r.read().decode()
        setcookie = r.getheader("Set-Cookie", "")
        c.close()
        return r.status, data, setcookie

    def login(self, user="chandu"):
        status, _, sc = self.req("POST", "/login",
                                 {"username": user, "password": "testpw"})
        self.assertEqual(status, 303)
        token = sc.split("mesa_session=")[1].split(";")[0]
        return token

    def current_qid(self, token):
        con = sqlite3.connect(self.seed.DB_PATH)
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT a.current_question_id q FROM attempts a JOIN sessions s "
            "ON s.student_id=a.student_id WHERE s.token=? AND a.status='active'",
            (token,)).fetchone()
        con.close()
        return row["q"] if row else None

    def test_flow_and_boundaries(self):
        token = self.login()
        # bad password rejected
        status, body, _ = self.req("POST", "/login",
                                   {"username": "chandu", "password": "wrong"})
        self.assertIn("don&#x27;t match", body)
        # exam requires session
        status, _, _ = self.req("GET", "/exam")
        self.assertEqual(status, 303)     # bounced to login
        # start attempt
        self.req("POST", "/start", {}, cookie=token)
        qid = self.current_qid(token)
        self.assertIsNotNone(qid)
        # student page leaks nothing sensitive
        status, page_html, _ = self.req("GET", "/exam", cookie=token)
        for forbidden in ("EASY", "MEDIUM", "HARD", "difficulty", "answer_index",
                          "explanation", "topic"):
            self.assertNotIn(forbidden, page_html)
        # SECURITY: submitting an answer for a NON-assigned question -> 409
        other = "Q090" if qid != "Q090" else "Q089"
        status, _, _ = self.req("POST", "/answer",
                                {"qid": other, "answer": "0"}, cookie=token)
        self.assertEqual(status, 409)
        # legit answer accepted
        status, _, _ = self.req("POST", "/answer",
                                {"qid": qid, "answer": "1"}, cookie=token)
        self.assertEqual(status, 303)
        # SECURITY: replaying the same question -> not re-scored, redirected
        status, _, _ = self.req("POST", "/answer",
                                {"qid": qid, "answer": "2"}, cookie=token)
        self.assertIn(status, (303, 409))
        con = sqlite3.connect(self.seed.DB_PATH)
        n = con.execute("SELECT COUNT(*) FROM attempt_questions WHERE question_id=? "
                        "AND answered_at IS NOT NULL", (qid,)).fetchone()[0]
        con.close()
        self.assertEqual(n, 1)
        # next question assigned and differs
        qid2 = self.current_qid(token)
        self.assertIsNotNone(qid2)
        self.assertNotEqual(qid, qid2)

    def test_timer_is_server_side(self):
        token = self.login("student001")
        self.req("POST", "/start", {}, cookie=token)
        con = sqlite3.connect(self.seed.DB_PATH)
        con.execute("UPDATE attempts SET deadline=? WHERE student_id="
                    "(SELECT id FROM students WHERE username='student001')",
                    (time.time() - 1,))
        con.commit()
        con.close()
        qid = None  # client "still has" the question, but server clock has expired
        status, _, _ = self.req("GET", "/exam", cookie=token)
        self.assertEqual(status, 303)     # pushed to /done — client can't extend time
        con = sqlite3.connect(self.seed.DB_PATH)
        st = con.execute("SELECT status FROM attempts WHERE student_id="
                         "(SELECT id FROM students WHERE username='student001')"
                         ).fetchone()[0]
        con.close()
        self.assertEqual(st, "expired")


if __name__ == "__main__":
    unittest.main()
