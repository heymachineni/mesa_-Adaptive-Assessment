"""Deployment guards — the things that only break once you've pushed.

Each of these covers a failure that actually happened on Vercel, where the
feedback loop is a git push and a build log:

  - a `handler` nested inside an if/else, invisible to Vercel's build-time
    scan: "Could not find a top-level app, application, or handler"
  - a backslash inside an f-string expression, which is a syntax error before
    Python 3.12 and shows up only as FUNCTION_INVOCATION_FAILED
  - a vercel.json that points at a file that isn't there

Run:  python3 test_deploy.py
"""
import ast
import json
import os
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
ENTRYPOINT = os.path.join(BASE, "api", "index.py")
PY_FILES = [os.path.join(BASE, f) for f in sorted(os.listdir(BASE))
            if f.endswith(".py")] + [ENTRYPOINT]


def top_level_names(path):
    """Exactly what Vercel sees: direct children of the module only."""
    with open(path) as f:
        tree = ast.parse(f.read())
    names = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names += [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target,
                                                            ast.Name):
            names.append(node.target.id)
    return names


class TestVercelEntrypoint(unittest.TestCase):
    def test_handler_is_top_level(self):
        names = top_level_names(ENTRYPOINT)
        self.assertTrue(
            {"app", "application", "handler"} & set(names),
            "Vercel parses api/index.py and walks only the module's direct "
            "children. Defining `handler` inside an if/else or try hides it "
            "and the build fails with 'Could not find a top-level app, "
            f"application, or handler'. Found: {names}")

    def test_handler_is_a_request_handler(self):
        import sys
        sys.path.insert(0, os.path.join(BASE, "api"))
        sys.path.insert(0, BASE)
        os.environ.setdefault("DB_DIR", "/tmp")
        import index
        from http.server import BaseHTTPRequestHandler
        self.assertIsNone(index._import_error,
                          f"api/index.py failed to import: {index._import_error}")
        self.assertTrue(issubclass(index.handler, BaseHTTPRequestHandler))
        for verb in ("do_GET", "do_POST"):
            self.assertTrue(callable(getattr(index.handler, verb, None)),
                            f"handler must answer {verb}")


class TestStartupFailureIsLegible(unittest.TestCase):
    """A blank 'it failed' page costs a redeploy to learn anything."""

    def setUp(self):
        import sys
        sys.path.insert(0, os.path.join(BASE, "api"))
        sys.path.insert(0, BASE)
        os.environ.setdefault("DB_DIR", "/tmp")
        import index
        self.index = index

    def test_page_names_the_cause_and_the_missing_file(self):
        fake = ("Traceback (most recent call last):\n"
                "  File \"seed.py\", line 30, in level_names\n"
                "FileNotFoundError: [Errno 2] No such file or directory: "
                "'/var/task/config.json'")
        page = self.index._startup_failure(fake)
        self.assertIn("FileNotFoundError", page)
        self.assertIn("config.json", page)
        self.assertIn("Required files:", page)

    def test_every_import_time_dependency_is_listed(self):
        """If server.py starts reading a new file at import, list it here."""
        for name in ("server.py", "seed.py", "adaptive_engine.py",
                     "config.json", "questions.json"):
            self.assertIn(name, self.index.REQUIRED_FILES)
            self.assertTrue(os.path.exists(os.path.join(BASE, name)))


class TestRuntimeCompatibility(unittest.TestCase):
    def test_no_backslash_inside_fstring_expressions(self):
        """Legal only from 3.12. Vercel's runtime may be older."""
        offenders = []
        for path in PY_FILES:
            with open(path) as f:
                src = f.read()
            for node in ast.walk(ast.parse(src)):
                if not isinstance(node, ast.JoinedStr):
                    continue
                for value in node.values:
                    if not isinstance(value, ast.FormattedValue):
                        continue
                    # .value is the expression inside the braces; the literal
                    # text around it may legitimately contain \n.
                    seg = ast.get_source_segment(src, value.value)
                    if seg and "\\" in seg:
                        offenders.append(
                            f"{os.path.basename(path)}:{value.lineno}: {seg}")
        self.assertEqual(offenders, [], "backslash in an f-string expression "
                         "is a SyntaxError before Python 3.12:\n" +
                         "\n".join(offenders))


