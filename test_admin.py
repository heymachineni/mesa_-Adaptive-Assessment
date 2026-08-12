"""Admin question-manager tests.  Run: python3 -m unittest test_admin -v

Covers what a coordinator actually does: add one question, upload a batch as
CSV or JSON, get a clear rejection when a file is malformed, retire a question
so it stops being served without losing past results, and the fact that none
of this is reachable without the admin key.
"""
import http.client
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.parse

PORT = 8768
_S = {}


def setUpModule():
    import sys
    from http.server import ThreadingHTTPServer
    import seed
    import server
    tmp = tempfile.mkdtemp()
    _S["seed"], _S["server"] = seed, server
    _S["orig"] = seed.DB_PATH
    seed.DB_PATH = os.path.join(tmp, "admin.db")
    os.environ["DEFAULT_STUDENT_PASSWORD"] = "testpw"
    sys.argv = ["seed.py"]
    seed.main()
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
    _S["httpd"] = httpd
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.2)


def tearDownModule():
    _S["httpd"].shutdown()
    _S["seed"].DB_PATH = _S["orig"]


class Base(unittest.TestCase):
    @property
    def key(self):
        return _S["server"].ADMIN_KEY

    def req(self, method, path, body=None, ctype=None):
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        headers = {"Connection": "close"}
        if body is not None and ctype is None:
            body = urllib.parse.urlencode(body)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif ctype:
            headers["Content-Type"] = ctype
        c.request(method, path, body, headers)
        r = c.getresponse()
        data = r.read().decode()
        loc = r.getheader("Location", "")
        c.close()
        return r.status, data, loc

    def multipart(self, path, fields):
        b = "----MESATEST"
        parts = []
        for name, value in fields.items():
            parts.append(f"--{b}\r\nContent-Disposition: form-data; name=\"{name}\"")
            if name == "file":
                parts[-1] += '; filename="q.csv"\r\nContent-Type: text/csv'
            parts[-1] += f"\r\n\r\n{value}\r\n"
        payload = "".join(parts) + f"--{b}--\r\n"
        return self.req("POST", path, payload,
                        f"multipart/form-data; boundary={b}")

    def db(self):
        con = sqlite3.connect(_S["seed"].DB_PATH, timeout=15)
        con.row_factory = sqlite3.Row
        return con

    def count_active(self):
        con = self.db()
        n = con.execute("SELECT COUNT(*) c FROM questions WHERE active=1").fetchone()["c"]
        con.close()
        return n


class TestAccessControl(Base):
    def test_every_admin_route_needs_the_key(self):
        for path in ("/admin", "/admin/leaderboard", "/admin/questions",
                     "/admin/export.csv", "/admin/questions/template.csv",
                     "/admin/questions/export.json"):
            status, _, _ = self.req("GET", path)
            self.assertEqual(status, 403, path)
            status, _, _ = self.req("GET", f"{path}?key=nope")
            self.assertEqual(status, 403, path)

    def test_write_routes_reject_a_bad_key(self):
        before = self.count_active()
        status, _, _ = self.req("POST", "/admin/questions/add",
                                {"key": "nope", "prompt": "x", "o1": "a", "o2": "b",
                                 "o3": "c", "o4": "d", "answer": "1"})
        self.assertEqual(status, 403)
        self.assertEqual(self.count_active(), before)


class TestAddQuestion(Base):
    def test_add_one_question_appears_in_the_pool(self):
        before = self.count_active()
        status, _, loc = self.req("POST", "/admin/questions/add", {
            "key": self.key, "prompt": "Which metric best shows retention?",
            "difficulty": "medium", "topic": "product",
            "o1": "Downloads", "o2": "Week-4 return rate", "o3": "Page views",
            "o4": "Email opens", "answer": "2",
            "explanation": "Returning users are the retention signal."})
        self.assertEqual(status, 303)
        self.assertIn("Added", urllib.parse.unquote(loc))
        self.assertEqual(self.count_active(), before + 1)
        con = self.db()
        row = con.execute("SELECT * FROM questions ORDER BY id DESC LIMIT 1").fetchone()
        con.close()
        self.assertEqual(row["difficulty"], "medium")
        self.assertEqual(row["answer_index"], 1)          # option 2 -> index 1
        self.assertEqual(len(json.loads(row["options_json"])), 4)
        self.assertEqual(row["active"], 1)

    def test_missing_field_is_rejected_with_a_message(self):
        before = self.count_active()
        status, _, loc = self.req("POST", "/admin/questions/add", {
            "key": self.key, "prompt": "Incomplete", "o1": "a", "o2": "",
            "o3": "c", "o4": "d", "answer": "1"})
        self.assertEqual(status, 303)
        self.assertIn("Could not add", urllib.parse.unquote(loc))
        self.assertEqual(self.count_active(), before)


