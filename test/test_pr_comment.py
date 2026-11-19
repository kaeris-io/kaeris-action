"""P2.2: in check mode the action keeps ONE comment on the pull request with what is broken, and
edits it on the next push.

These tests run the action's own composite steps from action.yml under bash, the way a runner
does (`bash --noprofile --norc -eo pipefail`, one GITHUB_OUTPUT per step, `if:` conditions,
`steps.<id>.outputs`, a failed step skipping the ones after it). Inputs and env come from
action.yml itself. Two things are replaced, nothing else:

  * `kaeris` is a stub that prints a recorded `kaeris check --json` result (test/fixtures/check,
    recorded from CLI 0.2.39 on test/fixtures/*; `mix` from CLI 0.2.46) and copies the recorded SARIF;
  * the GitHub REST API is a local http.server on 127.0.0.1 that keeps the PR's comments.

Python runs with PYTHONUTF8=0: a Windows runner has no UTF-8 mode, and a test that sets it hides
every encoding bug. Offline, stdlib only.
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
BOT = ("github-actions[bot]", "Bot")
FIXTURES = {   # name: (source folder, languages, exit code the CLI gave when it was recorded)
    "broken": ("broken", "de", 1), "good": ("good", "de,fr", 0), "nofile": ("good", "de,ja", 1),
    "stale": ("stale", "de", 0), "umlaut": ("ümlaut", "de", 1),
    # CLI 0.2.46: pl with only one/other plural forms, three long translations, {name} -> {nam}
    "mix": ("mix", "pl", 1)}


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

def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def parse_action():
    with open(ACTION, encoding="utf-8") as f:
        lines = f.read().splitlines()
    inputs, section, cur = {}, None, None
    steps, step, key = [], None, None
    for ln in lines:
        if ln and not ln.startswith(" "):
            section = ln.split(":")[0]
            continue
        if section == "inputs":
            m = re.match(r"^  ([\w-]+):\s*$", ln)
            if m:
                cur = m.group(1)
                inputs[cur] = ""
                continue
            m = re.match(r"^    default:\s*(.*)$", ln)
            if m and cur:
                inputs[cur] = _unquote(m.group(1))
        elif section == "runs":
            m = re.match(r"^    - (\w[\w-]*):\s*(.*)$", ln)
            if m:
                step = {"env": {}, "run": None}
                steps.append(step)
                step[m.group(1)] = _unquote(m.group(2))
                key = None
                continue
            if step is None:
                continue
            m = re.match(r"^      (\w[\w-]*):\s*(.*)$", ln)
            if m:
                key = m.group(1)
                if key == "run":
                    step["run"] = [] if m.group(2).strip() == "|" else [m.group(2)]
                elif key not in ("env", "with"):
                    step[key] = _unquote(m.group(2))
                continue
            if key == "env":
                m = re.match(r"^        ([A-Z_][A-Z0-9_]*):\s*(.*)$", ln)
                if m:
                    step["env"][m.group(1)] = m.group(2).strip()
            elif key == "run" and (not ln.strip() or ln.startswith("        ")):
                step["run"].append(ln[8:])
    for s in steps:
        if s["run"] is not None:
            s["run"] = "\n".join(s["run"]) + "\n"
    return inputs, steps


def resolve(expr, inputs, ctx, outputs):
    def value(name):
        name = name.strip()
        if name.startswith("inputs."):
            return resolve(inputs.get(name[7:], ""), inputs, ctx, outputs)
        if name.startswith("steps."):
            _, sid, _, out = name.split(".", 3)
            return outputs.get(sid, {}).get(out, "")
        return ctx[name]

    def sub(m):
        inner = m.group(1)
        if "==" in inner:                                   # GitHub compares strings ignoring case
            a, b = inner.split("==", 1)
            return "true" if value(a).lower() == value(b).lower() else "false"
        return value(inner)
    return re.sub(r"\$\{\{\s*([\w.\- =]+?)\s*\}\}", sub, expr)


def condition(expr, inputs, outputs, failed):
    """The few expression shapes action.yml uses in `if:`."""
    if not expr:
        return not failed
    e = expr.strip()
    if e.startswith("${{") and e.endswith("}}"):
        e = e[3:-2].strip()
    uses_status = re.search(r"\b(always|failure|cancelled|success)\(\)", e)
    # GitHub's `==` on strings ignores case ('Check' == 'check' is true): lower both sides.
    e = re.sub(r"'[^']*'", lambda m: m.group(0).lower(), e)
    e = re.sub(r"inputs\.([\w-]+)", lambda m: repr(inputs.get(m.group(1), "").lower()), e)
    e = re.sub(r"steps\.([\w-]+)\.outputs\.([\w-]+)",
               lambda m: repr(outputs.get(m.group(1), {}).get(m.group(2), "").lower()), e)
    e = (e.replace("!cancelled()", "True").replace("always()", "True")
          .replace("failure()", repr(failed)).replace("success()", repr(not failed))
          .replace("&&", " and ").replace("||", " or ").replace("!=", "\0")
          .replace("!", " not ").replace("\0", " != "))
    ok = bool(eval(e, {"__builtins__": {}}, {"True": True, "False": False}))
    return ok if uses_status else (ok and not failed)


def read_outputs(path):
    out = {}
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        m = re.match(r"^([\w-]+)<<(.+)$", ln)
        if m:
            name, delim, buf = m.group(1), m.group(2), []
            i += 1
            while i < len(lines) and lines[i] != delim:
                buf.append(lines[i])
                i += 1
            out[name] = "\n".join(buf)
        elif "=" in ln:
            k, v = ln.split("=", 1)
            out[k] = v
        i += 1
    return out


# ── a GitHub that keeps one PR's comments ────────────────────────────────────────────────────

class FakeGitHub:
    def __init__(self):
        self.deny = None          # "all": every call 403 (no permission); "write": GET ok, writes 403
        self.patch_404 = False    # the comment was deleted between our GET and our PATCH
        self.race = False         # another job of the same matrix posts the same comment first
        self.poster = BOT         # who the token is
        self.deny_delete = False  # DELETE is refused (403)
        self.comments = []
        self.requests = []
        self.next_id = 1000
        gh = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj=None):
                raw = json.dumps(obj).encode() if obj is not None else b""
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
                if gh.deny == "all" or (gh.deny == "write" and method != "GET"):
                    return self._send(403, {"message": "Resource not accessible by integration"})
                if method == "GET" and url.path == "/user":
                    # A PAT can read its user; GITHUB_TOKEN and App installation tokens cannot.
                    if gh.poster[1] == "User":
                        return self._send(200, {"login": gh.poster[0], "type": "User"})
                    return self._send(403, {"message": "Resource not accessible by integration"})
                if method == "DELETE" and gh.deny_delete:
                    return self._send(403, {"message": "Resource not accessible by integration"})
                list_path = f"/repos/{REPO}/issues/{PR}/comments"
                m = re.match(rf"^/repos/{REPO}/issues/comments/(\d+)$", url.path)
                if method == "GET" and url.path == list_path:
                    q = parse_qs(url.query)
                    per = int(q.get("per_page", ["30"])[0])
                    page = int(q.get("page", ["1"])[0])
                    return self._send(200, gh.comments[(page - 1) * per: page * per])
                if method == "POST" and url.path == list_path:
                    if gh.race:
                        gh.race = False
                        gh.add(body["body"], *gh.poster)
                    return self._send(201, gh.add(body["body"], *gh.poster))
                if method in ("PATCH", "DELETE") and m:
                    for c in gh.comments:
                        if c["id"] == int(m.group(1)) and not (method == "PATCH" and gh.patch_404):
                            if method == "DELETE":
                                gh.comments.remove(c)
                                return self._send(204)
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

            def do_DELETE(self):
                self._handle("DELETE")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def add(self, body, login="github-actions[bot]", kind="Bot"):
        self.next_id += 1
        c = {"id": self.next_id, "body": body, "user": {"login": login, "type": kind},
             "html_url": f"https://github.com/{REPO}/pull/{PR}#issuecomment-{self.next_id}"}
        self.comments.append(c)
        return c

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def calls(self, method):
        return [r for r in self.requests if r["method"] == method]


# ── one run of the action ────────────────────────────────────────────────────────────────────

KAERIS_STUB = """#!/bin/sh
# Stub of the KAERIS CLI: the recorded `kaeris check --json` result, the recorded SARIF.
echo "$@" >> "$FAKE_ARGS"
env >> "$FAKE_ENV"
prev=""
for a in "$@"; do
  if [ "$prev" = "--sarif" ] && [ -n "$FAKE_SARIF" ]; then cp "$FAKE_SARIF" "$a"; fi
  prev="$a"
