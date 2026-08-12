"""SEB integration tests.  Run: python3 -m unittest test_seb -v

These tests verify the SERVER side of the SEB protocol:
  - the generated .seb file matches the documented container format
  - hash verification implements SHA256(url + key) per the SEB specs
  - enforce mode blocks non-SEB requests and admits requests carrying the
    correct X-SafeExamBrowser-RequestHash (computed here exactly as SEB would)
  - detect mode stamps every answer with its SEB verification status

What they can NOT verify: the real SEB desktop app opening the config and
locking the machine — that half runs on your computer, not in this codebase.
"""
import gzip
import http.client
import os
import plistlib
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.parse

import seb_support


class TestSebConfigFile(unittest.TestCase):
    def test_container_format_and_contents(self):
        data = seb_support.build_seb_config(
            "http://localhost:8000/", "http://localhost:8000/seb/quit", "bye")
        inner = gzip.decompress(data)
        self.assertEqual(inner[:4], b"plnd")            # documented plain prefix
        cfg = plistlib.loads(inner[4:])                  # valid XML plist
        self.assertEqual(cfg["startURL"], "http://localhost:8000/")
        self.assertEqual(cfg["sebConfigPurpose"], 0)     # "start an exam"
        self.assertTrue(cfg["sendBrowserExamKey"])       # headers enabled
        self.assertEqual(cfg["quitURL"], "http://localhost:8000/seb/quit")
        self.assertEqual(len(cfg["hashedQuitPassword"]), 64)
        self.assertEqual(cfg["hashedQuitPassword"],
                         cfg["hashedQuitPassword"].upper())

    def test_roundtrip(self):
        data = seb_support.build_seb_config("http://x/")
        self.assertEqual(seb_support.parse_seb_config(data)["startURL"],
                         "http://x/")


class TestSebHashes(unittest.TestCase):
    def test_request_hash_matches_spec_construction(self):
        url, key = "http://localhost:8000/exam", "abc123"
        import hashlib
        manual = hashlib.sha256((url + key).encode()).hexdigest()
        self.assertEqual(seb_support.expected_hash(url, key), manual)
        self.assertTrue(seb_support.check_hash(url, key, manual))
        self.assertTrue(seb_support.check_hash(url, key, manual.upper()))
        self.assertFalse(seb_support.check_hash(url, key, "deadbeef"))
        self.assertFalse(seb_support.check_hash(url, key, None))

    def test_fragment_stripped(self):
        self.assertEqual(seb_support.expected_hash("http://h/p#frag", "k"),
                         seb_support.expected_hash("http://h/p", "k"))

    def test_verify_request_precedence(self):
        url = "http://h/x"
        h = {"X-SafeExamBrowser-RequestHash": seb_support.expected_hash(url, "BEK"),
             "User-Agent": "Chrome"}
        ok, method = seb_support.verify_request(url, h, "BEK", "CK")
        self.assertTrue(ok)
        self.assertEqual(method, "browser-exam-key")
        h2 = {"X-SafeExamBrowser-ConfigKeyHash": seb_support.expected_hash(url, "CK")}
        ok, method = seb_support.verify_request(url, h2, "", "CK")
        self.assertTrue(ok)
        self.assertEqual(method, "config-key")
        ok, method = seb_support.verify_request(
            url, {"User-Agent": "Mozilla/5.0 ... SEB/3.10"}, "", "")
        self.assertTrue(ok)
        self.assertIn("WEAK", method)


