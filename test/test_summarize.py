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


if __name__ == "__main__":
    unittest.main()
