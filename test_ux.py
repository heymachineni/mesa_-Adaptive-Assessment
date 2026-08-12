"""UX and cohort tests.  Run: python3 -m unittest test_ux -v

These prove the promises made to students and coordinators:
  - refresh / closed browser / dead laptop resumes on the same question
  - the student never sees a score, correct answer, or difficulty — including
    on the final screen
  - the exam works with JavaScript disabled (plain form posts)
  - window-focus events are recorded for the attempt
  - the admin leaderboard ranks by correct answers
  - many students can sit the exam at once without interfering
"""
import http.client
import os
import re
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

PORT = 8767
_STATE = {}


def setUpModule():
    import sys
    import tempfile
    from http.server import ThreadingHTTPServer
    import seed
    import server
    tmp = tempfile.mkdtemp()
    _STATE["seed"], _STATE["server"] = seed, server
    _STATE["orig_db"] = seed.DB_PATH
    seed.DB_PATH = os.path.join(tmp, "ux.db")
    os.environ["DEFAULT_STUDENT_PASSWORD"] = "testpw"
    sys.argv = ["seed.py", "--students", "20"]
    seed.main()
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
    _STATE["httpd"] = httpd
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.2)


def tearDownModule():
    _STATE["httpd"].shutdown()
    _STATE["seed"].DB_PATH = _STATE["orig_db"]


class Base(unittest.TestCase):
    @property
    def seed(self):
        return _STATE["seed"]

    @property
    def server(self):
        return _STATE["server"]

    def req(self, method, path, body=None, cookie=None):
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        headers = {"Connection": "close"}
        if body is not None:
            body = urllib.parse.urlencode(body)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if cookie:
            headers["Cookie"] = f"mesa_session={cookie}"
        c.request(method, path, body, headers)
        r = c.getresponse()
        data = r.read().decode()
        sc, loc = r.getheader("Set-Cookie", ""), r.getheader("Location", "")
        c.close()
        return r.status, data, sc, loc

    def login(self, user):
        status, _, sc, _ = self.req("POST", "/login",
                                    {"username": user, "password": "testpw"})
        self.assertEqual(status, 303)
        return sc.split("mesa_session=")[1].split(";")[0]

    def db(self):
        con = sqlite3.connect(self.seed.DB_PATH, timeout=15)
        con.row_factory = sqlite3.Row
        return con

    def current_qid(self, user):
        con = self.db()
        row = con.execute(
            "SELECT a.current_question_id q FROM attempts a JOIN students s "
            "ON s.id=a.student_id WHERE s.username=? AND a.status='active'",
            (user,)).fetchone()
        con.close()
        return row["q"] if row else None

    def answer_one(self, user, token, correct=True):
        qid = self.current_qid(user)
        con = self.db()
        idx = con.execute("SELECT answer_index i FROM questions WHERE id=?",
                          (qid,)).fetchone()["i"]
        con.close()
        pick = idx if correct else (idx + 1) % 4
        status, _, _, _ = self.req("POST", "/answer",
                                   {"qid": qid, "answer": str(pick)}, cookie=token)
        self.assertEqual(status, 303)
        return qid


class TestSessionPersistence(Base):
    def test_cookie_survives_browser_close(self):
        """Max-Age set => cookie is persistent, not session-only."""
        _, _, sc, _ = self.req("POST", "/login",
                               {"username": "student001", "password": "testpw"})
        self.assertIn("Max-Age=", sc)
        self.assertIn("HttpOnly", sc)
        age = int(re.search(r"Max-Age=(\d+)", sc).group(1))
        self.assertGreaterEqual(age, 3600)

    def test_refresh_returns_same_question(self):
        token = self.login("student002")
        self.req("POST", "/start", {}, cookie=token)
        qid = self.current_qid("student002")
        for _ in range(3):                       # three refreshes
            status, body, _, _ = self.req("GET", "/exam", cookie=token)
            self.assertEqual(status, 200)
            self.assertIn(f'value="{qid}"', body)
        self.assertEqual(self.current_qid("student002"), qid)

    def test_relogin_after_dead_laptop_resumes_same_question(self):
        token = self.login("student003")
        self.req("POST", "/start", {}, cookie=token)
        self.answer_one("student003", token)
        qid_before = self.current_qid("student003")
        # simulate: laptop dies, student signs in again later (new session)
        token2 = self.login("student003")
        self.assertNotEqual(token, token2)
        status, body, _, _ = self.req("GET", "/home", cookie=token2)
        self.assertIn("Welcome back", body)
        self.assertIn(">Continue<", body)
        self.assertIn("1 of 30 answered", body)
        status, _, _, loc = self.req("POST", "/start", {}, cookie=token2)
        self.assertEqual(loc, "/exam")
        self.assertEqual(self.current_qid("student003"), qid_before)
        status, body, _, _ = self.req("GET", "/exam", cookie=token2)
        self.assertIn(f'value="{qid_before}"', body)
        # the old session still works too — no lockout
        status, body, _, _ = self.req("GET", "/exam", cookie=token)
        self.assertEqual(status, 200)

    def test_resume_does_not_extend_the_clock(self):
        token = self.login("student004")
        self.req("POST", "/start", {}, cookie=token)
        con = self.db()
        dl1 = con.execute(
            "SELECT a.deadline d FROM attempts a JOIN students s ON s.id=a.student_id "
            "WHERE s.username='student004'").fetchone()["d"]
        con.close()
        time.sleep(0.05)
        self.req("POST", "/start", {}, cookie=self.login("student004"))
        con = self.db()
        dl2 = con.execute(
            "SELECT a.deadline d FROM attempts a JOIN students s ON s.id=a.student_id "
            "WHERE s.username='student004'").fetchone()["d"]
        con.close()
        self.assertEqual(dl1, dl2)