class TestSebHTTP(unittest.TestCase):
    """Boot the real server with a temp DB and drive SEB modes over HTTP."""

    PORT = 8766
    BEK = "test-browser-exam-key"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        import seed
        import server
        cls.seed, cls.server = seed, server
        cls.orig_db = seed.DB_PATH
        seed.DB_PATH = os.path.join(cls.tmp, "seb_test.db")
        os.environ["DEFAULT_STUDENT_PASSWORD"] = "testpw"
        import sys
        sys.argv = ["seed.py"]
        seed.main()
        from http.server import ThreadingHTTPServer
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", cls.PORT), server.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.server.SEB_MODE, cls.server.SEB_BEK = "off", ""
        cls.seed.DB_PATH = cls.orig_db

    def req(self, method, path, body=None, cookie=None, seb_key=None, ua=None):
        url = f"http://127.0.0.1:{self.PORT}{path}"
        c = http.client.HTTPConnection("127.0.0.1", self.PORT, timeout=5)
        headers = {}
        if body is not None:
            body = urllib.parse.urlencode(body)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if cookie:
            headers["Cookie"] = f"mesa_session={cookie}"
        if seb_key:      # simulate exactly what SEB sends for this URL
            headers["X-SafeExamBrowser-RequestHash"] = \
                seb_support.expected_hash(url, seb_key)
        if ua:
            headers["User-Agent"] = ua
        c.request(method, path, body, headers)
        r = c.getresponse()
        data = r.read()
        sc = r.getheader("Set-Cookie", "")
        ct = r.getheader("Content-Type", "")
        c.close()
        return r.status, data, sc, ct

    # ---- enforce mode ----
    def test_enforce_blocks_normal_browser_and_admits_seb(self):
        self.server.SEB_MODE, self.server.SEB_BEK = "enforce", self.BEK
        try:
            # plain browser -> blocked with instructions
            status, body, _, _ = self.req("GET", "/")
            self.assertEqual(status, 403)
            self.assertIn(b"Safe Exam Browser required", body)
            self.assertIn(b"/seb/config", body)
            # wrong key -> still blocked
            status, _, _, _ = self.req("GET", "/", seb_key="wrong-key")
            self.assertEqual(status, 403)
            # correct SEB hash -> login page served
            status, body, _, _ = self.req("GET", "/", seb_key=self.BEK)
            self.assertEqual(status, 200)
            self.assertIn(b"Sign in", body)
            # spoofed User-Agent alone must NOT pass when a key is configured
            status, _, _, _ = self.req("GET", "/", ua="FakeBrowser SEB/3.10")
            self.assertEqual(status, 403)
            # admin and config download stay reachable from any browser
            status, _, _, _ = self.req("GET", "/admin")
            self.assertEqual(status, 403)   # admin key missing, not SEB-blocked
            status, data, _, ct = self.req("GET", "/seb/config")
            self.assertEqual(status, 200)
            self.assertEqual(ct, "application/seb")
            self.assertEqual(seb_support.parse_seb_config(data)["startURL"],
                             f"http://127.0.0.1:{self.PORT}/")
        finally:
            self.server.SEB_MODE, self.server.SEB_BEK = "off", ""

    def test_enforce_full_exam_flow_inside_simulated_seb(self):
        self.server.SEB_MODE, self.server.SEB_BEK = "enforce", self.BEK
        try:
            status, _, sc, _ = self.req(
                "POST", "/login", {"username": "chandu", "password": "testpw"},
                seb_key=self.BEK)
            self.assertEqual(status, 303)
            token = sc.split("mesa_session=")[1].split(";")[0]
            self.req("POST", "/start", {}, cookie=token, seb_key=self.BEK)
            con = sqlite3.connect(self.seed.DB_PATH)
            qid = con.execute(
                "SELECT a.current_question_id FROM attempts a "
                "JOIN students s ON s.id=a.student_id "
                "WHERE s.username='chandu' AND a.status='active'").fetchone()[0]
            con.close()
            status, _, _, _ = self.req("POST", "/answer",
                                       {"qid": qid, "answer": "0"},
                                       cookie=token, seb_key=self.BEK)
            self.assertEqual(status, 303)
            con = sqlite3.connect(self.seed.DB_PATH)
            flag = con.execute(
                "SELECT aq.seb_verified FROM attempt_questions aq "
                "JOIN attempts a ON a.id=aq.attempt_id "
                "JOIN students s ON s.id=a.student_id "
                "WHERE s.username='chandu' AND aq.question_id=? "
                "AND aq.answered_at IS NOT NULL", (qid,)).fetchone()[0]
            con.close()
            self.assertEqual(flag, 1)       # answer stamped as SEB-verified
        finally:
            self.server.SEB_MODE, self.server.SEB_BEK = "off", ""

    # ---- detect mode ----
    def test_detect_allows_any_browser_but_stamps_answers(self):
        self.server.SEB_MODE, self.server.SEB_BEK = "detect", self.BEK
        try:
            status, _, sc, _ = self.req(
                "POST", "/login", {"username": "student001", "password": "testpw"})
            self.assertEqual(status, 303)   # NOT blocked in detect mode
            token = sc.split("mesa_session=")[1].split(";")[0]
            self.req("POST", "/start", {}, cookie=token)
            con = sqlite3.connect(self.seed.DB_PATH)
            qid = con.execute(
                "SELECT a.current_question_id FROM attempts a "
                "JOIN students s ON s.id=a.student_id "
                "WHERE s.username='student001' AND a.status='active'").fetchone()[0]
            con.close()
            self.req("POST", "/answer", {"qid": qid, "answer": "0"}, cookie=token)
            con = sqlite3.connect(self.seed.DB_PATH)
            flag = con.execute(
                "SELECT aq.seb_verified FROM attempt_questions aq "
                "JOIN attempts a ON a.id=aq.attempt_id "
                "JOIN students s ON s.id=a.student_id "
                "WHERE s.username='student001' AND aq.question_id=? "
                "AND aq.answered_at IS NOT NULL", (qid,)).fetchone()[0]
            con.close()
            self.assertEqual(flag, 0)       # recorded as NOT from SEB
        finally:
            self.server.SEB_MODE, self.server.SEB_BEK = "off", ""


if __name__ == "__main__":
    unittest.main()