done
cat "$FAKE_CHECK_JSON"
exit "${FAKE_EXIT:-0}"
"""


class Run:
    def __init__(self, code, out, summary, args, cli_env, outputs):
        self.code, self.out, self.summary, self.args = code, out, summary, args
        self.cli_env, self.outputs = cli_env, outputs


def run_action(gh, fixture="broken", event="pull_request", inputs=None, fork=False, token=TOKEN,
               exit_code=None, api_url=None):
    bash = find_bash()
    defaults, steps = parse_action()
    folder, langs, recorded_exit = FIXTURES[fixture]
    given = {"mode": "check", "source": f"test/fixtures/{folder}/en.json", "languages": langs}
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
        base = {k: v for k, v in os.environ.items()
                if not k.startswith(("GITHUB_", "RUNNER_", "INPUT_", "KA_", "KAERIS_", "PYTHON"))}
        base.update({
            "PATH": stubs + os.pathsep + base.get("PATH", ""),
            "PYTHONUTF8": "0", "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
            "GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": event, "GITHUB_EVENT_PATH": _posix(event_path),
            "GITHUB_REPOSITORY": REPO, "GITHUB_API_URL": api_url or gh.url,
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_RUN_ID": "4242", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "f" * 40,
            "RUNNER_TEMP": tmp,
            "FAKE_CHECK_JSON": _posix(os.path.join(FIX, f"{fixture}.json")),
            "FAKE_SARIF": _posix(os.path.join(FIX, f"{fixture}.sarif")),
            "FAKE_EXIT": str(recorded_exit if exit_code is None else exit_code),
            "FAKE_ARGS": f"{tmp}/kaeris_args", "FAKE_ENV": f"{tmp}/kaeris_env",
        })
        outputs, failed, code, log = {}, False, 0, []
        for n, step in enumerate(steps):
            if step["run"] is None or step.get("name") == "Install KAERIS CLI":
                continue
            if not condition(step.get("if", ""), values, outputs, failed):
                continue
            env = dict(base)
            env["GITHUB_OUTPUT"] = f"{tmp}/out_{n}"
            env["GITHUB_STEP_SUMMARY"] = f"{tmp}/summary_{n}.md"
            for k, v in step["env"].items():
                env[k] = resolve(v, values, ctx, outputs)
            script_path = os.path.join(t, f"step_{n}.sh")
            with open(script_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(resolve(step["run"], values, ctx, outputs))
            p = subprocess.run([bash, "--noprofile", "--norc", "-eo", "pipefail", _posix(script_path)],
                               cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=120)
            log.append(p.stdout + p.stderr)
            if step.get("id"):
                outputs[step["id"]] = read_outputs(os.path.join(t, f"out_{n}"))
            if p.returncode != 0 and not failed:
                failed, code = True, p.returncode
        summary = ""
        for name in sorted(os.listdir(t)):
            if name.startswith("summary_"):
                with open(os.path.join(t, name), encoding="utf-8") as f:
                    summary += f.read()

        def read(name):
            path = os.path.join(t, name)
            if not os.path.isfile(path):
                return ""
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read()
        return Run(code, "".join(log), summary, read("kaeris_args"), read("kaeris_env"), outputs)


@unittest.skipUnless(find_bash(), "needs bash (Git Bash on Windows)")
class PRComment(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.addCleanup(self.gh.close)

    def marked(self):
        return [c for c in self.gh.comments if c["body"].startswith("<!-- kaeris-action")]

    # (a) a broken {name}: one comment with the marker, the job is red
    def test_a_broken_placeholder_posts_one_comment_and_fails(self):
        r = run_action(self.gh, "broken")
        self.assertNotEqual(r.code, 0, r.out)
        posts = self.gh.calls("POST")
        self.assertEqual(len(posts), 1, r.out)
        self.assertEqual(posts[0]["path"], f"/repos/{REPO}/issues/{PR}/comments")
        self.assertEqual(posts[0]["auth"], f"Bearer {TOKEN}")
        body = posts[0]["body"]["body"]
        self.assertTrue(body.startswith("<!-- kaeris-action"), body)
        for want in ("greeting", "{name}", "checkout.title", "| de |", "test/fixtures/broken/de.json"):
            self.assertIn(want, body)
        self.assertIn(f"https://github.com/{REPO}/blob/{HEAD_SHA}/test/fixtures/broken/de.json#L2", body)
        self.assertIn(f"https://github.com/{REPO}/actions/runs/4242", body)
        self.assertIn("https://kaeris.dev/?utm_source=action-comment", body)
        self.assertIn("Checked de against `test/fixtures/broken/en.json` at `0123456`.", body)
        self.assertIn("Checked by [KAERIS]", body)
        self.assertNotIn(TOKEN, r.out)
        self.assertLess(len(body), 65536)
        self.assertEqual(r.outputs["comment"]["comment-url"], self.gh.comments[0]["html_url"])

    # (b) the next push edits that comment, it does not add a second one
    def test_b_second_run_patches_the_same_comment(self):
        run_action(self.gh, "broken")
        first = self.marked()[0]["id"]
        r = run_action(self.gh, "broken")
        self.assertNotEqual(r.code, 0, r.out)
        self.assertEqual(len(self.gh.calls("POST")), 1, r.out)
        patches = self.gh.calls("PATCH")
        self.assertEqual(len(patches), 1, r.out)
        self.assertEqual(patches[0]["path"], f"/repos/{REPO}/issues/comments/{first}")
        self.assertEqual(len(self.marked()), 1)

    # (c) clean after broken: the comment says so and the job is green
    def test_c_clean_run_updates_existing_comment_to_clean(self):
        run_action(self.gh, "broken")
        # the next push of the same PR: same source and languages, now clean
        r = run_action(self.gh, "good", inputs={"source": "test/fixtures/broken/en.json", "languages": "de"})
        self.assertEqual(r.code, 0, r.out)
        self.assertEqual(len(self.gh.calls("POST")), 1)
        self.assertEqual(len(self.gh.calls("PATCH")), 1, r.out)
        body = self.marked()[0]["body"]
        self.assertIn("no problems", body.lower())
        self.assertIn("Nothing left to fix.", body)
        self.assertNotIn("greeting", body)

    def test_c_clean_run_without_comment_creates_nothing(self):
        r = run_action(self.gh, "good")
        self.assertEqual(r.code, 0, r.out)
        self.assertEqual(self.gh.calls("POST") + self.gh.calls("PATCH"), [])
        self.assertEqual(self.gh.comments, [])

    # (d) fork PR / no pull-requests: write: a warning, and the findings alone decide the job
    def test_d_fork_broken_still_fails_on_findings_with_a_warning(self):
        self.gh.deny = "all"
        r = run_action(self.gh, "broken", fork=True)
        self.assertEqual(r.code, 1, r.out)
        self.assertIn("::warning", r.out)
        self.assertIn("fork", r.out)
        self.assertIn("job summary", r.out)
        self.assertNotIn(TOKEN, r.out)

    def test_d_forbidden_clean_stays_green_and_quiet(self):
        self.gh.deny = "all"
        r = run_action(self.gh, "good")
        self.assertEqual(r.code, 0, r.out)
        self.assertNotIn("::warning", r.out)

    def test_d_forbidden_broken_same_repo(self):
        self.gh.deny = "all"
        r = run_action(self.gh, "broken")
        self.assertEqual(r.code, 1, r.out)
        self.assertIn("::warning", r.out)
        self.assertIn("pull-requests: write", r.out)

    def test_d_read_ok_write_forbidden(self):
        self.gh.deny = "write"
        r = run_action(self.gh, "broken")
        self.assertEqual(r.code, 1, r.out)
        self.assertIn("::warning", r.out)
        self.assertIn("pull-requests: write", r.out)
        self.assertEqual(self.gh.comments, [])

    def test_d_no_token_warns_and_calls_nothing(self):
        r = run_action(self.gh, "broken", token="")
        self.assertEqual(r.code, 1, r.out)
        self.assertIn("::warning", r.out)
        self.assertEqual(self.gh.requests, [])

    def test_d_api_unreachable_warns_verdict_unchanged(self):
        r = run_action(self.gh, "broken", api_url="http://127.0.0.1:9")
        self.assertEqual(r.code, 1, r.out)
        self.assertIn("::warning", r.out)
        r = run_action(self.gh, "good", api_url="http://127.0.0.1:9")
        self.assertEqual(r.code, 0, r.out)

    # (e) not a pull request: the comment is not touched at all
    def test_e_push_event_touches_no_comment(self):
        run_action(self.gh, "broken")
        before = [dict(c) for c in self.gh.comments]
        n = len(self.gh.requests)
        r = run_action(self.gh, "good", event="push",
                       inputs={"source": "test/fixtures/broken/en.json", "languages": "de"})
        self.assertEqual(r.code, 0, r.out)
        self.assertEqual(len(self.gh.requests), n)
        self.assertEqual(self.gh.comments, before)

    def test_e_pull_request_target_is_not_a_comment_event(self):
        # pull_request_target checks out the BASE by default: findings would be about main.
        r = run_action(self.gh, "broken", event="pull_request_target")
        self.assertEqual(r.code, 1, r.out)
        self.assertEqual(self.gh.requests, [])

    def test_e_comment_false_touches_nothing(self):
        r = run_action(self.gh, "broken", inputs={"comment": "false"})
        self.assertEqual(r.code, 1, r.out)
        self.assertEqual(self.gh.requests, [])

    # B1: only our own comment is ours
    def test_b1_a_strangers_comment_with_our_marker_is_not_edited(self):
        run_action(self.gh, "broken")
        stolen = self.gh.comments[0]
        stolen["user"] = {"login": "stranger", "type": "User"}
        stolen["body"] = stolen["body"].split("\n", 1)[0] + "\nmine now"
        run_action(self.gh, "broken")
        self.assertEqual(len(self.gh.calls("POST")), 2)
        self.assertEqual(self.gh.calls("PATCH"), [])
        self.assertTrue(stolen["body"].endswith("mine now"))

    def test_b1_a_human_quoting_the_marker_is_not_hijacked(self):
        run_action(self.gh, "broken")
        first = self.gh.comments[0]
        quoted = self.gh.add("> " + first["body"].split("\n", 1)[0] + "\n> why?", "someone", "User")
        run_action(self.gh, "broken")
        self.assertTrue(quoted["body"].endswith("why?"))
        self.assertEqual(len(self.gh.calls("POST")), 1)

    def test_b1_own_token_with_comment_author(self):
        self.gh.poster = ("kaeris-ci", "User")
        run_action(self.gh, "broken", inputs={"comment-author": "kaeris-ci"})
        run_action(self.gh, "broken", inputs={"comment-author": "kaeris-ci"})
        self.assertEqual(len(self.gh.calls("POST")), 1)
        self.assertEqual(len(self.gh.calls("PATCH")), 1)

    # B2: one comment per check, not per source file
    def test_b2_language_matrix_keeps_separate_comments(self):
        run_action(self.gh, "broken", inputs={"languages": "de"})
        red = self.gh.comments[0]["body"]
        r = run_action(self.gh, "good", inputs={"source": "test/fixtures/broken/en.json", "languages": "fr"})
        self.assertEqual(r.code, 0, r.out)
        self.assertEqual(self.gh.calls("PATCH"), [])
        self.assertEqual(self.gh.comments[0]["body"], red)   # the red job's comment does not lie

    def test_b2_comment_key_separates_two_steps(self):
        run_action(self.gh, "broken", inputs={"comment-key": "web"})
        run_action(self.gh, "broken", inputs={"comment-key": "app"})
        self.assertEqual(len(self.gh.calls("POST")), 2)
        self.assertEqual(len(self.marked()), 2)

    def test_b2_os_matrix_race_keeps_the_oldest(self):
        self.gh.race = True
        r = run_action(self.gh, "broken")
        self.assertEqual(r.code, 1, r.out)
        self.assertEqual(len(self.marked()), 1, r.out)
        deletes = self.gh.calls("DELETE")
        self.assertEqual(len(deletes), 1, r.out)
        self.assertEqual(self.marked()[0]["id"], 1001)        # the rival's, posted first
        self.assertTrue(deletes[0]["path"].endswith("/1002"))

    # Round 3, 1: a PAT or App token without comment-author must not repost on every push
    def test_r3_pat_without_comment_author_edits_its_own_comment(self):
        self.gh.poster = ("alice", "User")
        pat = {"github-token": "ghp_personal_not_real"}
        r1 = run_action(self.gh, "broken", inputs=pat)
        r2 = run_action(self.gh, "broken", inputs=pat)
        self.assertEqual(r2.code, 1, r2.out)
        self.assertLessEqual(len(self.marked()), 1, r1.out + r2.out)
        self.assertEqual(len(self.gh.calls("PATCH")), 1, r2.out)     # found via GET /user
        r3 = run_action(self.gh, "good", inputs={**pat, "source": "test/fixtures/broken/en.json",
                                                 "languages": "de"})
        self.assertIn("no problems", self.marked()[0]["body"], r3.out)

    def test_r3_app_token_without_comment_author_warns_and_keeps_one(self):
        self.gh.poster = ("myapp[bot]", "Bot")
        app = {"github-token": "ghs_app_installation_not_real"}
        r1 = run_action(self.gh, "broken", inputs=app)
        self.assertIn("comment-author: myapp[bot]", r1.out)
        self.assertIn("::warning", r1.out)
        r2 = run_action(self.gh, "broken", inputs=app)
        self.assertLessEqual(len(self.marked()), 1, r2.out)
        self.assertIn("comment-author: myapp[bot]", r2.out)

    def test_r3_default_token_does_not_ask_for_the_user(self):
        run_action(self.gh, "broken")
        self.assertFalse([r for r in self.gh.requests if r["path"] == "/user"])

    # Round 3, 2: `if:` in GitHub ignores case, bash does not
    def test_r3_mode_with_a_capital_is_check_in_every_step(self):
        # GitHub's `if:` reads 'Check' as 'check'; bash now does too, and the later steps follow
        # what the run step decided (is-check), not their own reading of the input.
        r = run_action(self.gh, "broken", inputs={"mode": "Check"})
        self.assertEqual(r.code, 1, r.out)
        self.assertEqual(r.outputs["run"].get("is-check"), "true", r.out)
        self.assertNotIn("translate", r.args)
        self.assertEqual(len(self.gh.calls("POST")), 1, r.out)
        self.assertIn("kaeris check found", r.out)

    def test_r3_unknown_mode_is_a_clear_error(self):
        r = run_action(self.gh, "good", inputs={"mode": "lint"})
        self.assertEqual(r.code, 2, r.out)
        self.assertIn("::error::", r.out)
        self.assertIn("mode", r.out)
        self.assertNotIn("usage: summarize.py", r.out)
        self.assertEqual(self.gh.requests, [])
        self.assertEqual(r.args, "")                        # the CLI never ran

    # Round 3, 3: one rule for cta in bash and Python: only "false" turns it off
    def test_r3_cta_capital_false_is_not_false_like_bash(self):
        r = run_action(self.gh, "broken", inputs={"cta": "False"})
        body = self.gh.calls("POST")[0]["body"]["body"]
        self.assertIn("kaeris.dev", body)
        self.assertIn("kaeris.dev", r.summary)

    # Round 3, 4: exit 2 is not "broken translations"
    def test_r3_gate_says_what_exit_2_means(self):
        r = run_action(self.gh, "broken", exit_code=2)
        self.assertEqual(r.code, 2, r.out)
        self.assertIn("kaeris check failed (exit 2)", r.out)
        self.assertNotIn("found incomplete/broken translations", r.out)

    # Pre-tag round 2: exit 2 says nothing about the translations; the old "N to fix" must not stay
    def test_exit_2_turns_the_existing_comment_into_not_finished(self):
        run_action(self.gh, "broken")
        r = run_action(self.gh, "broken", exit_code=2)
        self.assertEqual(r.code, 2, r.out)
        self.assertEqual(len(self.gh.calls("POST")), 1, r.out)
        self.assertEqual(len(self.gh.calls("PATCH")), 1, r.out)
        body = self.marked()[0]["body"]
        self.assertIn("### KAERIS i18n check: did not finish (exit 2)", body)
        self.assertIn(f"https://github.com/{REPO}/actions/runs/4242", body)
        self.assertNotIn("to fix", body)
        self.assertNotIn("greeting", body)

    def test_exit_2_without_a_comment_posts_nothing(self):
        r = run_action(self.gh, "broken", exit_code=2)
        self.assertEqual(r.code, 2, r.out)
        self.assertEqual(self.gh.calls("POST") + self.gh.calls("PATCH"), [])

    # Pre-tag round 2: real CLI 0.2.46 output, keyless findings each on their own row
    def test_real_mix_rows_link_their_lines(self):
        r = run_action(self.gh, "mix")
        self.assertEqual(r.code, 1, r.out)
        body = self.marked()[0]["body"]
        self.assertIn("### KAERIS i18n check: 3 to fix, 3 warnings", body)
        save = next(ln for ln in body.splitlines() if "`save`" in ln)
        self.assertIn("test/fixtures/mix/pl.json#L4", save)
        self.assertIn("warning: translation +42 characters", save)

    # Round 3, 5: a refused DELETE is said, not swallowed
    def test_r3_refused_delete_of_a_duplicate_warns(self):
        self.gh.race = True
        self.gh.deny_delete = True
        r = run_action(self.gh, "broken")
        self.assertEqual(r.code, 1, r.out)
        self.assertIn("::warning", r.out)
        self.assertIn("duplicate", r.out)

    def test_patch_404_falls_back_to_post(self):
        run_action(self.gh, "broken")
        self.gh.patch_404 = True
        r = run_action(self.gh, "broken")
        self.assertEqual(r.code, 1, r.out)
        self.assertEqual(len(self.gh.calls("POST")), 2, r.out)
        self.assertNotIn("::warning", r.out)

    # N1: the token reaches only the comment step
    def test_n1_token_not_in_the_cli_environment(self):
        r = run_action(self.gh, "broken")
        self.assertTrue(r.cli_env, "the stub did not run")
        self.assertNotIn(TOKEN, r.cli_env)
        self.assertEqual(len(self.gh.calls("POST")), 1)

    # N3: a language without a file is one row, first, not every key of the source
    def test_n3_missing_file_is_one_row_first(self):
        r = run_action(self.gh, "nofile")
        self.assertEqual(r.code, 1, r.out)
        body = self.gh.calls("POST")[0]["body"]["body"]
        rows = [ln for ln in body.splitlines() if ln.startswith("| ") and not ln.startswith("| File")]
        self.assertEqual(len(rows), 1, body)
        self.assertIn("no locale file", rows[0])
        self.assertIn("test/fixtures/good/ja.json", rows[0])
        self.assertNotIn("checkout.pay", body)
        self.assertNotIn("](", rows[0])                        # nothing to link to

    # N4: what blocks is what made the check fail
    def test_n4_exit_zero_means_nothing_blocks(self):
        r = run_action(self.gh, "broken", exit_code=0)
        self.assertEqual(r.code, 0, r.out)
        self.assertEqual(self.gh.calls("POST"), [])

    def test_n4_strict_capital_true_is_not_strict_like_bash(self):
        r = run_action(self.gh, "stale", inputs={"strict": "True"})
        self.assertNotIn("--strict", r.args)
        self.assertEqual(r.code, 0, r.out)
        self.assertEqual(self.gh.calls("POST"), [])

    def test_n4_strict_makes_a_stale_key_blocking(self):
        r = run_action(self.gh, "stale", inputs={"strict": "true"}, exit_code=1)
        self.assertIn("--strict", r.args)
        self.assertEqual(r.code, 1, r.out)
        body = self.gh.calls("POST")[0]["body"]["body"]
        row = next(ln for ln in body.splitlines() if "old_banner" in ln)
        self.assertNotIn("warning:", row)
        self.assertIn('--strict', body)
        self.assertIn('--source "test/fixtures/stale/en.json"', body)

    # N5: around it
    def test_user_sarif_file_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            mine = os.path.join(d, "kaeris.sarif")
            r = run_action(self.gh, "broken", inputs={"sarif-file": _posix(mine)})
            self.assertTrue(os.path.isfile(mine), r.out)
            with open(mine, encoding="utf-8") as a, open(os.path.join(FIX, "broken.sarif"), encoding="utf-8") as b:
                self.assertEqual(a.read(), b.read())
        self.assertIn(f"--sarif {_posix(mine)}", r.args)

    def test_non_ascii_keys_and_paths_without_utf8_mode(self):
        r = run_action(self.gh, "umlaut")
        self.assertEqual(r.code, 1, r.out)
        body = self.gh.calls("POST")[0]["body"]["body"]
        self.assertIn("`grüße`", body)
        self.assertIn("/test/fixtures/%C3%BCmlaut/de.json#L2", body)
        self.assertIn("grüße", r.summary)

    def test_our_comment_found_past_the_first_page(self):
        for i in range(150):
            self.gh.add(f"review comment {i}", "someone", "User")
        run_action(self.gh, "broken")
        self.assertEqual(len(self.gh.calls("POST")), 1)
        run_action(self.gh, "broken")
        self.assertEqual(len(self.gh.calls("POST")), 1, "second run must PATCH the comment on page 2")
        self.assertEqual(len(self.gh.calls("PATCH")), 1)

    def test_job_summary_lists_every_finding(self):
        r = run_action(self.gh, "broken", event="push")
        self.assertIn("checkout.title", r.summary)
        self.assertIn("greeting", r.summary)

    def test_cta_false_drops_the_link(self):
        run_action(self.gh, "broken", inputs={"cta": "false"})
        body = self.gh.calls("POST")[0]["body"]["body"]
        self.assertNotIn("kaeris.dev", body)
        self.assertIn("`comment: false` turns it off", body)

    def test_check_is_still_called_with_the_v2_flags(self):
        r = run_action(self.gh, "broken")
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

    def body(self, check, strict=False):
        rows = self.m.collect(check, sarif=None, strict=strict)
        return self.m.build_comment(rows, source="locales/en.json", langs=["de"], failing=True)

    def test_markdown_in_values_is_escaped(self):
        check = {"ok": False, "missing": {}, "extra": {}, "missing_files": [], "placeholder_issues": [],
                 "warnings": [],
                 "faults": [{"lang": "de", "key": "a|b`c", "severity": "error",
                             "msg": "lost inline tag <b> | see @admin and #12 **now**"}]}
        body = self.body(check)
        row = next(ln for ln in body.splitlines() if "lost inline tag" in ln)
        self.assertEqual(row.count(" | "), 3, row)
        self.assertNotIn("<b>", row)
        self.assertNotIn("@admin", row)
        self.assertNotIn("#12", row)
        self.assertNotIn("**now**", row)
        self.assertNotIn("`c", row)

    def test_no_invisible_characters_in_the_source(self):
        with open(os.path.join(ROOT, "pr_comment.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("\u200b", text)                      # written as an escape, visible in review

    def test_size_is_bounded_and_says_how_many_are_left(self):
        missing = {"de": [f"section.key_{i:05d}_" + "x" * 150 for i in range(3000)]}
        check = {"ok": False, "missing": missing, "extra": {}, "missing_files": [],
                 "placeholder_issues": [], "faults": [], "warnings": []}
        body = self.body(check)
        self.assertLess(len(body), 65536)
        self.assertRegex(body, r"\d+ more")
        rows = self.m.collect(check, sarif=None, strict=False)
        summary = self.m.build_summary(rows)
        self.assertLessEqual(len(summary.encode("utf-8")), 512 * 1024)

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

    def table_rows(self, body):
        return [ln for ln in body.splitlines() if ln.startswith("| ") and not ln.startswith("| File")]

    def test_real_cli_0_2_46_output_keeps_fixes_and_warnings_apart(self):
        # Found by the pre-tag critic: 0 keyed errors + 3 file-level warnings were merged into the
        # keyless plural row, the header said "2 to fix" and the cell was cut with no word of it.
        with open(os.path.join(FIX, "mix.json"), encoding="utf-8") as f:
            check = json.load(f)
        body = self.body(check)
        self.assertIn("### KAERIS i18n check: 3 to fix, 3 warnings", body)
        rows = self.table_rows(body)
        self.assertEqual(len(rows), 6, body)
        plural = [r for r in rows if "plural form missing" in r]
        self.assertEqual(len(plural), 2, body)
        self.assertIn("`items_few`", plural[0])
        self.assertIn("`items_many`", plural[1])
        for r in plural:
            self.assertNotIn("overflow", r)
            self.assertNotIn("warning:", r)
        for key, chars in (("save", 42), ("open", 42), ("close", 37)):
            row = next(r for r in rows if f"`{key}`" in r)
            self.assertIn(f"warning: translation +{chars} characters vs source — may overflow UI", row)
        greeting = next(r for r in rows if "`greeting`" in r)
        self.assertIn("lost placeholder {name}; invented placeholder {nam}", greeting)
        self.assertLess(rows.index(greeting), rows.index(next(r for r in rows if "`save`" in r)))
        self.assertNotIn("…", body)

    def test_one_key_with_an_error_and_a_warning_is_two_rows(self):
        check = {"ok": False, "missing": {}, "extra": {}, "missing_files": [], "placeholder_issues": [],
                 "faults": [{"lang": "de", "key": "title", "severity": "error", "msg": "lost inline tag <b>"}],
                 "warnings": [{"lang": "de", "key": "title", "severity": "warn", "msg": "text left in English"}]}
        body = self.body(check)
        self.assertIn("### KAERIS i18n check: 1 to fix, 1 warning", body)
        rows = self.table_rows(body)
        self.assertEqual(len(rows), 2, body)
        self.assertNotIn("English", rows[0])
        self.assertIn("warning: text left in English", rows[1])

    def test_a_long_cell_says_how_many_problems_it_leaves_out(self):
        faults = [{"lang": "de", "key": "k", "severity": "error", "msg": f"problem number {i} " + "x" * 60}
                  for i in range(8)]
        check = {"ok": False, "missing": {}, "extra": {}, "missing_files": [], "placeholder_issues": [],
                 "faults": faults, "warnings": []}
        row = self.table_rows(self.body(check))[0]
        shown = len(re.findall(r"problem number \d", row))
        self.assertGreaterEqual(shown, 1)
        self.assertLess(shown, 8)
        self.assertIn(f"and {8 - shown} more", row)

    def test_exit_zero_merges_rows_back_to_one_per_key(self):
        check = {"ok": False, "missing": {}, "extra": {}, "missing_files": [], "placeholder_issues": [],
                 "faults": [{"lang": "de", "key": "title", "severity": "error", "msg": "lost inline tag <b>"}],
                 "warnings": [{"lang": "de", "key": "title", "severity": "warn", "msg": "text left in English"}]}
        rows = self.m.collect(check, sarif=None, strict=False, clean=True)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].blocking)

    def head(self, n_fix, n_warn, failing=True):
        check = {"ok": not n_fix, "missing": {"de": [f"fix.k{i}" for i in range(n_fix)]},
                 "extra": {"de": [f"old.k{i}" for i in range(n_warn)]}, "missing_files": [],
                 "placeholder_issues": [], "faults": [], "warnings": []}
        rows = self.m.collect(check, sarif=None, strict=False)
        body = self.m.build_comment(rows, source="locales/en.json", langs=["de"], failing=failing)
        return next(ln for ln in body.splitlines() if ln.startswith("### "))

    def test_header_counts_fixes_and_warnings_apart(self):
        # one system: "<n> to fix", then ", <n> warning(s)" only when there are any
        cases = {(2, 1): "2 to fix, 1 warning", (1, 2): "1 to fix, 2 warnings",
                 (1, 1): "1 to fix, 1 warning", (3, 0): "3 to fix", (1, 0): "1 to fix",
                 (0, 1): "nothing to fix, 1 warning", (0, 3): "nothing to fix, 3 warnings",
                 (0, 0): "no problems"}
        for (n_fix, n_warn), want in cases.items():
            with self.subTest(fix=n_fix, warn=n_warn):
                self.assertEqual(self.head(n_fix, n_warn), f"### KAERIS i18n check: {want}")


if __name__ == "__main__":
    unittest.main()