class TestBlankEnvironmentVariables(unittest.TestCase):
    """A hosting dashboard variable created with an empty value reads as unset.

    os.environ.get() only falls back when the key is *absent*. A blank PORT on
    Vercel therefore produced int("") and killed the deployment at import.
    """

    BLANK = {"PORT": "", "DEFAULT_STUDENT_PASSWORD": "", "DB_DIR": "",
             "PROCTOR_FOCUS": "", "MESA_DEBUG": "", "SEED_STUDENTS": "",
             "AUTO_SEED": "", "LOAD_HOST": "",
             "ADMIN_PASSWORD_CHANDU": "", "ADMIN_PASSWORD_ANKUR": ""}

    def _run(self, code):
        import subprocess
        import sys
        environ = dict(os.environ, **self.BLANK)
        return subprocess.run([sys.executable, "-c", code], env=environ,
                              cwd=BASE, capture_output=True, text=True)

    def test_helpers_treat_blank_as_unset(self):
        from seed import env, env_flag, env_int
        os.environ["MESA_BLANK_TEST"] = "   "
        try:
            self.assertEqual(env("MESA_BLANK_TEST", "fallback"), "fallback")
            self.assertEqual(env_int("MESA_BLANK_TEST", 8000), 8000)
            self.assertTrue(env_flag("MESA_BLANK_TEST", True))
            os.environ["MESA_BLANK_TEST"] = "not-a-number"
            self.assertEqual(env_int("MESA_BLANK_TEST", 8000), 8000)
            os.environ["MESA_BLANK_TEST"] = "off"
            self.assertFalse(env_flag("MESA_BLANK_TEST", True))
        finally:
            del os.environ["MESA_BLANK_TEST"]

    def test_server_imports_with_every_variable_blank(self):
        r = self._run("import server; print(server.PORT)")
        self.assertEqual(r.returncode, 0,
                         f"server.py failed to import with blank env vars:\n"
                         f"{r.stderr}")
        self.assertIn("8000", r.stdout)

    def test_entrypoint_imports_with_every_variable_blank(self):
        r = self._run(
            "import sys; sys.path.insert(0, 'api'); import index;"
            "assert index._import_error is None, index._import_error;"
            "print(index.storage.DB_PATH)")
        self.assertEqual(r.returncode, 0,
                         f"api/index.py failed with blank env vars:\n{r.stderr}")
        self.assertTrue(r.stdout.strip().startswith("/tmp/"),
                        f"blank DB_DIR must fall back to /tmp, got {r.stdout!r}")

    def test_blank_admin_password_override_falls_back_to_the_list(self):
        """An empty ADMIN_PASSWORD_* must not blank out an admin's password."""
        r = self._run(
            "import seed;"
            "u, _, pw = seed.ADMINS[0];"
            "resolved = seed.admin_password(u, pw);"
            "assert resolved == pw, resolved;"
            "assert resolved, 'admin password resolved to empty';"
            "print('ok')")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_admin_password_env_override_wins(self):
        import subprocess
        import sys
        environ = dict(os.environ, ADMIN_PASSWORD_CHANDU="from-env")
        r = subprocess.run(
            [sys.executable, "-c",
             "import seed; print(seed.admin_password('chandu', 'in-code'))"],
            env=environ, cwd=BASE, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "from-env")


class TestVercelConfig(unittest.TestCase):
    """Zero-config (`functions` + `rewrites`) is the shape that works here.

    `builds` is the legacy alternative. It turns zero-config off, ignores
    `rewrites`, and its `routes` rewrite the request path rather than passing
    the original URL through — which served a platform 404 on every route
    when it was tried. Mixing the two shapes is the failure mode these tests
    exist to catch.
    """

    def setUp(self):
        with open(os.path.join(BASE, "vercel.json")) as f:
            self.cfg = json.load(f)

    def test_declared_sources_exist(self):
        for build in self.cfg.get("builds", []):
            self.assertTrue(os.path.exists(os.path.join(BASE, build["src"])),
                            f"vercel.json builds {build['src']}, which is missing")
        for pattern in self.cfg.get("functions", {}):
            self.assertTrue(os.path.exists(os.path.join(BASE, pattern)),
                            f"vercel.json configures {pattern}, which is missing")

    def test_one_routing_shape_only(self):
        if self.cfg.get("builds"):
            self.assertNotIn("rewrites", self.cfg,
                             "rewrites are ignored when builds is present")
            self.assertNotIn("functions", self.cfg,
                             "functions and builds cannot be combined")
        else:
            self.assertNotIn("routes", self.cfg,
                             "routes is the legacy pairing for builds; "
                             "zero-config uses rewrites")

    def test_every_path_reaches_the_entrypoint(self):
        if self.cfg.get("builds"):
            rules = [(r.get("src"), r.get("dest"))
                     for r in self.cfg.get("routes", [])]
        else:
            rules = [(r.get("source"), r.get("destination"))
                     for r in self.cfg.get("rewrites", [])]
        self.assertIn(("/(.*)", "/api/index"), rules,
                      f"no catch-all to the entrypoint; found {rules}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
