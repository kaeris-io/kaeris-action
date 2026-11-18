# P2.2 result: the PR comment in check mode

Branch `p22-pr-comment` from `origin/main` = tag `v2.1.0` (`e574ae8`, same commit). Not tagged,
not released; the live e2e is prepared in `docs/P22-E2E.md`, not run.

## What changed

- `pr_comment.py` (new, stdlib): findings from `kaeris check --json`, the file of each language
  from the SARIF the CLI writes, one PR comment upserted by a hidden marker, the full list in
  the job summary. Always exits 0: the job's verdict stays `kaeris check`'s exit code.
- `action.yml`: inputs `comment` (default `true`) and `github-token` (default
  `${{ github.token }}`), output `comment-url`; check mode always writes SARIF (to
  `$RUNNER_TEMP` unless `sarif-file` is set) because only SARIF names each language's file.
- `README.md`: "Findings in a PR comment", `permissions: pull-requests: write` in the example,
  inputs/outputs, the upgrade note.
- `test/test_pr_comment.py` + `test/fixtures/check/*` (recorded from CLI 0.2.39).

## Us against the leaders (details: `docs/P22-LEADERS.md`)

| | Lingo.dev | Crowdin | Tolgee | KAERIS Action |
|---|---|---|---|---|
| Quality findings as a PR comment | No (App comment lists translated files and failures) | No | No (annotations about extraction) | Yes: file / key / language / problem, link to the line |
| Edited in place, not one per push | App: yes | n/a | n/a | Yes, found by marker on any page of comments |
| Red job on a broken `{name}` | Not documented | No | No | Yes |
| Needs account / key / App | Yes | Yes | Yes | No: `GITHUB_TOKEN` + `pull-requests: write` |
| Private repo without Code Security | App only | n/a | Annotations | Yes (that is the point; SARIF is invisible there) |
| No-permission / fork PR | n/a | n/a | n/a | One warning, verdict unchanged; a clean run stays quiet |

## Tests

How: the action's own `run:` block from action.yml under `bash --noprofile --norc -eo pipefail`
(Git Bash on Windows), inputs and env read from action.yml; `kaeris` is a stub that replays
the recorded check JSON + SARIF; the GitHub API is a local `http.server` on 127.0.0.1.

Command:
```
PYTHONUTF8=1 i18n_tool\.venv\Scripts\python.exe -m pytest test -v -p no:cacheprovider
```

### УПАЛО (before the code, commit 2dbe137)

Exit code 1.
```
FAILED test/test_pr_comment.py::PRComment::test_a_broken_placeholder_posts_one_comment_and_fails
FAILED test/test_pr_comment.py::PRComment::test_a_human_quoting_the_marker_is_not_hijacked
FAILED test/test_pr_comment.py::PRComment::test_b_second_run_patches_the_same_comment
FAILED test/test_pr_comment.py::PRComment::test_c_clean_run_updates_existing_comment_to_clean
FAILED test/test_pr_comment.py::PRComment::test_d_forbidden_broken_same_repo
FAILED test/test_pr_comment.py::PRComment::test_d_forbidden_broken_still_fails_on_findings_with_a_warning
FAILED test/test_pr_comment.py::PRComment::test_d_no_token_warns_and_calls_nothing
FAILED test/test_pr_comment.py::PRComment::test_job_summary_lists_every_finding
FAILED test/test_pr_comment.py::PRComment::test_our_comment_found_past_the_first_page
FAILED test/test_pr_comment.py::PRComment::test_pull_request_target_comments_too
ERROR test/test_pr_comment.py::Format::test_markdown_in_values_is_escaped - M...
ERROR test/test_pr_comment.py::Format::test_placeholder_problem_said_once - M...
ERROR test/test_pr_comment.py::Format::test_size_is_bounded_and_says_how_many_are_left
ERROR test/test_pr_comment.py::Format::test_warnings_alone_are_not_blocking
10 failed, 11 passed, 4 errors in 8.37s
```
The 11 that passed before the code are the ones asserting that nothing happens (push event,
`comment: false`, clean PR without a comment, a clean run under 403) plus the 6 old summary
tests: with no comment code, nothing happening is already true.

### ПРОЙДЕНО (after the code)

