#!/usr/bin/env python3
"""Build a markdown summary (PR body / step summary) for the KAERIS i18n Action,
and emit GitHub Actions outputs — all derived from real `kaeris check --json`
output (and, best-effort, the `kaeris translate` log for overflow QA). No number
in here is fabricated: if the CLI didn't measure something, we say so instead of
guessing zero.

Usage:
  summarize.py --mode translate|check --langs es,fr,de \
      --after AFTER.json [--before BEFORE.json] [--log translate.log] \
      --out-file summary.md [--github-output "$GITHUB_OUTPUT"]
"""
import argparse
import json
import os
import re
import secrets
import sys


def load_json(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# How the CLI spells a language (kaeris/cli.py `_canon_lang`, the same in 0.2.43 and 0.2.46):
# `--langs pt_BR` is pt-BR in every field of its JSON, `zh_hant` is zh-Hant. The matrix shows
# the code as the user wrote it and looks it up the way the CLI wrote it.
_LANG_TAG_RE = re.compile(r"[A-Za-z]{2,3}(?:[-_](?:[A-Za-z]{2}|[A-Za-z]{4}|\d{3}))*")


def canon_lang(tag):
    """'pt_BR' → 'pt-BR', 'zh_hant' → 'zh-Hant': language lower, script Title, region UPPER,
    either separator. Not shaped like a language tag: as written."""
    tag = str(tag if tag is not None else "").strip()
    if not _LANG_TAG_RE.fullmatch(tag):
        return tag
    first, *rest = re.split(r"[-_]", tag)
    return "-".join([first.lower()] + [p.upper() if len(p) == 2 else p.title() if len(p) == 4
                                       else p for p in rest])


def parse_overflow_count(log_path):
    """Best-effort: `kaeris translate` (whole-file mode only, i.e. --only-new not set)
    prints a line like '! 3 translation(s) may overflow your UI ...' to stderr with
    Translation QA. --only-new (the Action's default) doesn't run this QA step yet,
    so return None (not measured) rather than fabricating 0."""
    if not log_path or not os.path.isfile(log_path):
        return None
    total = None
    pattern = re.compile(r"(\d+)\s+translation\(s\)\s+may overflow")
    with open(log_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = pattern.search(line)
            if m:
                total = (total or 0) + int(m.group(1))
    return total


# The last line of the summary: a plain link to the site. ?utm_source= is the only parameter the
# site reads for the source of a visit (not ?ref=). One line, in the summary only; off with
# KAERIS_NO_CTA=1. `check` needs no key and costs nothing, `translate` does not say so.
SITE_URL = "https://kaeris.dev/?utm_source=action"


def site_footer(mode, env=None):
    env = os.environ if env is None else env
    if env.get("KAERIS_NO_CTA", "").strip().lower() not in ("", "0", "false", "no"):
        return None
    if mode == "check":
        return f"<sub>Checked by [KAERIS]({SITE_URL}) · free check, no key</sub>"
    return f"<sub>Translated by [KAERIS]({SITE_URL})</sub>"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="translate", choices=["translate", "check"])
    ap.add_argument("--langs", required=True)
    ap.add_argument("--after", required=True)
    ap.add_argument("--before", default=None)
    ap.add_argument("--log", default=None)
    ap.add_argument("--out-file", required=True)
    ap.add_argument("--github-output", default=None)
    args = ap.parse_args()

    # One row per language as the CLI counts them (`pt_BR,pt-BR` is one), spelled as written.
    langs, seen = [], set()
    for l in args.langs.split(","):
        if l.strip() and canon_lang(l) not in seen:
            seen.add(canon_lang(l))
            langs.append(l.strip())
    after = load_json(args.after)
    before = load_json(args.before)

    if after is None:
        # `kaeris check` itself failed to run (bad usage, exit 2) — no data to summarize.
        md = ["## KAERIS i18n\n", "`kaeris check` did not produce a result "
              "(bad usage — check source/langs/out) — see the step log above.\n"]
        write(args, "\n".join(md), missing=None, ph=None)
        return

    def by_lang(mapping):
        out = {}
        for lang, keys in (mapping or {}).items():
            out.setdefault(canon_lang(lang), []).extend(keys or [])
        return out

    def count(items):
        out = {}
        for i in items:
            out[canon_lang(i.get("lang"))] = out.get(canon_lang(i.get("lang")), 0) + 1
        return out

    missing_after = by_lang(after.get("missing"))
    missing_before = by_lang(before.get("missing")) if before and isinstance(before, dict) else None
    extra = by_lang(after.get("extra"))
    placeholder_issues = after.get("placeholder_issues") or []
    ph_by_lang = count(placeholder_issues)
    missing_files = {canon_lang(l) for l in after.get("missing_files") or []}
    # Findings beyond missing keys and placeholders (a plural form, a number, a tag…), by
    # language: an error fails the check, a warning fails it only with `strict`. A placeholder
    # fault repeats placeholder_issues and is counted there.
    ph_keys = {(canon_lang(i.get("lang")), i.get("key")) for i in placeholder_issues}
    faults = [f for f in after.get("faults") or []
              if not ((canon_lang(f.get("lang")), f.get("key")) in ph_keys
                      and "placeholder" in (f.get("msg") or ""))]
    errors_by_lang, warnings_by_lang = count(faults), count(after.get("warnings") or [])

    overflow = parse_overflow_count(args.log) if args.mode == "translate" else None

    total_missing = 0
    total_ph = 0
    total_translated = 0
    have_before = missing_before is not None

    rows = []
    for lang in langs:
        c = canon_lang(lang)
        m_after = len(missing_after.get(c, []))
        ph = ph_by_lang.get(c, 0)
        total_missing += m_after
        total_ph += ph

        translated_cell = "–"
        if have_before:
            m_before = len(missing_before.get(c, []))
            translated = max(m_before - m_after, 0)
            total_translated += translated
            translated_cell = str(translated)

        errs, warns = errors_by_lang.get(c, 0), warnings_by_lang.get(c, 0)
        # A key the source no longer has: what fails a `strict` check, a warning otherwise.
        stale = len(extra.get(c, []))
        if c in missing_files:
            status = "⚠ locale file missing"
        elif m_after == 0 and ph == 0 and errs == 0 and warns == 0 and stale == 0:
            status = "✓ complete"
        else:
            bits = []
            if m_after:
                bits.append(f"{m_after} missing")
            if ph:
                bits.append(f"{ph} placeholder issue{'s' if ph != 1 else ''}")
            if errs:
                bits.append(f"{errs} error{'s' if errs != 1 else ''}")
            if warns:
                bits.append(f"{warns} warning{'s' if warns != 1 else ''}")
            if stale:
                bits.append(f"{stale} extra key{'s' if stale != 1 else ''}")
            status = "⚠ " + ", ".join(bits)

        rows.append((lang, status, m_after, ph, translated_cell))

    lines = ["## KAERIS i18n" + (" — CI check" if args.mode == "check" else " — translate")]
    lines.append("")
    if args.mode == "translate":
        if have_before:
            lines.append(f"Translated **{total_translated}** key(s) across **{len(langs)}** language(s).")
        else:
            lines.append(f"Ran across **{len(langs)}** language(s) (no baseline available for a translated-key count).")
    lines.append(f"Placeholder-loss: **{total_ph}** (should be 0).")
    if args.mode == "translate":
        if overflow is None:
            lines.append("Overflow warnings: _not measured_ (only available without `--only-new`/incremental mode).")
        else:
            lines.append(f"Overflow warnings: **{overflow}**.")
    if total_missing:
        lines.append(f"Missing keys remaining: **{total_missing}**.")
    lines.append("")
    lines.append("| Language | Status | Missing | Placeholder issues | Translated |")
    lines.append("|---|---|---|---|---|")
    for lang, status, m_after, ph, translated_cell in rows:
        lines.append(f"| {lang} | {status} | {m_after} | {ph} | {translated_cell} |")

    if any(extra.values()):
        lines.append("")
        n_extra = sum(len(v) for v in extra.values())
        lines.append(f"_{n_extra} extra/stale key(s) found in target locale(s) not in the source "
                     "(warning only unless `--strict`)._")

    lines.append("")
    lines.append("_Generated from `kaeris check --json`" +
                  ("" if overflow is None else " and the translate log") + " — no fabricated numbers._")
    footer = site_footer(args.mode)
    if footer:
        lines.append("")
        lines.append(footer)

    write(args, "\n".join(lines) + "\n", missing=total_missing, ph=total_ph)


def write(args, markdown, missing, ph):
    with open(args.out_file, "w", encoding="utf-8") as f:
        f.write(markdown)

    if not args.github_output:
        return
    with open(args.github_output, "a", encoding="utf-8") as f:
        f.write(f"missing={missing if missing is not None else ''}\n")
        f.write(f"placeholder-issues={ph if ph is not None else ''}\n")
        delim = "KAERIS_SUMMARY_" + secrets.token_hex(8)
        f.write(f"summary<<{delim}\n")
        f.write(markdown)
        if not markdown.endswith("\n"):
            f.write("\n")
        f.write(f"{delim}\n")


if __name__ == "__main__":
    sys.exit(main())
