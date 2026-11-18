#!/usr/bin/env python3
"""Check mode: one comment on the pull request with what `kaeris check` found, edited in place on
every push, and the full list of findings in the job summary.

Why a comment: SARIF annotations need GitHub Code Security in a private repo, and a job summary
is a page nobody opens. A comment is where the reviewer already is, and it needs nothing but the
workflow's own GITHUB_TOKEN with `pull-requests: write`.

Rules this file keeps:

  * It never decides the job. The step's exit code is `kaeris check`'s, set in action.yml;
    this script always exits 0. No permission (a fork PR, `contents: read` only) or a GitHub
    outage is one `::warning::` line, never a red job and never a green one that should be red.
  * One comment per source file per PR, found by a hidden marker at the start of its body and
    edited (PATCH), never a second one (POST) per push. A human quoting the marker is not ours.
  * A comment is created only when something blocks the merge. A clean or warnings-only run
    edits an existing comment (so "fixed" is visible) and otherwise creates nothing.
  * Only on pull_request / pull_request_target. Other events do not touch the API at all.
  * Every value from the locale files is escaped before it reaches markdown: a translation can
    hold `|`, `<b>`, `@someone` or `**`, and must show as text, not break the table, render
    HTML or ping a user. The body stays under GitHub's 65 536 character limit.

The upsert follows backend/gh/api.py:upsert_pr_comment in i18n_tool (marker, every page of
comments, PATCH else POST); the table follows backend/gh/report.py:build_comment and `_esc`.
Both are copied ideas, not imports: this runs on a bare runner with the stdlib only.

Usage (from action.yml):
  pr_comment.py --check-json AFTER.json [--sarif CHECK.sarif] --source locales/en.json \
      --langs de,fr [--out DIR] [--strict true] [--summary-file "$GITHUB_STEP_SUMMARY"] \
      [--github-output "$GITHUB_OUTPUT"]
Environment: KA_COMMENT (true/false), KA_GITHUB_TOKEN, and the runner's GITHUB_* variables.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

TITLE = "KAERIS PR comment"
MARKER_PREFIX = "<!-- kaeris-action check: "
SITE_URL = "https://kaeris.dev/?utm_source=action-comment"   # the site reads utm_source, not ref
GITHUB_LIMIT = 65536
BODY_BUDGET = 60000          # room under GitHub's limit for the header and the footer
COMMENT_ROWS = 50            # a comment is for reading; the job summary has the rest
SUMMARY_ROWS = 1000
PR_EVENTS = ("pull_request", "pull_request_target")


# ── findings ─────────────────────────────────────────────────────────────────────────────────

class Row:
    __slots__ = ("lang", "key", "file", "line", "blocking", "problems")

    def __init__(self, lang, key):
        self.lang, self.key, self.file, self.line = lang, key, None, None
        self.blocking, self.problems = False, []


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
    --json result names no files; the SARIF does, resolved by the CLI's own layout rules."""
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
    """1-based line of `key` in a JSON/ARB file, or None. Textual, like the CLI's sarif.py, but a
    nested key is walked segment by segment ("checkout.title": "checkout", then the first
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
    placeholder twice (placeholder_issues and faults): it is said once here."""
    rows = {}

    def add(lang, key, blocking, problem):
        r = rows.setdefault((lang or "", key or ""), Row(lang or "", key or ""))
        r.blocking = r.blocking or bool(blocking)
        if problem not in r.problems:
            r.problems.append(problem)
        return r

    check = check or {}
    for lang, keys in (check.get("missing") or {}).items():
        for key in keys:
            add(lang, key, True, "missing in this language")
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
        if r.file and key and "missing in this language" not in r.problems:
            r.line = line_of_key(r.file, key)
    for lang in check.get("missing_files") or []:
        add(lang, "", True, "no locale file for this language")
    ordered = list(rows.values())
    ordered.sort(key=lambda r: not r.blocking)            # stable: CLI order within each group
    return ordered


# ── markdown ─────────────────────────────────────────────────────────────────────────────────

_MD = re.compile(r"([\\`*_\[\]<>|~])")


def esc(text, limit=200):
    """Plain text in a table cell: shown as typed, never rendered."""
    s = " ".join(str(text).split())
    if len(s) > limit:
        s = s[:limit - 1] + "…"
    s = s.replace("&", "&amp;")
    s = _MD.sub(r"\\\1", s)
    s = s.replace("@", "@​")                         # no mention ping
    return re.sub(r"#(?=\d)", "#​", s)               # no issue autolink


