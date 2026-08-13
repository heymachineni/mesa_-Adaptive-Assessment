"""Student management tests.  Run: python3 -m unittest test_students -v

Covers what a coordinator does with students: add new ones, reset passwords,
deactivate (they can't sign in anymore), and verify that none of this leaks
without the admin key.
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

PORT = 8769
_S = {}


def setUpModule():
    import sys
    from http.server import ThreadingHTTPServer
    import seed
    import server
    tmp = tempfile.mkdtemp()
    _S["seed"], _S["server"] = seed, server
    _S["orig"] = seed.DB_PATH
    seed.DB_PATH = os.path.join(tmp, "students.db")
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


def _admin_cookie():
    seed = _S["seed"]
    username, password = seed.ADMINS[0][0], seed.ADMINS[0][2]
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    c.request("POST", "/admin/login",
              urllib.parse.urlencode({"username": username,
                                      "password": password}),
              {"Content-Type": "application/x-www-form-urlencoded",
               "Connection": "close"})
    r = c.getresponse()
    r.read()
    raw = r.getheader("Set-Cookie", "")
    c.close()
    assert "mesa_admin=" in raw, f"admin sign-in failed: {r.status}"
    return "mesa_admin=" + raw.split("mesa_admin=", 1)[1].split(";", 1)[0]


class Base(unittest.TestCase):
    def req(self, method, path, body=None, auth=True):
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        headers = {"Connection": "close"}
        if auth:
            headers["Cookie"] = _S.setdefault("cookie", _admin_cookie())
        if body is not None:
            body = urllib.parse.urlencode(body)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        c.request(method, path, body, headers)
        r = c.getresponse()
        data = r.read().decode()
        loc = r.getheader("Location", "")
        c.close()
        return r.status, data, loc

    def db(self):
        con = sqlite3.connect(_S["seed"].DB_PATH, timeout=15)
        con.row_factory = sqlite3.Row
        return con

    def count_students(self):
        con = self.db()
        n = con.execute("SELECT COUNT(*) c FROM students").fetchone()["c"]
        con.close()
        return n


class TestAccessControl(Base):
    def test_students_page_needs_a_session(self):
        status, _, loc = self.req("GET", "/admin/students", auth=False)
        self.assertEqual(status, 303)
        self.assertEqual(loc, "/admin/login")
        status, _, loc = self.req("GET", "/admin/students?key=nope", auth=False)
        self.assertEqual(status, 303)

    def test_add_needs_a_session(self):
        before = self.count_students()
        status, _, loc = self.req("POST", "/admin/students/add",
                                  {"username": "x", "name": "X",
                                   "password": "y"}, auth=False)
        self.assertEqual(status, 303)
        self.assertEqual(loc, "/admin/login")
        self.assertEqual(self.count_students(), before)


class TestAddStudent(Base):
    def test_add_one_student(self):
        before = self.count_students()
        status, _, loc = self.req("POST", "/admin/students/add", {
            "username": "newstudent",
            "name": "New Student",
            "password": "testpw123"})
        self.assertEqual(status, 303)
        self.assertIn("Added", urllib.parse.unquote(loc))
        self.assertEqual(self.count_students(), before + 1)
        con = self.db()
        row = con.execute("SELECT * FROM students WHERE username=?",
                          ("newstudent",)).fetchone()
        con.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "New Student")
        self.assertEqual(row["active"], 1)

    def test_can_sign_in_with_new_password(self):
        self.req("POST", "/admin/students/add", {
            "username": "testuser99",
            "name": "Test 99",
            "password": "secretsecret"})
        status, _, _ = self.req("POST", "/login",
                                {"username": "testuser99", "password": "secretsecret"})
        self.assertEqual(status, 303)            # successful login redirect

    def test_duplicate_username_is_rejected(self):
        before = self.count_students()
        self.req("POST", "/admin/students/add", {
            "username": "chandu",               # already exists
            "name": "Duplicate",
            "password": "x"})
        self.assertEqual(self.count_students(), before)

    def test_missing_field_rejected(self):
        before = self.count_students()
        status, _, loc = self.req("POST", "/admin/students/add", {
            "username": "incomplete",
            "name": ""})                        # missing password and name
        self.assertEqual(status, 303)
        self.assertIn("!", urllib.parse.unquote(loc))
        self.assertEqual(self.count_students(), before)


class TestResetPassword(Base):
    def test_reset_generates_new_password(self):
        con = self.db()
        sid = con.execute("SELECT id FROM students WHERE username=?",
                          ("chandu",)).fetchone()["id"]
        old_hash = con.execute("SELECT pw_hash FROM students WHERE id=?",
                               (sid,)).fetchone()["pw_hash"]
        con.close()
        status, _, loc = self.req("POST", "/admin/students/reset",
                                  {"id": str(sid)})
        self.assertEqual(status, 303)
        self.assertIn("newpw=", loc)
        con = self.db()
        new_hash = con.execute("SELECT pw_hash FROM students WHERE id=?",
                               (sid,)).fetchone()["pw_hash"]
        con.close()
        self.assertNotEqual(old_hash, new_hash)

    def test_new_password_works(self):
        con = self.db()
        sid = con.execute("SELECT id FROM students WHERE username=?",
                          ("chandu",)).fetchone()["id"]
        con.close()
        status, _, loc = self.req("POST", "/admin/students/reset",
                                  {"id": str(sid)})
        newpw = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)["newpw"][0]
        status, _, _ = self.req("POST", "/login",
                                {"username": "chandu", "password": newpw})
        self.assertEqual(status, 303)            # login works with new password


class TestDeactivate(Base):
    def test_deactivate_blocks_login(self):
        con = self.db()
        sid = con.execute("SELECT id FROM students WHERE username=?",
                          ("student001",)).fetchone()["id"]
        con.close()
        status, _, _ = self.req("POST", "/login",
                                {"username": "student001", "password": "testpw"})
        self.assertEqual(status, 303)            # works before deactivation
        self.req("POST", "/admin/students/toggle", {"id": str(sid)})
        status, body, _ = self.req("POST", "/login",
                                   {"username": "student001", "password": "testpw"})
        self.assertIn("deactivated", body)      # message shown
        self.assertNotEqual(status, 303)        # login redirect not given

    def test_reactivate_restores_login(self):
        con = self.db()
        sid = con.execute("SELECT id FROM students WHERE username=?",
                          ("student002",)).fetchone()["id"]
        con.close()
        self.req("POST", "/admin/students/toggle", {"id": str(sid)})
        status, _, _ = self.req("POST", "/login",
                                {"username": "student002", "password": "testpw"})
        self.assertNotEqual(status, 303)        # blocked
        self.req("POST", "/admin/students/toggle", {"id": str(sid)})
        status, _, _ = self.req("POST", "/login",
                                {"username": "student002", "password": "testpw"})
        self.assertEqual(status, 303)            # working again


class TestStudentsDashboard(Base):
    def test_dashboard_shows_student_count(self):
        status, body, _ = self.req("GET", f"/admin/students")
        self.assertEqual(status, 200)
        self.assertIn("Total students", body)
        self.assertIn("Active", body)

    def test_dashboard_lists_all_students(self):
        status, body, _ = self.req("GET", f"/admin/students")
        self.assertEqual(status, 200)
        self.assertIn("chandu", body)
        self.assertIn("student001", body)

    def test_dashboard_shows_action_buttons(self):
        status, body, _ = self.req("GET", f"/admin/students")
        self.assertEqual(status, 200)
        # at least one Reset password and one Deactivate button
        self.assertGreaterEqual(body.count("Reset password"), 1)
        self.assertGreaterEqual(body.count("Deactivate"), 1)


if __name__ == "__main__":
    unittest.main()
