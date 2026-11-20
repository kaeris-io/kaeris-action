"""The summary's last line links the site with ?utm_source=action (launch-drafts-1030/cli-cta.md).

The site reads the source of a visit only from ?utm_source= (not ?ref=), and a link opened from
a PR or a job summary arrives from github.com, mixed with every other GitHub link. One line, at
the end, in the summary file and the `summary` output only: summarize.py prints nothing itself.
Off with KAERIS_NO_CTA=1, and absent when `kaeris check` gave no result.

Runs summarize.py as action.yml does: python3 summarize.py --mode ... (stdlib unittest)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "summarize.py")
LINK = "https://kaeris.dev/?utm_source=action"
CHECK_LINE = f"<sub>Checked by [KAERIS]({LINK}) · free check, no key</sub>"
TRANSLATE_LINE = f"<sub>Translated by [KAERIS]({LINK})</sub>"

CLEAN = {"ok": True, "missing": {"de": []}, "extra": {"de": []}, "placeholder_issues": [],
         "missing_files": [], "faults": [], "warnings": []}
BROKEN = {"ok": False, "missing": {"de": ["b"]}, "extra": {}, "missing_files": [],
          "placeholder_issues": [{"lang": "de", "key": "a", "missing": ["{n}"], "added": []}]}


def run(mode="check", after=CLEAN, env=None):
    """(stdout, summary file, the `summary` output) of one summarize.py run."""
    with tempfile.TemporaryDirectory() as t:
        after_path = os.path.join(t, "after.json")
        if after is not None:
            with open(after_path, "w", encoding="utf-8") as f:
                json.dump(after, f)
        out_file, gh_out = os.path.join(t, "summary.md"), os.path.join(t, "gh_output")
        e = {k: v for k, v in os.environ.items() if k != "KAERIS_NO_CTA"}
        e.update(env or {})
        p = subprocess.run([sys.executable, SCRIPT, "--mode", mode, "--langs", "de",
                            "--after", after_path, "--out-file", out_file,
                            "--github-output", gh_out],
                           capture_output=True, text=True, encoding="utf-8", env=e, check=True)
        with open(out_file, encoding="utf-8") as f:
            summary = f.read()
        with open(gh_out, encoding="utf-8") as f:
            output = f.read()
    return p.stdout + p.stderr, summary, output


class Footer(unittest.TestCase):
    def test_check_ends_with_the_link(self):
        printed, summary, output = run("check")
        self.assertEqual(summary.rstrip("\n").splitlines()[-1], CHECK_LINE)
        self.assertIn(CHECK_LINE, output)
        self.assertEqual(summary.count(LINK), 1)

    def test_a_failed_check_too(self):
        _, summary, _ = run("check", BROKEN)
        self.assertEqual(summary.rstrip("\n").splitlines()[-1], CHECK_LINE)

    def test_translate_does_not_say_free_or_no_key(self):
        _, summary, output = run("translate")
        self.assertEqual(summary.rstrip("\n").splitlines()[-1], TRANSLATE_LINE)
        self.assertNotIn("no key", summary)
        self.assertIn(TRANSLATE_LINE, output)

    def test_nothing_on_stdout(self):
        printed, _, _ = run("check")
        self.assertNotIn("kaeris.dev", printed)

    def test_switched_off(self):
        _, summary, output = run("check", env={"KAERIS_NO_CTA": "1"})
        self.assertNotIn(LINK, summary + output)

    def test_not_when_check_gave_no_result(self):
        _, summary, output = run("check", after=None)
        self.assertNotIn(LINK, summary + output)



def summary_md(after, langs, check_exit=None, raw=None):
    """The summary for one `kaeris check --json` body (`raw`: the file's text as is, for a CLI
    that printed something else), with `--check-exit` as action.yml passes it."""
    with tempfile.TemporaryDirectory() as t:
        after_path, out_file = os.path.join(t, "after.json"), os.path.join(t, "summary.md")
        with open(after_path, "w", encoding="utf-8") as f:
            f.write(raw if raw is not None else json.dumps(after))
        cmd = [sys.executable, SCRIPT, "--mode", "check", "--langs", ",".join(langs),
               "--after", after_path, "--out-file", out_file]
        if check_exit is not None:
            cmd += ["--check-exit", check_exit]
        subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=True)
        with open(out_file, encoding="utf-8") as f:
            return f.read()


def status_column(after, langs, check_exit=None):
    """{language: Status cell} of the matrix for one `kaeris check --json` body."""
    md = summary_md(after, langs, check_exit)
    rows = [l.split("|") for l in md.splitlines() if l.startswith("| ") and "---" not in l]
    return {r[1].strip(): r[2].strip() for r in rows if r[1].strip() in langs}


# The real body of `kaeris check --json` (B, 0.2.46) on pl one/other (an error, red in 0.2.43
# too), de without `one` (a warning) and a lost placeholder in de: faults and warnings carry the
# language. The placeholder fault repeats placeholder_issues and is counted once.
PLURALS = {"ok": False, "missing": {}, "extra": {}, "missing_files": [],
           "placeholder_issues": [{"lang": "de", "key": "a", "missing": ["{name}"], "added": []}],
           "faults": [{"lang": "de", "key": "a", "msg": "lost placeholder {name}", "severity": "error"},
                      {"lang": "pl", "key": "f", "severity": "error",
                       "msg": "ICU plural is missing the few/many form(s) pl requires"}],
           "warnings": [{"lang": "de", "key": "f", "severity": "warn",
                         "msg": "ICU plural is missing the one form(s) de requires"}]}


class StatusColumn(unittest.TestCase):
    """A language is "complete" only when the check has nothing on it: critic 4 (19.11) saw
    "✓ complete" beside pl, red only for its plural, under an ::error that sends people to this
    matrix."""

    def test_a_language_red_only_for_a_plural_is_not_complete(self):
        self.assertEqual(status_column(PLURALS, ["pl"])["pl"], "⚠ 1 error")

    def test_a_warning_is_named_and_not_complete(self):
        after = {**PLURALS, "placeholder_issues": [], "faults": []}
        self.assertEqual(status_column(after, ["de"])["de"], "⚠ 1 warning")

    def test_the_placeholder_fault_is_counted_once(self):
        self.assertEqual(status_column(PLURALS, ["de"])["de"], "⚠ 1 placeholder issue, 1 warning")

    def test_a_clean_language_is_complete(self):
        self.assertEqual(status_column(CLEAN, ["de"])["de"], "✓ complete")

    def test_an_extra_key_is_named(self):
        # Real body under `strict: true` for a de with every key and one the source no longer
        # has, exit 1 (critic 5, fx-summ/de_extra_key_only): "ok" stays true and the extra key
        # is the whole failure. Without `strict` it is reported as a warning is.
        after = {**CLEAN, "extra": {"de": ["zz"]}}
        self.assertEqual(status_column(after, ["de"])["de"], "⚠ 1 extra key")


# The real body of `kaeris check --json` (0.2.43 and B, 0.2.46) for `languages: pt_BR`, file
# pt_BR.json, one key missing and a lost placeholder, exit 1: the CLI writes the language as
# pt-BR in every field (critic 5, fx-summ/ptBR_us_missing).
PT_BR = {"ok": False, "missing": {"pt-BR": ["b"]}, "extra": {}, "missing_files": [],
         "placeholder_issues": [{"lang": "pt-BR", "key": "a", "missing": ["{name}"], "added": []}],
         "faults": [{"lang": "pt-BR", "key": "a", "msg": "lost placeholder {name}", "severity": "error"}],
         "warnings": []}


class LanguageCodes(unittest.TestCase):
    """The CLI reads `pt_BR`, `PT-br` and `pt-BR` as one language and writes it pt-BR (language
    lower, script Title, region UPPER, `_` read as `-`). The matrix looked the language up as
    the user wrote it and found nothing: "| pt_BR | ✓ complete | 0 | 0 |" beside exit 1."""

    def test_an_underscore_code_finds_its_findings(self):
        md = summary_md(PT_BR, ["pt_BR"])
        self.assertIn("| pt_BR | ⚠ 1 missing, 1 placeholder issue | 1 | 1 | – |", md)
        self.assertIn("Placeholder-loss: **1**", md)

    def test_case_is_read_as_the_cli_reads_it(self):
        after = {**PT_BR, "missing": {"zh-Hant": ["b"]}, "placeholder_issues": [], "faults": []}
        self.assertEqual(status_column(after, ["zh_hant"])["zh_hant"], "⚠ 1 missing")

    def test_one_language_written_twice_is_one_row(self):
        md = summary_md(PT_BR, ["pt_BR", "pt-BR"])
        self.assertEqual(md.count("| pt_BR |") + md.count("| pt-BR |"), 1)


class FailedCheck(unittest.TestCase):
    """No language is "✓ complete" beside a red check (critic 5, minor 3), and when the check
    did not finish the summary says what the PR comment says (pr_comment.build_unfinished).
    `--check-exit` is the exit code action.yml passes."""

    def test_a_clean_language_with_exit_0_is_complete(self):
        self.assertEqual(status_column(CLEAN, ["de"], "0")["de"], "✓ complete")

    def test_an_extra_key_under_strict_is_the_failure(self):
        after = {**CLEAN, "extra": {"de": ["zz"]}}
        self.assertEqual(status_column(after, ["de"], "1")["de"], "⚠ 1 extra key")

    def test_a_warning_without_strict_with_exit_0_is_not_complete(self):
        after = {**PLURALS, "placeholder_issues": [], "faults": []}
        self.assertEqual(status_column(after, ["de"], "0")["de"], "⚠ 1 warning")

    def test_pt_BR_with_exit_1(self):
        self.assertEqual(status_column(PT_BR, ["pt_BR"], "1")["pt_BR"],
                         "⚠ 1 missing, 1 placeholder issue")

    def test_garbage_instead_of_json_did_not_finish(self):
        md = summary_md(None, ["de"], "1", raw="Traceback (most recent call last):\n  boom\n")
        self.assertIn("`kaeris check` did not finish (exit 1: it gave no result, most likely a crash)", md)
        self.assertNotIn("bad usage", md)
        self.assertNotIn("complete", md)

    def test_exit_2_did_not_finish_with_bad_usage(self):
        md = summary_md(None, ["de"], "2", raw="")
        self.assertIn("`kaeris check` did not finish (exit 2: bad usage or an unreadable file)", md)

    def test_check_that_never_ran_claims_nothing(self):
        md = summary_md(None, ["de"], raw="")
        self.assertIn("`kaeris check` did not produce a result", md)
        self.assertNotIn("complete", md)
        self.assertNotIn("bad usage", md)

    def test_empty_result_with_exit_1_did_not_finish(self):
        # `{}` lists nothing, so it cannot be why the check failed: the PR comment calls this
        # "did not finish" too (exit 1 and no findings).
        md = summary_md({}, ["de"], "1")
        self.assertIn("did not finish (exit 1: it gave no result, most likely a crash)", md)
        self.assertNotIn("complete", md)

    def test_empty_result_with_exit_0_is_complete(self):
        # Exit 0 is the CLI saying nothing is wrong, and `{}` names nothing either.
        self.assertEqual(status_column({}, ["de"], "0")["de"], "✓ complete")

    def test_a_finding_without_a_language_marks_no_language_complete(self):
        after = {"ok": False, "faults": [{"lang": None, "key": None, "msg": "x", "severity": "error"}]}
        md = summary_md(after, ["de"], "1")
        self.assertEqual(status_column(after, ["de"], "1")["de"], "⚠ not confirmed")
        self.assertIn("1 finding(s) in the result name no language listed here", md)

    def test_exit_1_with_nothing_on_any_language_did_not_finish(self):
        # A red check whose result places nothing on the languages asked for.
        md = summary_md(CLEAN, ["de"], "1")
        self.assertIn("did not finish (exit 1", md)
        self.assertNotIn("complete", md)

    def test_an_exit_above_1_did_not_finish_whatever_the_json(self):
        md = summary_md(CLEAN, ["de"], "3")
        self.assertIn("did not finish (exit 3: it gave no result, most likely a crash)", md)

    def test_another_language_can_still_be_complete(self):
        after = {**PLURALS, "placeholder_issues": [], "warnings": [], "faults": PLURALS["faults"][1:]}
        cols = status_column(after, ["pl", "de"], "1")
        self.assertEqual(cols, {"pl": "⚠ 1 error", "de": "✓ complete"})


if __name__ == "__main__":
    unittest.main()