def code(text, limit=120):
    """A value in a code span: backticks cannot close it, a pipe cannot split the table."""
    s = " ".join(str(text).split()).replace("`", "'")
    if len(s) > limit:
        s = s[:limit - 1] + "…"
    return "`" + s.replace("|", "\\|") + "`"


def marker(source):
    return MARKER_PREFIX + str(source).replace("--", "-").replace(">", "")[:200] + " -->"


def _blob(server, repo, sha, path, line):
    if not (server and repo and sha and path):
        return None
    url = f"{server.rstrip('/')}/{repo}/blob/{sha}/{urllib.parse.quote(path, safe='/')}"
    return url + (f"#L{line}" if line else "")


def table(rows, limit, budget, link=None):
    """(lines, shown) — at most `limit` rows and `budget` characters."""
    out = ["| File | Key | Language | Problem |", "|---|---|---|---|"]
    size, shown = sum(len(x) + 1 for x in out), 0
    for r in rows[:limit]:
        if r.file:
            label = code(r.file + (f":{r.line}" if r.line else ""))
            url = link(r.file, r.line) if link else None
            where = f"[{label}]({url})" if url else label
        else:
            where = "–"
        problem = "; ".join(p for p in r.problems if p)
        if not r.blocking:
            problem = "warning: " + problem
        line = f"| {where} | {code(r.key) if r.key else '–'} | {esc(r.lang, 20)} | {esc(problem)} |"
        if size + len(line) + 1 > budget:
            break
        out.append(line)
        size += len(line) + 1
        shown += 1
    return out, shown


def build_comment(rows, source, langs, head_sha=None, repo=None, server=None, run_url=None,
                  out_dir=None, cta=True):
    n_fix = sum(r.blocking for r in rows)
    n_review = len(rows) - n_fix
    where = f"`{code(source)[1:-1]}` against {esc(', '.join(langs))}"
    if head_sha:
        where += f" at `{head_sha[:7]}`"
    if n_fix:
        head = f"### KAERIS i18n check: {n_fix} problem{'s' if n_fix != 1 else ''} to fix"
    elif n_review:
        head = f"### KAERIS i18n check: no problems to fix, {n_review} to review"
    else:
        head = "### KAERIS i18n check: no problems"
    md = [marker(source), head, ""]
    summary_link = f"[job summary]({run_url})" if run_url else "job summary"
    if not rows:
        md.append(f"Checked {where}: nothing to fix any more.")
    else:
        counts = [f"{n} {what}" for n, what in ((n_fix, "to fix"), (n_review, "to review")) if n]
        md.append(f"Checked {where}: {', '.join(counts)}.")
        md.append("")
        lines, shown = table(rows, COMMENT_ROWS, BODY_BUDGET,
                             lambda p, l: _blob(server, repo, head_sha, p, l))
        md.extend(lines)
        if shown < len(rows):
            md.append("")
            md.append(f"{len(rows) - shown} more not listed here; the {summary_link} has all of them.")
        md.append("")
        # A row is a warning only without `strict` (collect() makes every row blocking with it).
        note = "The check fails until the problems above are fixed." if n_fix else "Nothing blocks the merge."
        if n_review:
            note += " Warnings fail the check only with `strict: true`."
        md.append(note)
        cmd = f"kaeris check --source {source} --langs {','.join(langs)}"
        if out_dir:
            cmd += f" --out {out_dir}"
        md.append(f"Full report: {summary_link}. Locally: `pip install kaeris`, then {code(cmd, 300)}.")
    md.append("")
    if cta:
        md.append(f"<sub>[KAERIS]({SITE_URL}) i18n check, free, no key. "
                  "Edited on every push; `comment: false` turns it off.</sub>")
    else:
        md.append("<sub>Edited on every push; `comment: false` turns it off.</sub>")
    body = "\n".join(md) + "\n"
    return body[:GITHUB_LIMIT - 1]


def build_summary(rows, link=None):
    if not rows:
        return ""
    lines, shown = table(rows, SUMMARY_ROWS, 900_000, link)
    md = ["", f"### Findings ({len(rows)})", ""] + lines
    if shown < len(rows):
        md += ["", f"{len(rows) - shown} more not listed; run `kaeris check` locally for all of them."]
    return "\n".join(md) + "\n"


# ── GitHub ───────────────────────────────────────────────────────────────────────────────────

