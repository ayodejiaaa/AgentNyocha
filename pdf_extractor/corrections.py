"""Behind-the-scenes half of the review loop: once a DES has corrected a
flagged review CSV in place, this diffs it against what the tool
originally produced and turns that into two things -

  1. an audit log of exactly what changed (for folding into the canonical
     training data - deliberately left as a separate, reviewable file
     rather than this script silently rewriting Canonical Training Data
     itself), and
  2. mined patterns - recurring single-character OCR substitutions, and
     real names the tool didn't recognize - that are candidates for
     strengthening parser._fix_ocr_confusions and classify.py's name
     vocabulary, so future extractions need fewer corrections.

DES never touch this: they correct the review CSV and export a clean
copy (review.py); this runs separately over the pairs of (original,
corrected) files as they accumulate.

    python -m pdf_extractor.corrections diff <original.csv> <corrected.csv> -o log.csv
    python -m pdf_extractor.corrections mine <log.csv> [<log2.csv> ...]
"""

import argparse
import csv
import difflib
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .classify import _NAME_WORDS  # noqa: SLF001 - read-only, for "is this name new" checks
from .parser import CANON_FIELDS
from .review import REVIEW_FIELDS

# page/programme/country change rarely and mostly by design (header state
# corrections cascade); the fields worth diffing are the ones a DES is
# actually checking against the snapshot.
_DIFF_FIELDS = ["name", "faculty", "department", "course", "degree", "grade",
                "distinction_notes", "year", "affiliated_institution"]

LOG_FIELDS = ["row_index", "field", "before", "after", "confidence", "flag_reason", "diffed_at"]


def find_corrections(original_rows: list[dict], corrected_rows: list[dict]) -> list[dict]:
    """Row-for-row diff by position - both files must be the same review
    CSV, one before and one after editing, same row order (which is what
    "correct it in place" naturally produces; rows a DES deletes outright
    just stop contributing corrections, they aren't matched up by
    content)."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    diffs = []
    for i, (orig, fixed) in enumerate(zip(original_rows, corrected_rows)):
        for field in _DIFF_FIELDS:
            before, after = (orig.get(field) or "").strip(), (fixed.get(field) or "").strip()
            if before != after:
                diffs.append({
                    "row_index": i, "field": field, "before": before, "after": after,
                    "confidence": orig.get("confidence", ""), "flag_reason": orig.get("flag_reason", ""),
                    "diffed_at": now,
                })
    return diffs


def write_corrections_log(diffs: list[dict], out_path: str):
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        writer.writeheader()
        writer.writerows(diffs)


def diff_files(original_csv: str, corrected_csv: str, out_path: str, verbose: bool = True):
    with open(original_csv, encoding="utf-8") as f:
        original_rows = list(csv.DictReader(f))
    with open(corrected_csv, encoding="utf-8") as f:
        corrected_rows = list(csv.DictReader(f))
    if len(original_rows) != len(corrected_rows):
        print(f"WARNING: row counts differ ({len(original_rows)} vs {len(corrected_rows)}) - "
              f"rows were added or deleted, not just edited; diffing by position anyway, "
              f"which is only reliable up to the first such change.", file=sys.stderr)
    diffs = find_corrections(original_rows, corrected_rows)
    write_corrections_log(diffs, out_path)
    if verbose:
        name_changes = sum(1 for d in diffs if d["field"] == "name")
        print(f"{len(diffs)} correction(s) across {len(original_rows)} row(s) "
              f"({name_changes} to the name itself) -> {out_path}", file=sys.stderr)


def _char_substitutions(before: str, after: str) -> list[tuple[str, str]]:
    """Single-character (or short-run) substitutions implied by a name
    correction, via a plain sequence diff - e.g. "OSUNNUB" -> "OSUNNUBI"
    yields ("", "I"); "QJUKWU" -> "OJUKWU" yields ("Q", "O"). Longer
    replaced runs are skipped: they're more likely a genuinely different
    name than one OCR mistake, and mining those would risk learning
    something specific to one person rather than a real OCR confusion."""
    sm = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    out = []
    for tag, a0, a1, b0, b1 in sm.get_opcodes():
        if tag == "equal":
            continue
        old, new = before[a0:a1], after[b0:b1]
        if len(old) <= 2 and len(new) <= 2:
            out.append((old, new))
    return out


def mine_patterns(diffs: list[dict]) -> dict:
    """Two candidate lists for a human to review before wiring into the
    tool - this never edits parser.py/classify.py itself, since a
    pattern's real frequency across corrections matters more than one
    edit, and a wrong OCR-confusion rule (unlike a flag) actively
    corrupts future output."""
    subs = Counter()
    new_names = Counter()
    for d in diffs:
        if d["field"] != "name" or not d["before"] or not d["after"]:
            continue
        for old, new in _char_substitutions(d["before"].upper(), d["after"].upper()):
            subs[(old, new)] += 1
        for tok in d["after"].upper().split():
            tok = "".join(c for c in tok if c.isalpha())
            if len(tok) >= 3 and tok not in _NAME_WORDS:
                new_names[tok] += 1
    return {"substitutions": subs.most_common(30), "new_vocabulary": new_names.most_common(50)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("diff", help="Diff an original review CSV against its corrected copy")
    d.add_argument("original_csv")
    d.add_argument("corrected_csv")
    d.add_argument("-o", "--out", required=True)

    m = sub.add_parser("mine", help="Summarize recurring patterns across one or more correction logs")
    m.add_argument("logs", nargs="+")

    args = ap.parse_args()
    if args.cmd == "diff":
        diff_files(args.original_csv, args.corrected_csv, args.out)
    elif args.cmd == "mine":
        diffs = []
        for log in args.logs:
            with open(log, encoding="utf-8") as f:
                diffs.extend(csv.DictReader(f))
        result = mine_patterns(diffs)
        print(f"Recurring character substitutions (old -> new, count), across {len(diffs)} logged change(s):")
        for (old, new), count in result["substitutions"]:
            print(f"  {old!r:>6} -> {new!r:<6} x{count}")
        print(f"\nNames DES confirmed that the tool didn't recognize (candidates for the name vocabulary):")
        for name, count in result["new_vocabulary"]:
            print(f"  {name:<20} x{count}")


if __name__ == "__main__":
    main()
