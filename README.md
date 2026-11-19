<img src="https://kaeris.dev/icon-512.png" alt="KAERIS" width="72" align="left" style="margin-right:16px" />

# KAERIS i18n Check and Translate

Fail CI when a locale file is missing keys or a translation broke a placeholder. Or translate
the keys you added into 46 languages and open a pull request with the result.

[![test](https://github.com/kaeris-io/kaeris-action/actions/workflows/test.yml/badge.svg)](https://github.com/kaeris-io/kaeris-action/actions/workflows/test.yml)
[![release](https://img.shields.io/github/v/release/kaeris-io/kaeris-action)](https://github.com/kaeris-io/kaeris-action/releases)
[![license](https://img.shields.io/github/license/kaeris-io/kaeris-action)](LICENSE)

```yaml
- uses: kaeris-io/kaeris-action@v2
  with:
    mode: check
    source: locales/en.json
    languages: de,fr,ja
```

- **`check`**: offline, no API call, no key. Fails the job on a missing key, a lost or renamed
  placeholder, number drift or a missing plural form. Optional SARIF report puts each finding on
  the line of the locale file in the pull request.
- **`translate`**: translates only the keys that are new or changed since the last run, writes
  the locale files, and returns `changed` and a Markdown `summary` for the pull request body.
- **Names, terms and models from your repository**: `kaeris.json` and `kaeris-glossary.json`
  at the root keep brand names as written, make each language use the agreed word for a term,
  and on Lifetime pick the model per language ([below](#brand-names-glossary-and-models)).
- **Translation memory**: `kaeris-tm.json` remembers translations by text, so a renamed key is
  reused, not paid for again ([below](#translation-memory)).
- **13 formats** in both modes: JSON, YAML, `.strings`, `.xcstrings`, `.po`, ARB, Android XML,
  CSV, XLIFF, `.properties`, `.resx`, Fluent and Markdown.

`check` is free. `translate` runs without a key on the free tier (8 languages per run); plans:
Pro $19/month with 12M characters, Team/API $79/month with 60M, Lifetime $79 once with your own
OpenRouter key. See [kaeris.dev/pricing](https://kaeris.dev/pricing.html).

## Usage

### Gate every PR (i18n firewall — no API key)

```yaml
# .github/workflows/i18n-check.yml
name: i18n check
on: [pull_request]
permissions:
  contents: read
  pull-requests: write    # for the PR comment with the findings; leave it out and you get none
concurrency:              # a new push cancels the run of the previous one
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: kaeris-io/kaeris-action@v2
        with:
          mode: check
          source: locales/en.json
          languages: es,fr,de,ja
```

A broken or incomplete translation now fails the check and can't merge, and the PR gets one
comment saying which file, key and language broke ([below](#findings-in-a-pr-comment)). Add the
job as a required status check in branch protection to make it a hard gate.

### Auto-translate new strings into a PR

```yaml
# .github/workflows/i18n-translate.yml
name: i18n translate
on:
  push:
    branches: [main]
    paths: ['locales/en.json']
jobs:
  translate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - id: kaeris
        uses: kaeris-io/kaeris-action@v2
        with:
          mode: translate
          source: locales/en.json
          languages: es,fr,de,ja
          api-key: ${{ secrets.KAERIS_API_KEY }}   # omit for the free demo
      - if: steps.kaeris.outputs.changed == 'true'
        uses: peter-evans/create-pull-request@v6
        with:
          title: "i18n: update translations"
          body: ${{ steps.kaeris.outputs.summary }}
          branch: i18n/update
```

### Brand names, glossary and models

The action runs the CLI from the root of your repository, so files there apply to every run
without an input. Both modes read `keep` and the glossary; `models` matters only to `translate`.

`kaeris.json`:

```json
{
  "keep": ["KAERIS", "GitHub"],
  "glossary_file": "kaeris-glossary.json"
}
```

`kaeris-glossary.json`:

```json
{"terms": [
  {"term": "workspace", "translations": {"de": "Arbeitsbereich", "fr": "espace de travail"}}
]}
```

- **`keep`**: words left exactly as written in every language.
- **The glossary**: the word each language must use for a term. `translate` gives the terms to
  the model; `check` fails a translation that says the term another way (in languages whose
  words change shape, such as German or Russian, a miss is a warning, because an inflected form
  can still be right).
- **`models`** (Lifetime, with `openrouter-key`): one OpenRouter model per language, e.g.
  `"models": {"de": "anthropic/claude-sonnet-4.5", "ja": "google/gemini-2.5-pro"}`. A language
  that is not listed uses `model` from the same file, and the default model when that is not set
  either. Other plans refuse it with HTTP 403 before anything is started or charged.

The same settings work locally with `pip install kaeris`; the CLI docs have a runnable example:
[kaeris.dev/docs/cli](https://kaeris.dev/docs/cli#cli-terms).

### Translation memory

`translate` saves every translation to `kaeris-tm.json` next to the source and, on the next run,
looks each line up by its text, not its key. A renamed key, a string moved to another file or a
fresh clone gets the saved translation and is not charged again. The file sits next to
`kaeris.lock`, and the pull request from `create-pull-request` carries both with the locale
files: keep them committed. A line saved under another tone, glossary or model is translated
again.

## Inputs

| Input | Description | Default |
|---|---|---|
| `mode` | `translate` (write locale files) or `check` (i18n firewall, no API call); case does not matter, anything else is an error | `translate` |
| `source` | Source strings file, e.g. `locales/en.json` | — (required) |
| `languages` | Comma-separated target languages, e.g. `es,fr,de,ja` | — (required) |
| `out` | Directory for translated / target locale files | source's dir |
| `api-key` | KAERIS API key, e.g. the free key from [kaeris.dev/docs/key](https://kaeris.dev/docs/key) (no account). Omit for the free demo. Unused in `check`. | — |
| `openrouter-key` | OpenRouter key for the Lifetime (BYOK) plan. Unused in `check`. | — |
| `only-new` | `translate`: only translate keys missing/changed since the last run (uses `kaeris.lock`) | `true` |
| `tone` | `translate`: `neutral`, `formal`, or `casual` | — (`neutral`) |
| `icu` | `translate`: hint that strings use ICU MessageFormat (plurals/select) so it's preserved | `false` |
| `strict` | `check`: also fail on warnings and on extra/stale keys not in the source | `false` |
| `sarif-file` | `check`: also write a SARIF 2.1.0 report to this path (see below) | — |
| `comment` | `check` on `pull_request`: keep one PR comment with the findings, edited on every push (see below). `false` turns it off | `true` |
| `comment-key` | `check`: which comment this check keeps; set it to tell apart two checks over the same files | from `source`, `out`, `languages` |
| `comment-author` | `check`: the login `github-token` posts as; only its comments are edited. Set it for an App token (a PAT's login is read from `GET /user`) | `github-actions[bot]` |
| `github-token` | Token for the PR comment, passed to that step only | `${{ github.token }}` |
| `cta` | Link kaeris.dev on the last line of the summary and the PR comment; `false` leaves it out | `true` |
| `api-url` | API base URL | `https://kaeris.dev` |

## Outputs

| Output | Description |
|---|---|
| `changed` | `"true"` if `translate` wrote/updated any locale content — open a PR only when this is true |
| `summary` | Markdown summary (keys translated, per-language matrix, placeholder/overflow counts) — use as a PR body. Its last line links kaeris.dev; `cta: false` (or `KAERIS_NO_CTA: "1"` in the step's `env`) leaves it out |
| `missing` | Total missing-key count across all target languages |
| `placeholder-issues` | Total placeholder-mismatch count (should be `0`) |
| `check-exit-code` | Exit code of `kaeris check` (0 clean, 1 problem, 2 bad usage) |
| `translate-exit-code` | Exit code of `kaeris translate` (0 clean, 3 some languages fell back to the source); non-zero also fails the step |
| `comment-url` | `check`: URL of the PR comment this run posted or edited; empty when none |

## Findings in a PR comment

In `check` mode on a `pull_request` event the action keeps **one** comment on the pull request:

```markdown
### KAERIS i18n check: 2 to fix

Checked de against `locales/en.json` at `3f2a9c1`.

| File | Key | Language | Problem |
|---|---|---|---|
| `locales/de.json` | `checkout.title` | de | missing in this language |
| `locales/de.json:2` | `greeting` | de | lost placeholder {name}; invented placeholder {nam} (not in source) |

The check fails until the problems above are fixed.
Full report: job summary. Locally: `pip install kaeris`, then `kaeris check --source "locales/en.json" --langs de`.

<sub>Checked by KAERIS (free, no API key). Updated on every push; `comment: false` turns it off.</sub>
```

(The file cells are links in the real comment; the hidden marker line above the header is left out here.)

Each file cell links to the line at the PR's head commit. The header counts what blocks the
merge and what is only a warning apart, one row per key and kind: for example
`2 to fix, 1 warning`. How it behaves:

- **Posted only when the check fails.** A clean PR gets no comment at all.
- **Edited, never repeated.** The next push edits the same comment: "no problems" once
  everything is fixed, "nothing to fix, 2 warnings" when only warnings are left, so the
  reviewer sees what is gone. If `kaeris check` does not finish (exit 2: bad usage, an
  unreadable file; or a crash with no result) the comment says "did not finish" instead of
  keeping the old list.
- **Needs `pull-requests: write`.** Add it under `permissions:` (workflow or job). Without it
  there is no comment, a failing run gets one warning, and the result does not change: the
  findings alone make the job red or green. The token is passed to the comment step only,
  never to the CLI.
- **Pull requests from forks get no comment.** GitHub gives them a read-only token; the
  findings are in the job summary. Do not switch to `pull_request_target` for this: it checks
  out the base branch, so the check would not see the PR's files, and running PR code there
  with a write token is how repositories get compromised. The comment runs on `pull_request` only.
- **Works in private repos without GitHub Advanced Security**, where SARIF annotations do not
  show. Nothing else to install: no App, no account, no key.
- **One comment per check.** A check is its `source`, `out` and `languages`: a language matrix
  or two steps keep separate comments, and a clean job never marks a failing job's comment
  "no problems". `comment-key` tells apart two checks over the same files. Change `source`,
  `out` or `languages` and the next run starts a new comment; the old one stays as it was.
- **Only its own comments.** It edits a comment only if it starts with the marker *and* was
  posted by `comment-author` (`github-actions[bot]` for the default token, the PAT's own login
  read from `GET /user`). An App token cannot say who it is: set `comment-author: my-app[bot]`;
  until you do, the job warns with the login to use and keeps the PR to one comment.
- **Matrix runs.** If several jobs (an OS matrix) post at the same moment, each re-reads the
  thread and the newer duplicates are deleted: the oldest comment stays.
- The job summary lists every finding (the comment shows the first 50).
- `comment: false` turns it off; `cta: false` drops the kaeris.dev link from its last line.

## Findings inside the pull request (SARIF)

`check` can emit a SARIF 2.1.0 report. Upload it and every finding lands as an annotation on
the exact line of the locale file — in the diff, next to the human reviewer's comments —
instead of hiding in the job log.

```yaml
- uses: kaeris-io/kaeris-action@v2
  with:
    mode: check
    source: locales/en.json
    languages: de,fr,es
    sarif-file: kaeris.sarif

- uses: github/codeql-action/upload-sarif@v3
  if: always()            # upload the report even when the gate failed — that is when it matters
  with:
    sarif_file: kaeris.sarif
```

The report is written even on a clean run (an empty result set), because `upload-sarif`
fails the workflow if the file is missing, and "nothing was wrong" must not look like "the
step is broken".

Note on visibility: code-scanning uploads are free on public repositories; on private ones
they need GitHub Advanced Security. Without it the gate still works exactly as before — the
job fails, the PR comment and the job summary list every finding — you just do not get the
inline annotations.

## What it protects against

The deterministic checks (no API needed) catch the bugs that actually break apps: lost or renamed placeholders, number drift (`Delete 5 files` → `Delete 50 files`), the ICU/CLDR plural categories a target language requires, and UI-overflow risk. Every number in the PR summary comes from a real `kaeris check --json` run — nothing is fabricated.

## Upgrading from v1

Change `@v1` to `@v2`. Inputs and outputs are the same. v2 installs `kaeris>=0.2.39`
(v1 installs `>=0.2.12`), and the Marketplace name is now "KAERIS i18n Check and Translate".
The `v1` tag stays where it is.

Since v2.2 `check` on a pull request also keeps the PR comment above. Existing inputs,
outputs and exit codes are unchanged; the new inputs (`comment`, `comment-key`,
`comment-author`, `github-token`, `cta`) all have defaults. A workflow without `pull-requests: write` gets no comment and one
warning on a run that has problems to report (a clean run stays quiet); add the permission to
get the comment, or `comment: false` to silence the warning.

## Keep a CLI version

The action sets up its own Python and installs the newest `kaeris` from PyPI on every run. To
stay on one version, give pip a constraint before the action; the action's own `pip install`
obeys it:

```yaml
- name: Keep the KAERIS CLI at 0.2.43
  shell: bash
  run: |
    echo "kaeris==0.2.43" > "$RUNNER_TEMP/kaeris-pin.txt"
    echo "PIP_CONSTRAINT=$RUNNER_TEMP/kaeris-pin.txt" >> "$GITHUB_ENV"
- uses: kaeris-io/kaeris-action@v2
  with:
    mode: check
    source: locales/en.json
    languages: de,fr
```

A `pip install "kaeris==0.2.43"` step before the action does not do it: that installs into
another Python, and the action then installs the newest CLI into its own. `PIP_CONSTRAINT` holds
for every later `pip install` in the job, and the file names only `kaeris`. The action asks for
0.2.39 or later, so pin 0.2.39 or newer.

## Links

- Website and docs: [kaeris.dev](https://kaeris.dev), [GitHub Actions recipe](https://kaeris.dev/ci-github-actions.html)
- CLI: `pip install kaeris` ([PyPI](https://pypi.org/project/kaeris/)); the check alone, without Python: `npx kaeris check` ([npm](https://www.npmjs.com/package/kaeris))
- MCP server for Claude, Cursor and other MCP clients: `pip install kaeris-mcp`
- Questions and ideas: [Discussions](https://github.com/kaeris-io/kaeris-cli/discussions). Security: [SECURITY.md](SECURITY.md)

## License

MIT — see [LICENSE](./LICENSE).