Exit code 0.
```
test/test_pr_comment.py::PRComment::test_a_broken_placeholder_posts_one_comment_and_fails PASSED [  4%]
test/test_pr_comment.py::PRComment::test_a_human_quoting_the_marker_is_not_hijacked PASSED [  8%]
test/test_pr_comment.py::PRComment::test_b_second_run_patches_the_same_comment PASSED [ 12%]
test/test_pr_comment.py::PRComment::test_c_clean_run_updates_existing_comment_to_clean PASSED [ 16%]
test/test_pr_comment.py::PRComment::test_c_clean_run_without_comment_creates_nothing PASSED [ 20%]
test/test_pr_comment.py::PRComment::test_check_is_still_called_with_the_v2_flags PASSED [ 24%]
test/test_pr_comment.py::PRComment::test_d_forbidden_broken_same_repo PASSED [ 28%]
test/test_pr_comment.py::PRComment::test_d_forbidden_broken_still_fails_on_findings_with_a_warning PASSED [ 32%]
test/test_pr_comment.py::PRComment::test_d_forbidden_clean_stays_green_and_quiet PASSED [ 36%]
test/test_pr_comment.py::PRComment::test_d_no_token_warns_and_calls_nothing PASSED [ 40%]
test/test_pr_comment.py::PRComment::test_e_comment_false_touches_nothing PASSED [ 44%]
test/test_pr_comment.py::PRComment::test_e_push_event_touches_no_comment PASSED [ 48%]
test/test_pr_comment.py::PRComment::test_job_summary_lists_every_finding PASSED [ 52%]
test/test_pr_comment.py::PRComment::test_our_comment_found_past_the_first_page PASSED [ 56%]
test/test_pr_comment.py::PRComment::test_pull_request_target_comments_too PASSED [ 60%]
test/test_pr_comment.py::Format::test_markdown_in_values_is_escaped PASSED [ 64%]
test/test_pr_comment.py::Format::test_placeholder_problem_said_once PASSED [ 68%]
test/test_pr_comment.py::Format::test_size_is_bounded_and_says_how_many_are_left PASSED [ 72%]
test/test_pr_comment.py::Format::test_warnings_alone_are_not_blocking PASSED [ 76%]
test/test_summarize.py::Footer::test_a_failed_check_too PASSED           [ 80%]
test/test_summarize.py::Footer::test_check_ends_with_the_link PASSED     [ 84%]
test/test_summarize.py::Footer::test_not_when_check_gave_no_result PASSED [ 88%]
test/test_summarize.py::Footer::test_nothing_on_stdout PASSED            [ 92%]
test/test_summarize.py::Footer::test_switched_off PASSED                 [ 96%]
test/test_summarize.py::Footer::test_translate_does_not_say_free_or_no_key PASSED [100%]

============================= 25 passed in 13.94s =============================
```
The CI command of this repo (`python3 -m unittest discover -s test -p "test_*.py"`) runs the
same file: `Ran 25 tests ... OK`.

Between red and green one test was wrong, not the code: (c) ran the clean push with a
different `source` than the broken one, so a different marker; a real PR keeps its source.

## Decisions

- **`comment` defaults to `true`.** The feature is worth something only if a user of `@v2`
  sees it without reading a changelog. It cannot break a v2 workflow: the exit code is the
  same in every case, no comment is created on a clean PR, and a token without
  `pull-requests: write` gives one warning only on a run that has problems (a clean run logs a
  plain line). `comment: false` turns it off completely.
- **Created only when something blocks the merge.** `kaeris check` checks the whole locale,
  not the diff: a non-strict repo with old warnings would otherwise get a comment on every PR.
  An existing comment is always edited, so "fixed" and warnings stay visible after a red run.
- **Link `?utm_source=action-comment`, not `?ref=action-comment`.** The site reads the source
  of a visit only from `utm_source` (summarize.py, mcp server, launch notes all say so); with
  `?ref=` these visits would be counted as plain github.com referrals.
- **No emoji, no bold lead** in the comment: header, counts, table, one sentence on what
  fails, links.
- Values are escaped as text (`|`, `` ` ``, `<b>`, `**`, `&`), `@name` and `#12` are defused
  with a zero-width space so a translation cannot ping a user or link an issue.

## Live e2e

Not run. Steps: `docs/P22-E2E.md`, workflow: `docs/p22-e2e/i18n-check.yml`.