class GitHub:
    """The three calls this needs; `request` is the only network entry. After trusted_pr.py."""

    def __init__(self, token, api_url):
        self.token, self.api_url = token, api_url.rstrip("/")

    def request(self, method, path, body=None):
        req = urllib.request.Request(
            self.api_url + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
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


def find_comment(api, repo, pr, mark):
    """(status, our comment or None). Every page: on a busy PR ours is not on page one."""
    for page in range(1, 11):
        status, batch = api.request("GET", f"/repos/{repo}/issues/{pr}/comments?per_page=100&page={page}")
        if status != 200 or not isinstance(batch, list):
            return status, None
        for c in batch:
            if (c.get("body") or "").startswith(mark):
                return 200, c
        if len(batch) < 100:
            break
    return 200, None


def _cmd_escape(s):
    return str(s).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def warn(text):
    print(f"::warning title={TITLE}::{_cmd_escape(text)}")


def _denied(status, data, what, fork):
    msg = (data or {}).get("message", "") if isinstance(data, dict) else ""
    head = f"Could not {what} the PR comment (HTTP {status}{': ' + msg if msg else ''})."
    if fork:
        return (head + " This pull request comes from a fork, and GitHub gives fork workflows a "
                "read-only token, so the comment cannot be posted here; `pull-requests: write` in "
                "the workflow does not change that for forks. The check result still decides the job.")
    return (head + " Give the job `permissions: pull-requests: write`, or set `comment: false` to stop "
            "trying. The check result still decides the job.")


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
    token = env.get("KA_GITHUB_TOKEN", "").strip()
    repo = env.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        warn("No token for the PR comment. Pass `github-token`, or set `comment: false`.")
        return
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    base_repo = ((pr.get("base") or {}).get("repo") or {}).get("full_name") or repo
    fork = bool(head_repo) and head_repo != base_repo
    api = GitHub(token, env.get("GITHUB_API_URL") or "https://api.github.com")
    mark = marker(args.source)

    status, mine = find_comment(api, repo, number, mark)
    if status != 200:
        # Nothing blocking: there is nothing to tell the reviewer, so no warning on every clean
        # PR of a workflow that never granted pull-requests: write. Just a log line.
        if any(r.blocking for r in rows):
            warn(_denied(status, None, "read", fork))
        else:
            print(f"{TITLE}: cannot read PR comments (HTTP {status}); nothing to fix, so nothing to post.")
        return
    if not mine and not any(r.blocking for r in rows):
        print(f"{TITLE}: nothing to fix, no comment posted.")
        return

    server = env.get("GITHUB_SERVER_URL") or "https://github.com"
    run_id = env.get("GITHUB_RUN_ID")
    attempt = env.get("GITHUB_RUN_ATTEMPT") or "1"
    run_url = None
    if run_id:
        run_url = f"{server}/{repo}/actions/runs/{run_id}" + (f"/attempts/{attempt}" if attempt != "1" else "")
    cta = env.get("KAERIS_NO_CTA", "").strip().lower() in ("", "0", "false", "no")
    body = build_comment(rows, args.source, args.langs_list,
                         head_sha=(pr.get("head") or {}).get("sha") or env.get("GITHUB_SHA"),
                         repo=repo, server=server, run_url=run_url, out_dir=args.out or None, cta=cta)
    if mine:
        status, data = api.request("PATCH", f"/repos/{repo}/issues/comments/{mine['id']}", {"body": body})
        done = "updated"
    else:
        status, data = api.request("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})
        done = "posted"
    if status not in (200, 201):
        warn(_denied(status, data, "post" if done == "posted" else "edit", fork))
        return
    url = (data or {}).get("html_url", "") if isinstance(data, dict) else ""
    print(f"{TITLE}: {done} {url}".rstrip())
    write_output(args.github_output, "comment-url", url)


def main(argv=None, env=None):
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-json", required=True)
    ap.add_argument("--sarif", default="")
    ap.add_argument("--source", required=True)
    ap.add_argument("--langs", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--strict", default="false")
    ap.add_argument("--summary-file", default="")
    ap.add_argument("--github-output", default="")
    args = ap.parse_args(argv)
    args.langs_list = [l.strip() for l in args.langs.split(",") if l.strip()]
    try:
        check = load_json(args.check_json)
        rows = collect(check, load_json(args.sarif), _truthy(args.strict)) if check is not None else None
        if rows and args.summary_file:
            pr = (load_json(env.get("GITHUB_EVENT_PATH")) or {}).get("pull_request") or {}
            sha = (pr.get("head") or {}).get("sha") or env.get("GITHUB_SHA")
            server = env.get("GITHUB_SERVER_URL") or "https://github.com"
            repo = env.get("GITHUB_REPOSITORY")
            with open(args.summary_file, "a", encoding="utf-8") as f:
                f.write(build_summary(rows, lambda p, l: _blob(server, repo, sha, p, l)))
        post_comment(args, rows, env)
    except Exception as e:                                # never the reason a job is red
        warn(f"The PR comment step failed ({type(e).__name__}: {e}). The check result still decides the job.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