class TestUploadParsing(Base):
    """Parser-level checks — no HTTP needed."""

    def parse(self, text):
        return _S["server"].Handler.parse_question_upload(text)

    def test_csv_happy_path(self):
        csv_text = (
            "id,difficulty,topic,prompt,option1,option2,option3,option4,answer,explanation\n"
            ",easy,marketing,What does CAC mean?,Customer Acquisition Cost,"
            "Cost After Conversion,Customer Activity Count,Channel Ad Charge,1,Cost per customer\n"
            ",hard,finance|business,Breakeven at 90k fixed?,2000,3000,6000,667,2,Simple division\n")
        rows, errors = self.parse(csv_text)
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["answer_index"], 0)
        self.assertEqual(rows[1]["topics"], ["finance", "business"])

    def test_json_happy_path(self):
        rows, errors = self.parse(json.dumps([{
            "difficulty": "hard", "topics": ["ai"], "prompt": "What is RAG for?",
            "options": ["a", "b", "c", "d"], "answer_index": 2}]))
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["answer_index"], 2)

    def test_bad_rows_are_reported_with_line_numbers(self):
        rows, errors = self.parse(
            "difficulty,topic,prompt,option1,option2,option3,option4,answer\n"
            "impossible,ai,Bad level,a,b,c,d,1\n"
            "easy,ai,Bad answer,a,b,c,d,9\n")
        self.assertTrue(errors)
        self.assertTrue(any("Line 3" in e or "answer" in e.lower() for e in errors))

    def test_empty_input_is_reported(self):
        rows, errors = self.parse("   ")
        self.assertEqual(rows, [])
        self.assertTrue(errors)

    def test_broken_json_is_reported_not_crashed(self):
        rows, errors = self.parse('[{"difficulty": "easy",]')
        self.assertEqual(rows, [])
        self.assertIn("JSON", errors[0])


class TestUploadOverHTTP(Base):
    def test_csv_upload_adds_questions(self):
        before = self.count_active()
        csv_text = (
            "difficulty,topic,prompt,option1,option2,option3,option4,answer\n"
            "easy,sales,Upload test one,a,b,c,d,1\n"
            "medium,sales,Upload test two,a,b,c,d,3\n")
        status, _, loc = self.multipart("/admin/questions/upload",
                                        {"key": self.key, "file": csv_text})
        self.assertEqual(status, 303)
        self.assertIn("Added 2", urllib.parse.unquote(loc))
        self.assertEqual(self.count_active(), before + 2)

    def test_pasted_json_works_too(self):
        before = self.count_active()
        payload = json.dumps([{"difficulty": "easy", "topics": ["ai"],
                               "prompt": "Pasted json question",
                               "options": ["a", "b", "c", "d"], "answer_index": 0}])
        status, _, loc = self.multipart("/admin/questions/upload",
                                        {"key": self.key, "pasted": payload})
        self.assertEqual(status, 303)
        self.assertEqual(self.count_active(), before + 1)

    def test_bad_file_saves_nothing(self):
        before = self.count_active()
        status, _, loc = self.multipart("/admin/questions/upload", {
            "key": self.key,
            "file": ("difficulty,topic,prompt,option1,option2,option3,option4,answer\n"
                     "wrong,ai,Bad,a,b,c,d,1\n")})
        self.assertEqual(status, 303)
        self.assertIn("Nothing was saved", urllib.parse.unquote(loc))
        self.assertEqual(self.count_active(), before)


class TestRetire(Base):
    def test_retire_removes_from_pool_but_keeps_the_row(self):
        con = self.db()
        qid = con.execute("SELECT id FROM questions WHERE active=1 "
                          "ORDER BY id LIMIT 1").fetchone()["id"]
        con.close()
        before = self.count_active()
        status, _, loc = self.req("POST", "/admin/questions/toggle",
                                  {"key": self.key, "id": qid})
        self.assertEqual(status, 303)
        self.assertIn("retired", urllib.parse.unquote(loc))
        self.assertEqual(self.count_active(), before - 1)
        con = self.db()
        row = con.execute("SELECT active FROM questions WHERE id=?", (qid,)).fetchone()
        con.close()
        self.assertIsNotNone(row)                      # history preserved
        self.assertEqual(row["active"], 0)
        # the engine must not serve it
        import server
        con = self.db()
        pool = [q["id"] for q in server.all_questions(con)]
        con.close()
        self.assertNotIn(qid, pool)
        # restore
        self.req("POST", "/admin/questions/toggle", {"key": self.key, "id": qid})
        self.assertEqual(self.count_active(), before)


class TestTemplatesAndDashboard(Base):
    def test_csv_template_downloads_and_round_trips(self):
        status, body, _ = self.req("GET", f"/admin/questions/template.csv?key={self.key}")
        self.assertEqual(status, 200)
        self.assertIn("difficulty", body.splitlines()[0])
        rows, errors = _S["server"].Handler.parse_question_upload(body)
        self.assertEqual(errors, [])                   # our own template validates
        self.assertEqual(len(rows), 2)

    def test_bank_export_is_valid_json(self):
        status, body, _ = self.req("GET", f"/admin/questions/export.json?key={self.key}")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(all("prompt" in q and "options" in q for q in data))

    def test_dashboard_renders_stats(self):
        status, body, _ = self.req("GET", f"/admin?key={self.key}")
        self.assertEqual(status, 200)
        for label in ("In progress", "Finished", "Cohort accuracy", "Left the window"):
            self.assertIn(label, body)


if __name__ == "__main__":
    unittest.main()