class TestStudentSeesNoResults(Base):
    FORBIDDEN = ("easy", "medium", "hard", "difficulty", "explanation",
                 "answer_index", "correct", "score")

    def assert_clean(self, body, where):
        low = body.lower()
        # strip the stylesheet/script (contains words like 'correct' never shown)
        visible = re.sub(r"<style>.*?</style>|<script>.*?</script>", "", low,
                         flags=re.S)
        for word in self.FORBIDDEN:
            self.assertNotIn(word, visible, f"'{word}' leaked on {where}")

    def test_exam_and_done_screens_are_clean(self):
        token = self.login("student005")
        self.req("POST", "/start", {}, cookie=token)
        status, body, _, _ = self.req("GET", "/exam", cookie=token)
        self.assert_clean(body, "exam screen")
        for _ in range(3):
            self.answer_one("student005", token, correct=True)
        status, body, _, _ = self.req("GET", "/exam", cookie=token)
        self.assert_clean(body, "exam screen after promotion")
        # force the attempt to finish, then check the final screen
        con = self.db()
        con.execute("UPDATE attempts SET status='completed' WHERE student_id="
                    "(SELECT id FROM students WHERE username='student005')")
        con.commit()
        con.close()
        status, body, _, _ = self.req("GET", "/done", cookie=token)
        self.assertEqual(status, 200)
        self.assert_clean(body, "done screen")
        self.assertIn("released by your coordinator", body)

    def test_instructions_screen_sets_expectations(self):
        token = self.login("student006")
        status, body, _, _ = self.req("GET", "/instructions", cookie=token)
        self.assertEqual(status, 200)
        for promise in ("30 questions", "No going back", "saves automatically",
                        "released by your coordinator"):
            self.assertIn(promise, body)


class TestNoJavaScriptFallback(Base):
    def test_plain_form_post_completes_a_question(self):
        """Everything works with JS off: form action + method are real."""
        token = self.login("student007")
        self.req("POST", "/start", {}, cookie=token)
        status, body, _, _ = self.req("GET", "/exam", cookie=token)
        self.assertIn('<form method="post" action="/answer"', body)
        self.assertIn('type="radio"', body)          # native controls, not divs
        qid = self.answer_one("student007", token)   # posted without any JS
        self.assertNotEqual(self.current_qid("student007"), qid)


class TestProctorEvents(Base):
    def test_focus_loss_is_recorded(self):
        token = self.login("student008")
        self.req("POST", "/start", {}, cookie=token)
        for kind in ("blur", "return", "blur"):
            status, _, _, _ = self.req("POST", "/event", {"kind": kind}, cookie=token)
            self.assertEqual(status, 204)
        con = self.db()
        n = con.execute(
            "SELECT COUNT(*) c FROM events e JOIN attempts a ON a.id=e.attempt_id "
            "JOIN students s ON s.id=a.student_id "
            "WHERE s.username='student008' AND e.kind='blur'").fetchone()["c"]
        con.close()
        self.assertEqual(n, 2)


class TestLeaderboard(Base):
    def test_ranks_by_correct_answers(self):
        strong, weak = self.login("student009"), self.login("student010")
        self.req("POST", "/start", {}, cookie=strong)
        self.req("POST", "/start", {}, cookie=weak)
        for _ in range(4):
            self.answer_one("student009", strong, correct=True)
        for _ in range(4):
            self.answer_one("student010", weak, correct=False)
        key = self.server.ADMIN_KEY
        status, body, _, _ = self.req("GET", f"/admin/leaderboard?key={key}")
        self.assertEqual(status, 200)
        self.assertLess(body.index("Student 009"), body.index("Student 010"))
        self.assertIn("<b>4</b>/4", body)      # strong student's correct count
        status, _, _, _ = self.req("GET", "/admin/leaderboard?key=wrong")
        self.assertEqual(status, 403)            # never public


class TestConcurrentCohort(Base):
    def test_twelve_students_at_once_stay_independent(self):
        users = [f"student{i:03d}" for i in range(11, 17)]

        def run(user):
            token = self.login(user)
            self.req("POST", "/start", {}, cookie=token)
            seen = []
            for _ in range(5):
                seen.append(self.answer_one(user, token, correct=True))
            return user, seen

        with ThreadPoolExecutor(max_workers=len(users)) as pool:
            results = dict(pool.map(run, users))
        for user, seen in results.items():
            self.assertEqual(len(seen), len(set(seen)),
                             f"{user} saw a repeated question")
        con = self.db()
        for user in users:
            row = con.execute(
                "SELECT a.answered_count n, a.score s FROM attempts a "
                "JOIN students st ON st.id=a.student_id WHERE st.username=?",
                (user,)).fetchone()
            self.assertEqual(row["n"], 5)
            self.assertEqual(row["s"], 5)        # all answered correctly
        con.close()


if __name__ == "__main__":
    unittest.main()
