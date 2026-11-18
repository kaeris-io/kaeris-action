"""P2.2: in check mode the action keeps ONE comment on the pull request with what is broken, and
edits it on the next push.

These tests run the action's own `run:` block from action.yml under bash, the way a runner does
(`bash --noprofile --norc -eo pipefail`), with its inputs and env wired from action.yml itself.
Two things are replaced, nothing else:

  * `kaeris` is a stub that prints a recorded `kaeris check --json` result (test/fixtures/check,
    recorded from CLI 0.2.39 on test/fixtures/{broken,good}) and copies the recorded SARIF;
  * the GitHub REST API is a local http.server on 127.0.0.1 that keeps the PR's comments.

So a passing run here means the real step posts / edits the comment and turns red or green by
the findings, not just that some helper function returns the right string. Offline, stdlib.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ACTION = os.path.join(ROOT, "action.yml")
FIX = os.path.join(HERE, "fixtures", "check")
REPO = "octo/shop"
PR = 7
TOKEN = "ghs_test_token_not_real"
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"


def _posix(path):
    return path.replace("\\", "/")


def find_bash():
    explicit = os.environ.get("KAERIS_TEST_BASH")
    if explicit:
        return explicit
    if os.name == "nt":
        # Git Bash, never C:\Windows\System32\bash.exe (that is WSL, a different filesystem).
        for p in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
            if os.path.isfile(p):
                return p
        return None
    return shutil.which("bash")


# ── action.yml, read the way the runner reads it (only the shapes this file uses) ────────────

def parse_action():
    with open(ACTION, encoding="utf-8") as f:
        lines = f.read().splitlines()
    inputs, section, cur = {}, None, None
    for ln in lines:
        if ln and not ln.startswith(" "):
            section = ln.split(":")[0]
            continue
        if section != "inputs":
            continue
        m = re.match(r"^  ([\w-]+):\s*$", ln)
        if m:
            cur = m.group(1)
            inputs[cur] = ""
            continue
        m = re.match(r"^    default:\s*(.*)$", ln)
        if m and cur:
            v = m.group(1).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            inputs[cur] = v
    i = next(n for n, ln in enumerate(lines) if ln.strip() == "id: run")
    env, script, n = {}, [], i + 1
    while not lines[n].strip().startswith("run:"):
        m = re.match(r"^        ([A-Z_][A-Z0-9_]*):\s*(.*)$", lines[n])
        if m:
            env[m.group(1)] = m.group(2).strip()
        n += 1
    for ln in lines[n + 1:]:
        if ln.strip() and not ln.startswith("        "):
            break
        script.append(ln[8:])
    return inputs, env, "\n".join(script) + "\n"


def resolve(expr, inputs, ctx):
    def sub(m):
        name = m.group(1)
        if name.startswith("inputs."):
            return resolve(inputs.get(name[7:], ""), inputs, ctx)
        return ctx[name]
    return re.sub(r"\$\{\{\s*([\w.-]+)\s*\}\}", sub, expr)


# ── a GitHub that keeps one PR's comments ────────────────────────────────────────────────────

class FakeGitHub:
    def __init__(self, deny=False):
        self.deny = deny                   # True: every call is 403, as for a read-only token
        self.comments = []
        self.requests = []
        self.next_id = 1000
        gh = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                url = urlsplit(self.path)
                gh.requests.append({"method": method, "path": url.path, "query": parse_qs(url.query),
                                    "body": body, "auth": self.headers.get("Authorization")})
                if gh.deny:
                    return self._send(403, {"message": "Resource not accessible by integration"})
                list_path = f"/repos/{REPO}/issues/{PR}/comments"
                m = re.match(rf"^/repos/{REPO}/issues/comments/(\d+)$", url.path)
                if method == "GET" and url.path == list_path:
                    q = parse_qs(url.query)
                    per = int(q.get("per_page", ["30"])[0])
                    page = int(q.get("page", ["1"])[0])
                    return self._send(200, gh.comments[(page - 1) * per: page * per])
                if method == "POST" and url.path == list_path:
                    c = gh.add(body["body"])
                    return self._send(201, c)
                if method == "PATCH" and m:
                    for c in gh.comments:
                        if c["id"] == int(m.group(1)):
                            c["body"] = body["body"]
                            return self._send(200, c)
                    return self._send(404, {"message": "Not Found"})
                return self._send(404, {"message": "Not Found"})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PATCH(self):
                self._handle("PATCH")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def add(self, body, login="github-actions[bot]"):
        self.next_id += 1
        c = {"id": self.next_id, "body": body, "user": {"login": login, "type": "Bot"}}
        self.comments.append(c)
        return c

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def calls(self, method):
        return [r for r in self.requests if r["method"] == method]


# ── one run of the step ──────────────────────────────────────────────────────────────────────

KAERIS_STUB = """#!/bin/sh
# Stub of the KAERIS CLI: the recorded `kaeris check --json` result, the recorded SARIF.
echo "$@" >> "$FAKE_ARGS"
prev=""
for a in "$@"; do
  if [ "$prev" = "--sarif" ] && [ -n "$FAKE_SARIF" ]; then cp "$FAKE_SARIF" "$a"; fi
  prev="$a"
