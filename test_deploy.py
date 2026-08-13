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


class TestVercelConfig(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(BASE, "vercel.json")) as f:
            self.cfg = json.load(f)

    def test_declared_sources_exist(self):
        for build in self.cfg.get("builds", []):
            self.assertTrue(os.path.exists(os.path.join(BASE, build["src"])),
                            f"vercel.json builds {build['src']}, which is missing")

    def test_routing_is_a_catch_all_to_the_entrypoint(self):
        # `builds` disables zero-config, so `rewrites` would be ignored.
        if self.cfg.get("builds"):
            self.assertNotIn("rewrites", self.cfg,
                             "rewrites are ignored when builds is present; "
                             "use routes")
            dests = [r.get("dest") for r in self.cfg.get("routes", [])]
            self.assertIn("/api/index", dests)


if __name__ == "__main__":
    unittest.main(verbosity=2)
