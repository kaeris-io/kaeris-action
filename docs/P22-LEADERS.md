# P2.2: what the leaders show inside a pull request

Measured 18.11.2026 from their own action.yml / docs (sources at the end). Question: does a
developer see translation quality findings (broken `{name}`, missing keys) right in the PR,
and what does it take to get there?

| | Lingo.dev | Crowdin | Tolgee | KAERIS Action (P2.2) |
|---|---|---|---|---|
| What runs in CI | `npx lingo.dev ci`: translates, commits or opens a PR (`pull-request: false` by default) | uploads sources, downloads translations, opens a PR (`create_pull_request: true`) | CLI in a workflow: `push` / `pull`, `extract check` | `kaeris check`: static gate, no network call to KAERIS |
| PR comment with quality findings | No. The GitHub **App** keeps one comment with "the translated files and any failures" (job failures, not placeholder/missing-key findings). The Action posts nothing. | No comment inputs at all; the PR body is free text, not QA | No comment. `extract check` adds annotations about keys extracted from code, not translation quality | Yes: one comment, updated on every push, table of file / key / language / problem with links to the line |
| Placeholder / missing-key check in the PR | Not documented | Not in the action (QA checks live in Crowdin's web editor) | Not documented | Yes, and the job goes red |
| Account / key / App needed | Account + `LINGO_API_KEY` for the Action; the App needs an org, an engine and an admin install | Crowdin account, project id, personal token | Tolgee project + API key for push/pull | None: `GITHUB_TOKEN` and `permissions: pull-requests: write` |
| Private repo without GitHub Code Security | Comment (App only) works | n/a | Annotations work | Comment works; SARIF upload does not show there, which is why the comment exists |

What we take from this:

- Nobody shows translation quality findings inside the PR. The leaders' PR story is "here are
  the translated files", and quality lives in their web dashboards behind an account.
- The one comment that exists (Lingo.dev App) is the pattern to match on form: one comment,
  edited in place, never a new one per push.
- Our edge is the combination: findings with file and line, red job, zero setup beyond one
  `permissions` line, works in a private repo where SARIF annotations are paywalled.

Sources:

- Lingo.dev action.yml: https://github.com/lingodotdev/lingo.dev/blob/main/action.yml
- Lingo.dev GitHub App guide: https://lingo.dev/en/docs/workflows/github-app
- Lingo.dev CI page: https://lingo.dev/en/ci/github
- Crowdin action.yml: https://github.com/crowdin/github-action/blob/master/action.yml
- Tolgee CLI, syncing strings (`extract check`): https://docs.tolgee.io/tolgee-cli/extraction/syncing-strings
