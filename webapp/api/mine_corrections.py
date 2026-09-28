"""The training-feedback half of the review loop, now that corrections
live in the database instead of paired CSV files - every record already
carries its original_fields snapshot next to whatever a DES has since
edited it to (see db.py's records table), so finding what changed is a
plain query, not a file diff. Everything past that point reuses
pdf_extractor.corrections' pattern-mining exactly as built and tested for
the CLI workflow - same candidate-list philosophy: this reports recurring
OCR-confusion substitutions and unrecognized-but-confirmed names for a
person to fold into parser.py/classify.py deliberately, never applies
anything automatically.

    python -m webapp.api.mine_corrections                    # everything ever corrected
    python -m webapp.api.mine_corrections --since 2026-09-01
    python -m webapp.api.mine_corrections --job 42
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pdf_extractor.corrections import mine_patterns  # noqa: E402

from . import db  # noqa: E402

_DIFF_FIELDS = ["name", "faculty", "department", "course", "degree", "grade",
                "distinction_notes", "year", "affiliated_institution"]


def find_corrections(job_id: int | None = None, since: datetime | None = None) -> list[dict]:
    query = select(db.records)
    if job_id is not None:
        query = query.where(db.records.c.job_id == job_id)
    if since is not None:
        query = query.where(db.records.c.updated_at >= since)
    with db.engine.begin() as conn:
        rows = conn.execute(query).mappings().all()

    diffs = []
    for r in rows:
        original = r["original_fields"] or {}
        for field in _DIFF_FIELDS:
            before, after = (original.get(field) or "").strip(), (r[field] or "").strip()
            if before != after:
                diffs.append({"row_index": r["id"], "field": field, "before": before, "after": after,
                               "confidence": r["confidence"], "flag_reason": r["flag_reason"]})
    return diffs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--job", type=int, default=None, help="Limit to one job id")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD - only corrections saved on or after this date")
    args = ap.parse_args()

    since = datetime.fromisoformat(args.since) if args.since else None
    diffs = find_corrections(job_id=args.job, since=since)
    if not diffs:
        print("No corrections found in that range.")
        return

    result = mine_patterns(diffs)
    print(f"{len(diffs)} field correction(s) found.\n")
    print("Recurring character substitutions (old -> new, count) - candidates for "
          "parser._fix_ocr_confusions:")
    for (old, new), count in result["substitutions"]:
        print(f"  {old!r:>6} -> {new!r:<6} x{count}")
    print("\nNames DES confirmed that the tool didn't recognize - candidates for the "
          "name vocabulary in classify.py:")
    for name, count in result["new_vocabulary"]:
        print(f"  {name:<20} x{count}")


if __name__ == "__main__":
    main()
