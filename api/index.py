"""Vercel serverless entrypoint for the MESA exam server.

Vercel's Python runtime looks for a class named `handler` in this file that
subclasses BaseHTTPRequestHandler -- which is exactly what server.py already
builds. So this module only does the three things that `python3 seed.py &&
python3 server.py` does for us locally:

  1. put the project root on sys.path so `import server` works from api/,
  2. point the SQLite file at /tmp, the only writable path on Vercel,
  3. create and seed that database on cold start, since /tmp starts empty.

`handler` MUST stay at the top level of this module. Vercel looks for it by
parsing this file at build time and walking only the direct children of the
module, so a class nested inside an `if` or a `try` is invisible to it and the
build fails with "Could not find a top-level app, application, or handler".
That is why the base class is chosen into a variable rather than the class
being defined in two branches. test_deploy.py guards this.

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
storage = None
server = None
try:
    import seed as storage
    import server
except Exception:                       # noqa: BLE001 - reported below
    _import_error = traceback.format_exc()
    sys.stderr.write("[mesa] import failed:\n" + _import_error)

# server.Handler when the app imported cleanly; a bare handler otherwise, so
# that a broken deploy still answers with the reason instead of nothing.
_Base = BaseHTTPRequestHandler if _import_error else server.Handler

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


class handler(_Base):
    """Top level on purpose -- see the module docstring."""

    def _plain(self, status, body):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _normalize(self):
        """The catch-all route can arrive as /api/index; map it back.

        Vercel is expected to hand us the URL the student actually asked
        for, so this should never fire. If it does, every route would
        otherwise collapse onto the sign-in page, so say so loudly in the
        Runtime Logs rather than fail quietly.
        """
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path in ("/api/index", "/api/index.py"):
            path = "/"
        elif path.startswith("/api/index/"):
            path = path[len("/api/index"):]
        else:
            return
        sys.stderr.write(
            f"[mesa] routing rewrote the path: got {self.path!r}, using "
            f"{path!r}. The original URL did not survive the catch-all "
            f"route.\n")
        self.path = urllib.parse.urlunsplit(("", "", path, parsed.query, ""))

    def _serve(self, method_name):
        if _import_error is not None:
            body = ("The exam server failed to start.\n\n"
                    "Set MESA_DEBUG=1 in your Vercel environment variables\n"
                    "and redeploy to see the traceback here, or read it now\n"
                    "in Vercel -> your project -> Runtime Logs.\n")
            return self._plain(500, _diagnostics(_import_error) if DEBUG
                               else body)
        try:
            _bootstrap()
            self._normalize()
            getattr(_Base, method_name)(self)
        except Exception:               # noqa: BLE001 - would be an opaque 500
            detail = traceback.format_exc()
            sys.stderr.write(f"[mesa] {self.command} {self.path} failed:\n"
                             + detail)
            body = ("Something went wrong handling that request.\n\n"
                    "The traceback is in Vercel -> Runtime Logs. Set\n"
                    "MESA_DEBUG=1 to see it here instead.\n")
            try:
                self._plain(500, _diagnostics(detail) if DEBUG else body)
            except Exception:           # noqa: BLE001 - headers already sent
                pass

    def do_GET(self):
        self._serve("do_GET")

    def do_POST(self):
        self._serve("do_POST")
