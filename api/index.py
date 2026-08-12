"""Vercel serverless entrypoint for the MESA exam server.

Vercel's Python runtime looks for a class named `handler` in this file that
subclasses BaseHTTPRequestHandler -- which is exactly what server.py already
builds. So this module only does the three things that `python3 seed.py &&
python3 server.py` does for us locally:

  1. put the project root on sys.path so `import server` works from api/,
  2. point the SQLite file at /tmp, the only writable path on Vercel,
  3. create and seed that database on cold start, since /tmp starts empty.

If the import or the seeding fails, the whole function would otherwise die as
an opaque 500 / FUNCTION_INVOCATION_FAILED. Instead we catch it, always write
the traceback to stderr (visible in Vercel's Runtime Logs), and serve it in
the response body only when MESA_DEBUG=1 -- tracebacks leak paths and config,
so that stays off unless you are actively debugging a deploy.

NOTE: /tmp is per-instance and ephemeral. Every cold start gets a fresh,
freshly-seeded database, so attempts in progress do NOT survive one. See the
"Deploying to Vercel" section of README.md before using this for a real exam.
"""
import os
import sys
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Must be set before `seed` is imported: seed.DB_PATH is read at import time.
os.environ.setdefault("DB_DIR", "/tmp")

DEBUG = os.environ.get("MESA_DEBUG", "").strip() in ("1", "true", "on")

_import_error = None
try:
    import seed as storage
    import server
except Exception:                       # noqa: BLE001 - reported below
    _import_error = traceback.format_exc()
    sys.stderr.write("[mesa] import failed:\n" + _import_error)

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


def _diagnostics(detail):
    """What we know about the bundle, to explain a failed cold start."""
    try:
        listing = "\n".join(sorted(os.listdir(ROOT)))
    except Exception as exc:            # noqa: BLE001
        listing = f"<could not list {ROOT}: {exc}>"
    return (
        f"python:   {sys.version}\n"
        f"root:     {ROOT}\n"
        f"db_dir:   {os.environ.get('DB_DIR')}\n\n"
        f"files bundled at root:\n{listing}\n\n"
        f"{detail}"
    )


if _import_error is not None:
    # server.Handler never got defined, so serve the reason instead.
    class handler(BaseHTTPRequestHandler):
        def _fail(self):
            body = ("The exam server failed to start.\n\n"
                    "Set MESA_DEBUG=1 in your Vercel environment variables and\n"
                    "redeploy to see the traceback here, or read it now in\n"
                    "Vercel -> your project -> Runtime Logs.\n")
            if DEBUG:
                body = _diagnostics(_import_error)
            data = body.encode()
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _fail

else:
    class handler(server.Handler):
        def _normalize(self):
            """The catch-all rewrite can arrive as /api/index; map it back."""
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            if path in ("/api/index", "/api/index.py"):
                path = "/"
            elif path.startswith("/api/index/"):
                path = path[len("/api/index"):]
            else:
                return
            self.path = urllib.parse.urlunsplit(("", "", path, parsed.query, ""))

        def _serve(self, method):
            try:
                _bootstrap()
                self._normalize()
                method(self)
            except Exception:           # noqa: BLE001 - would be an opaque 500
                detail = traceback.format_exc()
                sys.stderr.write(f"[mesa] {self.command} {self.path} failed:\n"
                                 + detail)
                body = ("Something went wrong handling that request.\n\n"
                        "The traceback is in Vercel -> Runtime Logs. Set\n"
                        "MESA_DEBUG=1 to see it here instead.\n")
                if DEBUG:
                    body = _diagnostics(detail)
                data = body.encode()
                try:
                    self.send_response(500)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                except Exception:       # noqa: BLE001 - headers already sent
                    pass

        def do_GET(self):
            self._serve(server.Handler.do_GET)

        def do_POST(self):
            self._serve(server.Handler.do_POST)