done
cat "$FAKE_CHECK_JSON"
exit "${FAKE_EXIT:-0}"
"""


class Run:
    def __init__(self, code, out, summary, args):
        self.code, self.out, self.summary, self.args = code, out, summary, args


def run_step(gh, fixture="broken", event="pull_request", inputs=None, fork=False, token=TOKEN):
    bash = find_bash()
    defaults, env_map, script = parse_action()
    given = {"mode": "check", "source": f"test/fixtures/{fixture}/en.json",
             "languages": "de" if fixture == "broken" else "de,fr"}
    given.update(inputs or {})
    values = {**defaults, **given}
    with tempfile.TemporaryDirectory() as t:
        tmp = _posix(t)
        stubs = os.path.join(t, "bin")
        os.makedirs(stubs)
        for name, text in (("kaeris", KAERIS_STUB),
                           ("python3", f'#!/bin/sh\nexec "{_posix(sys.executable)}" "$@"\n')):
            p = os.path.join(stubs, name)
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            os.chmod(p, 0o755)
        payload = {"pull_request": {
            "number": PR, "head": {"sha": HEAD_SHA, "repo": {"full_name": ("fork/shop" if fork else REPO)}},
            "base": {"repo": {"full_name": REPO}}}} if event.startswith("pull_request") else \
            {"ref": "refs/heads/main", "after": HEAD_SHA}
        event_path = os.path.join(t, "event.json")
        with open(event_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        ctx = {"github.token": token, "github.action_path": _posix(ROOT)}
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GITHUB_", "RUNNER_", "INPUT_", "KA_", "KAERIS_"))}
        env.update({
            "PATH": stubs + os.pathsep + env.get("PATH", ""),
            "PYTHONUTF8": "1", "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
            "GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": event, "GITHUB_EVENT_PATH": _posix(event_path),
            "GITHUB_REPOSITORY": REPO, "GITHUB_API_URL": gh.url, "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_RUN_ID": "4242", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "f" * 40,
            "GITHUB_OUTPUT": f"{tmp}/gh_output", "GITHUB_STEP_SUMMARY": f"{tmp}/step_summary.md",
            "RUNNER_TEMP": tmp,
            "FAKE_CHECK_JSON": _posix(os.path.join(FIX, f"{fixture}.json")),
            "FAKE_SARIF": _posix(os.path.join(FIX, f"{fixture}.sarif")),
            "FAKE_EXIT": "1" if fixture == "broken" else "0",
            "FAKE_ARGS": f"{tmp}/kaeris_args",
        })
        for k, v in env_map.items():
            env[k] = resolve(v, values, ctx)
        script_path = os.path.join(t, "step.sh")
        with open(script_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(resolve(script, values, ctx))
        p = subprocess.run([bash, "--noprofile", "--norc", "-eo", "pipefail", _posix(script_path)],
                           cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
        summary = ""
        if os.path.isfile(os.path.join(t, "step_summary.md")):
            with open(os.path.join(t, "step_summary.md"), encoding="utf-8") as f:
                summary = f.read()
        args = ""
        if os.path.isfile(os.path.join(t, "kaeris_args")):
            with open(os.path.join(t, "kaeris_args"), encoding="utf-8") as f:
                args = f.read()
    return Run(p.returncode, p.stdout + p.stderr, summary, args)


@unittest.skipUnless(find_bash(), "needs bash (Git Bash on Windows)")
class PRComment(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.addCleanup(self.gh.close)

    def marked(self):
        return [c for c in self.gh.comments if c["body"].startswith("<!-- kaeris-action")]

    # (a) a broken {name}: one comment with the marker, the job is red
    def test_a_broken_placeholder_posts_one_comment_and_fails(self):
        r = run_step(self.gh, "broken")
        self.assertNotEqual(r.code, 0, r.out)
        posts = self.gh.calls("POST")
        self.assertEqual(len(posts), 1, r.out)
        self.assertEqual(posts[0]["path"], f"/repos/{REPO}/issues/{PR}/comments")
        self.assertEqual(posts[0]["auth"], f"Bearer {TOKEN}")
        body = posts[0]["body"]["body"]
        self.assertTrue(body.startswith("<!-- kaeris-action"), body)
        for want in ("greeting", "{name}", "checkout.title", "| de |", "test/fixtures/broken/de.json"):
            self.assertIn(want, body)
        # the line of the broken key, at the PR's head commit
        self.assertIn(f"https://github.com/{REPO}/blob/{HEAD_SHA}/test/fixtures/broken/de.json#L2", body)
        self.assertIn(f"https://github.com/{REPO}/actions/runs/4242", body)
        self.assertIn("https://kaeris.dev/?utm_source=action-comment", body)
        self.assertNotIn(TOKEN, r.out)
        self.assertLess(len(body), 65536)

    # (b) the next push edits that comment, it does not add a second one
    def test_b_second_run_patches_the_same_comment(self):
        run_step(self.gh, "broken")
        first = self.marked()[0]["id"]
        r = run_step(self.gh, "broken")
        self.assertNotEqual(r.code, 0, r.out)
        self.assertEqual(len(self.gh.calls("POST")), 1, r.out)
        patches = self.gh.calls("PATCH")
        self.assertEqual(len(patches), 1, r.out)
        self.assertEqual(patches[0]["path"], f"/repos/{REPO}/issues/comments/{first}")
        self.assertEqual(len(self.marked()), 1)

    # (c) clean after broken: the comment says so and the job is green
    def test_c_clean_run_updates_existing_comment_to_clean(self):
        run_step(self.gh, "broken")
        r = run_step(self.gh, "good")
        self.assertEqual(r.code, 0, r.out)
        self.assertEqual(len(self.gh.calls("POST")), 1)
        self.assertEqual(len(self.gh.calls("PATCH")), 1, r.out)
        body = self.marked()[0]["body"]
        self.assertIn("no problems", body.lower())
        self.assertNotIn("greeting", body)

    # (c) clean with no comment yet: nothing is created
    def test_c_clean_run_without_comment_creates_nothing(self):
        r = run_step(self.gh, "good")
        self.assertEqual(r.code, 0, r.out)
        self.assertEqual(self.gh.calls("POST") + self.gh.calls("PATCH"), [])
        self.assertEqual(self.gh.comments, [])

    # (d) fork PR / no pull-requests: write: a warning, and the findings alone decide the job
    def test_d_forbidden_broken_still_fails_on_findings_with_a_warning(self):
        self.gh.deny = True
        r = run_step(self.gh, "broken", fork=True)
        self.assertEqual(r.code, 1, r.out)
        self.assertIn("::warning", r.out)
        self.assertIn("pull-requests: write", r.out)
        self.assertNotIn(TOKEN, r.out)

    def test_d_forbidden_clean_stays_green(self):
        self.gh.deny = True
        r = run_step(self.gh, "good")
        self.assertEqual(r.code, 0, r.out)

    def test_d_forbidden_broken_same_repo(self):
        self.gh.deny = True
        r = run_step(self.gh, "broken")
        self.assertEqual(r.code, 1, r.out)
        self.assertIn("::warning", r.out)

    def test_d_no_token_warns_and_calls_nothing(self):
        r = run_step(self.gh, "broken", token="")
        self.assertEqual(r.code, 1, r.out)
        self.assertIn("::warning", r.out)
        self.assertEqual(self.gh.requests, [])

    # (e) not a pull request: the comment is not touched at all
    def test_e_push_event_touches_no_comment(self):
        self.gh.add("<!-- kaeris-action check: test/fixtures/broken/en.json -->\nold")
        r = run_step(self.gh, "broken", event="push")
        self.assertEqual(r.code, 1, r.out)
        self.assertEqual(self.gh.requests, [])
        self.assertTrue(self.gh.comments[0]["body"].endswith("old"))

    def test_e_comment_false_touches_nothing(self):
        r = run_step(self.gh, "broken", inputs={"comment": "false"})
        self.assertEqual(r.code, 1, r.out)
        self.assertEqual(self.gh.requests, [])

    # around it
    def test_our_comment_found_past_the_first_page(self):
        for i in range(150):
            self.gh.add(f"review comment {i}", login="someone")
        run_step(self.gh, "broken")
        self.assertEqual(len(self.gh.calls("POST")), 1)
        run_step(self.gh, "broken")
        self.assertEqual(len(self.gh.calls("POST")), 1, "second run must PATCH the comment on page 2")
        self.assertEqual(len(self.gh.calls("PATCH")), 1)

    def test_a_human_quoting_the_marker_is_not_hijacked(self):
        quoted = self.gh.add("> <!-- kaeris-action check: test/fixtures/broken/en.json -->\n> why?",
                             login="someone")
        run_step(self.gh, "broken")
        self.assertEqual(len(self.gh.calls("POST")), 1)
        self.assertTrue(quoted["body"].endswith("why?"))

    def test_pull_request_target_comments_too(self):
        run_step(self.gh, "broken", event="pull_request_target")
        self.assertEqual(len(self.gh.calls("POST")), 1)

    def test_job_summary_lists_every_finding(self):
        r = run_step(self.gh, "broken", event="push")
        self.assertIn("checkout.title", r.summary)
        self.assertIn("greeting", r.summary)

    def test_check_is_still_called_with_the_v2_flags(self):
        r = run_step(self.gh, "broken")
        self.assertIn("check --source test/fixtures/broken/en.json --langs de", r.args)
        self.assertIn("--ci", r.args)
        self.assertIn("--json", r.args)


class Format(unittest.TestCase):
    """The comment text itself: escaped, bounded."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, ROOT)
        import pr_comment
        cls.m = pr_comment

    def body(self, check, **kw):
        rows = self.m.collect(check, sarif=None, strict=kw.pop("strict", False))
        return self.m.build_comment(rows, source="locales/en.json", langs=["de"], **kw)

    def test_markdown_in_values_is_escaped(self):
        check = {"ok": False, "missing": {}, "extra": {}, "missing_files": [], "placeholder_issues": [],
                 "warnings": [],
                 "faults": [{"lang": "de", "key": "a|b`c", "severity": "error",
                             "msg": "lost inline tag <b> | see @admin and #12 **now**"}]}
        body = self.body(check)
        row = next(ln for ln in body.splitlines() if "lost inline tag" in ln)
        self.assertEqual(row.count(" | "), 3, row)          # 4 columns, the | in the values escaped
        self.assertNotIn("<b>", row)                         # shown, not rendered as bold
        self.assertNotIn("@admin", row)                      # no mention ping
        self.assertNotIn("**now**", row)
        self.assertNotIn("`c", row)

    def test_size_is_bounded_and_says_how_many_are_left(self):
        missing = {"de": [f"section.key_{i:05d}_" + "x" * 150 for i in range(3000)]}
        check = {"ok": False, "missing": missing, "extra": {}, "missing_files": [],
                 "placeholder_issues": [], "faults": [], "warnings": []}
        body = self.body(check)
        self.assertLess(len(body), 65536)
        self.assertRegex(body, r"\d+ more")

    def test_placeholder_problem_said_once(self):
        with open(os.path.join(FIX, "broken.json"), encoding="utf-8") as f:
            check = json.load(f)
        body = self.body(check)
        self.assertEqual(body.count("lost placeholder {name}"), 1, body)

    def test_warnings_alone_are_not_blocking(self):
        check = {"ok": True, "missing": {}, "extra": {"de": ["old.key"]}, "missing_files": [],
                 "placeholder_issues": [], "faults": [],
                 "warnings": [{"lang": "de", "key": "title", "severity": "warn", "msg": "text left in English"}]}
        rows = self.m.collect(check, sarif=None, strict=False)
        self.assertFalse(any(r.blocking for r in rows))
        rows = self.m.collect(check, sarif=None, strict=True)
        self.assertTrue(all(r.blocking for r in rows))


if __name__ == "__main__":
    unittest.main()
