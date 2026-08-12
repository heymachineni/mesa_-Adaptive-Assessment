"""Vercel serverless entrypoint for the MESA exam server.

Vercel's Python runtime looks for a class named `handler` in this file that
subclasses BaseHTTPRequestHandler -- which is exactly what server.py already
builds. So this module only does the three things that `python3 seed.py &&
python3 server.py` does for us locally:

  1. put the project root on sys.path so `import server` works from api/,
  2. point the SQLite file at /tmp, the only writable path on Vercel,
  3. create and seed that database on cold start, since /tmp starts empty.

NOTE: /tmp is per-instance and ephemeral. Every cold start gets a fresh,
freshly-seeded database, so attempts in progress do NOT survive one. See the
"Deploying to Vercel" section of README.md before using this for a real exam.
"""
import os
import sys
import threading
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Must be set before `seed` is imported: seed.DB_PATH is read at import time.
os.environ.setdefault("DB_DIR", "/tmp")

import seed as storage          # noqa: E402  (path/env setup has to run first)
import server                   # noqa: E402

_lock = threading.Lock()
_seeded = False


def _bootstrap():
    """Create and seed the database once per cold start."""
    global _seeded
    if _seeded and os.path.exists(storage.DB_PATH):
        return
    with _lock:
        if _seeded and os.path.exists(storage.DB_PATH):
            return
        argv = sys.argv
        sys.argv = ["seed.py"]          # seed.main() reads its flags from argv
        try:
            storage.main()
        finally:
            sys.argv = argv
        _seeded = True


class handler(server.Handler):
    def _normalize(self):
        """The catch-all rewrite can arrive as /api/index; map it back to /."""
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path in ("/api/index", "/api/index.py"):
            path = "/"
        elif path.startswith("/api/index/"):
            path = path[len("/api/index"):]
        else:
            return
        self.path = urllib.parse.urlunsplit(("", "", path, parsed.query, ""))

    def do_GET(self):
        _bootstrap()
        self._normalize()
        super().do_GET()

    def do_POST(self):
        _bootstrap()
        self._normalize()
        super().do_POST()
