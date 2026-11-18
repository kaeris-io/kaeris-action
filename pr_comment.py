#!/usr/bin/env python3
"""Check mode: one comment on the pull request with what `kaeris check` found, edited in place on
every push, and the full list of findings in the job summary.

Why a comment: SARIF annotations need GitHub Code Security in a private repo, and a job summary
is a page nobody opens. A comment is where the reviewer already is, and it needs nothing but the
workflow's own GITHUB_TOKEN with `pull-requests: write`.

Rules this file keeps:

  * It never decides the job. It runs in its own step after `kaeris check`, the gate is the
    step after it, and this script always exits 0. No permission, a fork PR or a GitHub outage
    is one `::warning::` line, never a red job and never a green one that should be red.
  * What blocks is what made the check fail: `--check-exit` is the CLI's exit code. Exit 0
    means nothing in the comment says "fails", whatever the rows look like.
  * One comment per check: the hidden marker is a hash of source, out and languages (or the
    `comment-key` input), so a language matrix or two steps over one source keep apart, and a
    clean job never edits a red job's comment into "no problems".
  * Only our own comments: the marker at the start of the body AND the author we post as
    (`github-actions[bot]`, or `comment-author` for a PAT / App token). A human who copies or
    quotes the marker is never edited.
  * Edited (PATCH), not reposted. If two jobs of an OS matrix post at once, the newer one is
    deleted after a re-read and the oldest stays. A comment deleted under us is posted again.
  * Created only when the check failed. A clean or warnings-only run edits an existing comment
    (so "fixed" is visible) and otherwise creates nothing.
  * Only on `pull_request`. `pull_request_target` checks out the base branch by default, so
    the findings would be about main, and running PR code there with a write token is the
    well-known "pwn request". Other events do not touch the API at all.
  * Every value from the locale files is escaped before it reaches markdown: a translation can
    hold `|`, `<b>`, `@someone` or `**`, and must show as text, not break the table, render
    HTML or ping a user. The body stays under GitHub's 65 536 character limit.
  * Files and stdout are UTF-8 whatever the runner's locale (a Windows runner has no UTF-8
    mode).

The upsert and the table follow the KAERIS GitHub App's own PR comment (marker, every page of
comments, PATCH else POST; escaped table cells). Copied as ideas, not imported: this runs on a
bare runner with the stdlib only.

Usage (from action.yml):
  pr_comment.py --check-json AFTER.json --check-exit 1 [--sarif CHECK.sarif] \
      --source locales/en.json --langs de,fr [--out DIR] [--strict true] \
      [--summary-file "$GITHUB_STEP_SUMMARY"] [--github-output "$GITHUB_OUTPUT"]
Environment: KA_COMMENT, KA_GITHUB_TOKEN, KA_COMMENT_KEY, KA_COMMENT_AUTHOR, KA_CTA, and the
runner's GITHUB_* variables.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

TITLE = "KAERIS PR comment"
SITE_URL = "https://kaeris.dev/?utm_source=action-comment"   # the site reads utm_source, not ref
DEFAULT_AUTHOR = "github-actions[bot]"
GITHUB_LIMIT = 65536
BODY_BUDGET = 60000          # characters: room under GitHub's limit for the header and the footer
SUMMARY_BUDGET = 512 * 1024  # bytes: a job summary step is capped at 1 MiB, leave half to the rest
COMMENT_ROWS = 50            # a comment is for reading; the job summary has the rest
SUMMARY_ROWS = 1000
PR_EVENTS = ("pull_request",)
ZWSP = chr(0x200B)           # zero-width space: breaks @mentions and #123 autolinks, shows nothing
MISSING = "missing in this language"


# ── findings ─────────────────────────────────────────────────────────────────────────────────

class Row:
    __slots__ = ("lang", "key", "file", "line", "blocking", "problems", "expected")

    def __init__(self, lang, key):
        self.lang, self.key, self.file, self.line = lang, key, None, None
        self.blocking, self.problems, self.expected = False, [], None


def load_json(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def files_by_lang(sarif):
    """{lang: repo-relative path of its locale file} from the SARIF `kaeris check` wrote. The
    --json result names no files; the SARIF does, resolved by the CLI's own layout rules (for a
    language with no file it is where the file was expected)."""
    out = {}
    if not isinstance(sarif, dict):
        return out
    for run in sarif.get("runs") or []:
        for res in run.get("results") or []:
            text = ((res.get("message") or {}).get("text")) or ""
            m = re.match(r"^\[([^\]]+)\] ", text)
            if not m or "no locale file found" in text:
                continue
            try:
                uri = res["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
            except (KeyError, IndexError, TypeError):
                continue
            out.setdefault(m.group(1), uri)
    return out


def line_of_key(path, key):
    """1-based line of `key` in a JSON/ARB file, or None. Textual, like the CLI's SARIF writer,
    but a nested key is walked segment by segment ("checkout.title": "checkout", then the first
    "title" after it), so a leaf name used in two sections lands in the right one."""
    if not path or not key:
        return None
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return None

    def find(name, start):
        pat = re.compile(r'^\s*"' + re.escape(name) + r'"\s*:')
        return next((n for n in range(start, len(lines)) if pat.match(lines[n])), None)

    whole = find(key, 0)
    if whole is not None:
        return whole + 1
    pos = 0
    for part in key.split("."):
        n = find(part, pos)
        if n is None:
            return None
        pos = n + 1
    return pos


def collect(check, sarif=None, strict=False):
    """Rows, one per (language, key), most serious first. `kaeris check` reports a broken
    placeholder twice (placeholder_issues and faults): it is said once here. A language with no
    file is one row (the CLI also lists every source key as missing for it)."""
    check = check or {}
    no_file = [lang for lang in check.get("missing_files") or []]
    rows = {}

    def add(lang, key, blocking, problem):
        r = rows.setdefault((lang or "", key or ""), Row(lang or "", key or ""))
        r.blocking = r.blocking or bool(blocking)
        if problem not in r.problems:
            r.problems.append(problem)
        return r

    for lang, keys in (check.get("missing") or {}).items():
        if lang in no_file:
            continue
        for key in keys:
            add(lang, key, True, MISSING)
    for f in check.get("faults") or []:
        add(f.get("lang"), f.get("key"), f.get("severity", "error") == "error" or strict, f.get("msg", ""))
    for issue in check.get("placeholder_issues") or []:
        r = rows.get((issue.get("lang") or "", issue.get("key") or ""))
        if r and any("placeholder" in p for p in r.problems):
            continue
        bits = []
        if issue.get("missing"):
            bits.append("lost " + " ".join(issue["missing"]))
        if issue.get("added"):
            bits.append("invented " + " ".join(issue["added"]))
        add(issue.get("lang"), issue.get("key"), True,
            "placeholder mismatch: " + (", ".join(bits) or "changed"))
    for w in check.get("warnings") or []:
        add(w.get("lang"), w.get("key"), strict, w.get("msg", ""))
    for lang, keys in (check.get("extra") or {}).items():
        for key in keys:
            add(lang, key, strict, "not in the source (left over after a rename or delete?)")

    files = files_by_lang(sarif)
    for (lang, key), r in rows.items():
        r.file = files.get(lang)
        if r.file and key and MISSING not in r.problems:
            r.line = line_of_key(r.file, key)
    first = []
    for lang in no_file:
        r = Row(lang, "")
        r.blocking, r.expected = True, files.get(lang) or ""
        r.problems.append("no locale file")
        first.append(r)
    rest = list(rows.values())
    rest.sort(key=lambda r: not r.blocking)               # stable: CLI order within each group
    return first + rest


# ── markdown ─────────────────────────────────────────────────────────────────────────────────

_MD = re.compile(r"([\\`*_\[\]<>|~])")


def esc(text, limit=200):
    """Plain text in a table cell: shown as typed, never rendered."""
    s = " ".join(str(text).split())
    if len(s) > limit:
        s = s[:limit - 1] + "…"
    s = s.replace("&", "&amp;")
    s = _MD.sub(r"\\\1", s)
    s = s.replace("@", "@" + ZWSP)                        # no mention ping
    return re.sub(r"#(?=\d)", "#" + ZWSP, s)              # no issue autolink


def code(text, limit=120):
    """A value in a code span: backticks cannot close it, a pipe cannot split the table."""
    s = " ".join(str(text).split()).replace("`", "'")
    if len(s) > limit:
        s = s[:limit - 1] + "…"
    return "`" + s.replace("|", "\\|") + "`"


def comment_id(source, out, langs, key=""):
    """Which check this comment belongs to. An explicit `comment-key` wins; otherwise a stable
    hash of what the check looked at, so a language matrix or two steps keep apart."""
    key = (key or "").strip()
    if key and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", key):
        return key
    basis = key or "\n".join([source or "", out or "", ",".join(sorted(langs))])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]


def marker(cid):
    return f"<!-- kaeris-action check {cid} -->"


def _blob(server, repo, sha, path, line):
    if not (server and repo and sha and path):
        return None
    url = f"{server.rstrip('/')}/{repo}/blob/{sha}/{urllib.parse.quote(path, safe='/')}"
    return url + (f"#L{line}" if line else "")


def table(rows, limit, budget, link=None, measure=len):
    """(lines, shown): at most `limit` rows and `budget` (as `measure` counts it)."""
    out = ["| File | Key | Language | Problem |", "|---|---|---|---|"]
    size, shown = sum(measure(x) + 1 for x in out), 0
    for r in rows[:limit]:
        if r.expected is not None:
            where, key = "–", "–"
            problem = "no locale file" + (f" (expected {code(r.expected)})" if r.expected else "")
        else:
            if r.file:
                label = code(r.file + (f":{r.line}" if r.line else ""))
                url = link(r.file, r.line) if link else None
                where = f"[{label}]({url})" if url else label
            else:
                where = "–"
            key = code(r.key) if r.key else "–"
            problem = esc(("" if r.blocking else "warning: ") + "; ".join(p for p in r.problems if p))
        line = f"| {where} | {key} | {esc(r.lang, 20)} | {problem} |"
        if size + measure(line) + 1 > budget:
            break
        out.append(line)
        size += measure(line) + 1
        shown += 1
    return out, shown


def _q(value):
    return '"' + str(value).replace('"', '\\"') + '"'


def local_command(source, langs, out_dir=None, strict=False):
    cmd = f"kaeris check --source {_q(source)} --langs {','.join(langs)}"
    if out_dir:
        cmd += f" --out {_q(out_dir)}"
    if strict:
        cmd += " --strict"
    return cmd


def build_comment(rows, source, langs, failing, head_sha=None, repo=None, server=None, run_url=None,
                  out_dir=None, strict=False, cid=None, cta=True):
    n_fix = sum(r.blocking for r in rows) if failing else 0
    n_review = len(rows) - n_fix
    if n_fix:
        head = f"### KAERIS i18n check: {n_fix} problem{'s' if n_fix != 1 else ''} to fix"
    elif n_review:
        head = f"### KAERIS i18n check: no problems to fix, {n_review} to review"
    else:
        head = "### KAERIS i18n check: no problems"
    checked = f"Checked {esc(', '.join(langs))} against {code(source)}"
    if head_sha:
        checked += f" at `{head_sha[:7]}`"
    checked += "."
    md = [marker(cid or comment_id(source, out_dir, langs)), head, ""]
    summary_link = f"[job summary]({run_url})" if run_url else "job summary"
    if not rows:
        md.append(f"{checked} Nothing left to fix.")
    else:
        md.append(checked)
        md.append("")
        lines, shown = table(rows, COMMENT_ROWS, BODY_BUDGET,
                             lambda p, l: _blob(server, repo, head_sha, p, l))
        md.extend(lines)
        if shown < len(rows):
            md.append("")
            md.append(f"{len(rows) - shown} more not listed here; the {summary_link} has all of them.")
        md.append("")
        note = "The check fails until the problems above are fixed." if n_fix else "Nothing blocks the merge."
        if n_review and not strict:
            note += " Warnings fail the check only with `strict: true`."
        md.append(note)
        md.append(f"Full report: {summary_link}. Locally: `pip install kaeris`, then "
                  f"{code(local_command(source, langs, out_dir, strict), 400)}.")
    md.append("")
    if cta:
        md.append(f"<sub>Checked by [KAERIS]({SITE_URL}) (free, no API key). "
                  "Updated on every push; `comment: false` turns it off.</sub>")
    else:
        md.append("<sub>Updated on every push; `comment: false` turns it off.</sub>")
    body = "\n".join(md) + "\n"
    return body[:GITHUB_LIMIT - 1]


def build_summary(rows, link=None):
    if not rows:
        return ""
    head = ["", f"### Findings ({len(rows)})", ""]
    lines, shown = table(rows, SUMMARY_ROWS, SUMMARY_BUDGET - 200, link,
                         measure=lambda s: len(s.encode("utf-8")))
    md = head + lines
    if shown < len(rows):
        md += ["", f"{len(rows) - shown} more not listed; run `kaeris check` locally for all of them."]
    return "\n".join(md) + "\n"


# ── GitHub ───────────────────────────────────────────────────────────────────────────────────

class GitHub:
    """The four calls this needs; `request` is the only network entry."""

    def __init__(self, token, api_url):
        self.token, self.api_url = token, api_url.rstrip("/")

    def request(self, method, path, body=None):
        req = urllib.request.Request(
            self.api_url + path, method=method,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json",
                     "User-Agent": "kaeris-action"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"message": raw.decode("utf-8", "replace")[:300]}


def is_ours(c, mark, author):
    user = c.get("user") or {}
    if not (c.get("body") or "").startswith(mark):
        return False
    if user.get("login") != author:
        return False
    # github-actions[bot] and App tokens post as a Bot; a PAT posts as a User of that login.
    return user.get("type") == "Bot" if author.endswith("[bot]") else True


def own_comments(api, repo, pr, mark, author):
    """(status, [our comments with this marker], oldest first). Every page: on a busy PR ours is
    not on page one."""
    found = []
    for page in range(1, 11):
        status, batch = api.request("GET", f"/repos/{repo}/issues/{pr}/comments?per_page=100&page={page}")
        if status != 200 or not isinstance(batch, list):
            return status, []
        found += [c for c in batch if is_ours(c, mark, author)]
        if len(batch) < 100:
            break
    return 200, sorted(found, key=lambda c: c.get("id", 0))


def _cmd_escape(s):
    return str(s).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def warn(text):
    print(f"::warning title={TITLE}::{_cmd_escape(text)}")


def _denied(status, data, what, fork):
    msg = (data or {}).get("message", "") if isinstance(data, dict) else ""
    head = f"Could not {what} the PR comment (HTTP {status}{': ' + msg if msg else ''})."
    if fork:
        return (head + " This pull request comes from a fork: GitHub gives it a read-only token, so "
                "there is no PR comment here. The findings are in the job summary, and the check "
                "result decides the job.")
    return (head + " Give the job `permissions: pull-requests: write`, or set `comment: false`. "
            "The findings are in the job summary, and the check result decides the job.")


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def write_output(path, name, value):
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def post_comment(args, rows, env):
    if not _truthy(env.get("KA_COMMENT", "true")):
        return
    if env.get("GITHUB_EVENT_NAME") not in PR_EVENTS:
        return
    event = load_json(env.get("GITHUB_EVENT_PATH")) or {}
    pr = event.get("pull_request") or {}
    number = pr.get("number")
    if not number:
        return
    if rows is None:
        print(f"{TITLE}: `kaeris check` gave no result; the comment is left as it is.")
        return
    failing = args.check_exit not in ("", "0")
    token = env.get("KA_GITHUB_TOKEN", "").strip()
    repo = env.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        if failing:
            warn("No token for the PR comment. Pass `github-token`, or set `comment: false`.")
        return
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    base_repo = ((pr.get("base") or {}).get("repo") or {}).get("full_name") or repo
    fork = bool(head_repo) and head_repo != base_repo
    api = GitHub(token, env.get("GITHUB_API_URL") or "https://api.github.com")
    author = env.get("KA_COMMENT_AUTHOR", "").strip() or DEFAULT_AUTHOR
    cid = comment_id(args.source, args.out, args.langs_list, env.get("KA_COMMENT_KEY", ""))
    mark = marker(cid)

    status, mine = own_comments(api, repo, number, mark, author)
    if status != 200:
        # Nothing failing: there is nothing to tell the reviewer, so no warning on every clean
        # PR of a workflow that never granted pull-requests: write. Just a log line.
        if failing:
            warn(_denied(status, None, "read", fork))
        else:
            print(f"{TITLE}: cannot read PR comments (HTTP {status}); nothing to fix, so nothing to post.")
        return
    if not mine and not failing:
        print(f"{TITLE}: nothing to fix, no comment posted.")
        return

    server = env.get("GITHUB_SERVER_URL") or "https://github.com"
    run_id = env.get("GITHUB_RUN_ID")
    attempt = env.get("GITHUB_RUN_ATTEMPT") or "1"
    run_url = None
    if run_id:
        run_url = f"{server}/{repo}/actions/runs/{run_id}" + (f"/attempts/{attempt}" if attempt != "1" else "")
    cta = _truthy(env.get("KA_CTA", "true")) and \
        env.get("KAERIS_NO_CTA", "").strip().lower() in ("", "0", "false", "no")
    body = build_comment(rows, args.source, args.langs_list, failing,
                         head_sha=(pr.get("head") or {}).get("sha") or env.get("GITHUB_SHA"),
                         repo=repo, server=server, run_url=run_url, out_dir=args.out or None,
                         strict=args.strict == "true", cid=cid, cta=cta)

    data, done = None, None
    if mine:
        status, data = api.request("PATCH", f"/repos/{repo}/issues/comments/{mine[0]['id']}", {"body": body})
        done = "updated"
        if status == 404:                                 # deleted under us: post it again
            mine = []
    if not mine:
        status, data = api.request("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})
        done = "posted"
    if status not in (200, 201):
        warn(_denied(status, data, "post" if done == "posted" else "edit", fork))
        return
    url = (data or {}).get("html_url", "") if isinstance(data, dict) else ""
    # Two jobs of one matrix can both find nothing and both post. Re-read: the oldest stays,
    # every newer one of ours with this marker goes.
    s2, again = own_comments(api, repo, number, mark, author)
    if s2 == 200 and len(again) > 1:
        for c in again[1:]:
            api.request("DELETE", f"/repos/{repo}/issues/comments/{c['id']}")
        url = again[0].get("html_url", url)
        done += ", kept the older duplicate"
    print(f"{TITLE}: {done} {url}".rstrip())
    write_output(args.github_output, "comment-url", url)


def main(argv=None, env=None):
    env = os.environ if env is None else env
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-json", required=True)
    ap.add_argument("--check-exit", default="")
    ap.add_argument("--sarif", default="")
    ap.add_argument("--source", required=True)
    ap.add_argument("--langs", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--strict", default="false")
    ap.add_argument("--summary-file", default="")
    ap.add_argument("--github-output", default="")
    args = ap.parse_args(argv)
    args.langs_list = [l.strip() for l in args.langs.split(",") if l.strip()]
    args.check_exit = (args.check_exit or "").strip()
    try:
        check = load_json(args.check_json)
        # Same test as action.yml's bash: only the exact string "true" passes --strict.
        rows = collect(check, load_json(args.sarif), args.strict == "true") if check is not None else None
        if rows is not None and args.check_exit == "0":
            for r in rows:                                # the CLI said nothing blocks: nothing does
                r.blocking = False
        if rows and args.summary_file:
            pr = (load_json(env.get("GITHUB_EVENT_PATH")) or {}).get("pull_request") or {}
            sha = (pr.get("head") or {}).get("sha") or env.get("GITHUB_SHA")
            server = env.get("GITHUB_SERVER_URL") or "https://github.com"
            repo = env.get("GITHUB_REPOSITORY")
            with open(args.summary_file, "a", encoding="utf-8") as f:
                f.write(build_summary(rows, lambda p, l: _blob(server, repo, sha, p, l)))
        post_comment(args, rows, env)
    except (urllib.error.URLError, OSError) as e:         # DNS, refused, timeout
        warn(f"Could not reach the GitHub API for the PR comment ({e}). The findings are in the "
             "job summary, and the check result decides the job.")
    except Exception as e:                                # never the reason a job is red
        warn(f"The PR comment step failed ({type(e).__name__}: {e}). The check result still decides the job.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
