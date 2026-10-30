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

What it learns is kept in the database (the mined_patterns table), so the
candidate list has a memory: what has been seen and how often, and - the part
that matters - what a person already decided about each pattern, so the same
candidate isn't put in front of anyone twice. Each pattern also records how many
different booklets (university + year) it was seen in, and --list puts the widest
spread first: thirty identical corrections in one booklet are one bad scan, not a
pattern, and shouldn't outrank five spread across three.

    python -m webapp.api.mine_corrections                    # report only, saves nothing
    python -m webapp.api.mine_corrections --since 2026-09-01 # report over a window
    python -m webapp.api.mine_corrections --job 42           # report over one job
    python -m webapp.api.mine_corrections --save             # mine everything, keep the result
    python -m webapp.api.mine_corrections --list             # what's waiting for a decision
    python -m webapp.api.mine_corrections --list --status all --min-count 3
    python -m webapp.api.mine_corrections --list --min-booklets 3   # only patterns seen in 3+ booklets
    python -m webapp.api.mine_corrections --mark 12 accepted --by jane@akowe.example
    python -m webapp.api.mine_corrections --mark 12 rejected

--save always covers every correction ever made, never a slice: a --job or
--since run would otherwise overwrite the stored totals with a partial count.
Run it as often as you like - counts are recomputed, not added to, so a
repeat run changes nothing, and decisions are never overwritten. Something
still has to run it (a scheduled job, or by hand); nothing here does so on
its own.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pdf_extractor.classify import _NAME_WORDS  # noqa: E402, SLF001 - read-only "is this token already known"
from pdf_extractor.corrections import mine_patterns  # noqa: E402

from . import db  # noqa: E402

_DIFF_FIELDS = ["name", "faculty", "department", "course", "degree", "grade",
                "distinction_notes", "year", "affiliated_institution"]

# What a person can set. "in_vocabulary" is set only by persist_patterns, when a
# name token that used to be a candidate turns up in the vocabulary on its own.
HUMAN_STATUSES = ("candidate", "accepted", "rejected")
ALL_STATUSES = HUMAN_STATUSES + ("in_vocabulary",)


def booklet_key(university: str, year: str) -> str:
    """One booklet = one university + year, however many times it was uploaded:
    a re-upload, or the parts of a split booklet, are the same booklet. Case and
    stray spaces in what a DES typed at upload are ignored."""
    return f"{(university or '').strip().lower()}|{(year or '').strip()}"


def find_corrections(job_id: int | None = None, since: datetime | None = None, engine=None) -> list[dict]:
    query = select(db.records).where(db.records.c.removed.is_(False))   # a removed row teaches nothing
    if job_id is not None:
        query = query.where(db.records.c.job_id == job_id)
    if since is not None:
        query = query.where(db.records.c.updated_at >= since)
    with (engine or db.engine).begin() as conn:
        rows = conn.execute(query).mappings().all()
        booklets = {j["id"]: booklet_key(j["university"], j["year"]) for j in conn.execute(
            select(db.jobs.c.id, db.jobs.c.university, db.jobs.c.year)).mappings()}

    diffs = []
    for r in rows:
        original = r["original_fields"] or {}
        for field in _DIFF_FIELDS:
            before, after = (original.get(field) or "").strip(), (r[field] or "").strip()
            if before != after:
                diffs.append({"row_index": r["id"], "field": field, "before": before, "after": after,
                               "confidence": r["confidence"], "flag_reason": r["flag_reason"],
                               "booklet": booklets.get(r["job_id"], "")})
    return diffs


def _substitution_key(old: str, new: str) -> str:
    return json.dumps([old, new])  # unambiguous even when a side is empty or odd punctuation


def describe(kind: str, key: str) -> str:
    if kind == "substitution":
        old, new = json.loads(key)
        return f"{old!r} -> {new!r}"
    return key


def persist_patterns(result: dict, engine=None, now: datetime | None = None) -> dict:
    """Upserts one full (untruncated) mine_patterns() result into
    mined_patterns. `count` is set to the evidence in `result`, never added to
    it, which is what makes running this twice harmless (as is `booklets`, the
    number of different booklets that evidence came from); `peak_count`,
    `first_seen` and a person's `status` are never lowered or overwritten. A
    pattern absent from `result` has lost its evidence (its corrections were
    reverted or removed) and drops to count 0 - or, for a name token that has
    since joined the vocabulary, is marked in_vocabulary if nobody had decided
    on it yet."""
    engine = engine or db.engine
    now = now or datetime.now(timezone.utc)
    examples = result.get("substitution_examples", {})
    sub_booklets = result.get("substitution_booklets", {})
    name_booklets = result.get("vocabulary_booklets", {})
    seen: dict[tuple[str, str], tuple[int, str, int]] = {}
    for (old, new), count in result["substitutions"]:
        seen[("substitution", _substitution_key(old, new))] = (
            count, examples.get((old, new), ""), sub_booklets.get((old, new), 0))
    for token, count in result["new_vocabulary"]:
        seen[("vocabulary", token)] = (count, "", name_booklets.get(token, 0))

    stats = {"new": 0, "updated": 0, "no_longer_seen": 0, "now_in_vocabulary": 0}
    table = db.mined_patterns
    with engine.begin() as conn:
        existing = {(r["kind"], r["key"]): r for r in conn.execute(select(table)).mappings()}
        for (kind, key), (count, example, booklets) in seen.items():
            row = existing.get((kind, key))
            if row is None:
                conn.execute(table.insert().values(
                    kind=kind, key=key, count=count, peak_count=count, booklets=booklets, example=example,
                    status="candidate", first_seen=now, last_seen=now))
                stats["new"] += 1
            else:
                conn.execute(update(table).where(table.c.id == row["id"]).values(
                    count=count, peak_count=max(row["peak_count"], count), booklets=booklets,
                    example=example or row["example"], last_seen=now))
                stats["updated"] += 1
        for (kind, key), row in existing.items():
            if (kind, key) in seen:
                continue
            values = {}
            if row["count"]:
                values["count"] = 0
                values["booklets"] = 0
                stats["no_longer_seen"] += 1
            if kind == "vocabulary" and row["status"] == "candidate" and key in _NAME_WORDS:
                values["status"] = "in_vocabulary"
                stats["now_in_vocabulary"] += 1
            if values:
                conn.execute(update(table).where(table.c.id == row["id"]).values(**values))
    return stats


def mine_and_save(engine=None, now: datetime | None = None) -> dict:
    """Mines every correction currently in the database and stores the result.
    Creates the mined_patterns table first if this database predates it
    (create_all only ever adds missing tables)."""
    engine = engine or db.engine
    db.metadata.create_all(engine)
    diffs = find_corrections(engine=engine)
    result = mine_patterns(diffs, limit_substitutions=None, limit_names=None)
    return {"corrections": len(diffs), **persist_patterns(result, engine, now)}


def list_patterns(status: str = "candidate", kind: str | None = None, min_count: int = 1,
                  min_booklets: int = 0, engine=None) -> list[dict]:
    """Widest spread across booklets first, then most corrections: a pattern
    seen in several booklets is a real confusion, a large count from one booklet
    may be a single bad scan."""
    query = select(db.mined_patterns).where(db.mined_patterns.c.count >= min_count,
                                            db.mined_patterns.c.booklets >= min_booklets)
    if status != "all":
        query = query.where(db.mined_patterns.c.status == status)
    if kind:
        query = query.where(db.mined_patterns.c.kind == kind)
    query = query.order_by(db.mined_patterns.c.booklets.desc(), db.mined_patterns.c.count.desc(),
                           db.mined_patterns.c.id)
    with (engine or db.engine).begin() as conn:
        return [dict(r) for r in conn.execute(query).mappings()]


def set_status(pattern_id: int, status: str, by_email: str | None = None, engine=None,
               now: datetime | None = None) -> bool:
    """Records a person's decision. False if there's no such pattern. Setting
    it back to "candidate" clears who/when, since it's undecided again."""
    if status not in HUMAN_STATUSES:
        raise ValueError(f"status must be one of {HUMAN_STATUSES}, not {status!r}")
    engine = engine or db.engine
    with engine.begin() as conn:
        user_id = None
        if by_email:
            user_id = conn.execute(select(db.users.c.id).where(
                db.users.c.email == by_email.lower().strip())).scalar()
            if user_id is None:
                raise LookupError(f"no user with email {by_email!r}")
        decided = status != "candidate"
        result = conn.execute(update(db.mined_patterns).where(db.mined_patterns.c.id == pattern_id).values(
            status=status,
            decided_by=user_id if decided else None,
            decided_at=(now or datetime.now(timezone.utc)) if decided else None))
        return result.rowcount > 0


def _spread(booklets: dict, key) -> str:
    n = booklets.get(key)
    return f"   in {n} booklet{'s' if n != 1 else ''}" if n else ""


def _print_report(result: dict):
    sub_booklets = result.get("substitution_booklets", {})
    name_booklets = result.get("vocabulary_booklets", {})
    print("Recurring character substitutions (old -> new, count) - candidates for "
          "parser._fix_ocr_confusions:")
    for (old, new), count in result["substitutions"]:
        print(f"  {old!r:>6} -> {new!r:<6} x{count}{_spread(sub_booklets, (old, new))}")
    print("\nNames DES confirmed that the tool didn't recognize - candidates for the "
          "name vocabulary in classify.py:")
    for name, count in result["new_vocabulary"]:
        print(f"  {name:<20} x{count}{_spread(name_booklets, name)}")


def _print_list(rows: list[dict]):
    if not rows:
        print("Nothing matches.")
        return
    print(f"{'id':>4}  {'kind':<12} {'count':>5} {'booklets':>8} {'peak':>5}  {'status':<13} pattern")
    for r in rows:
        line = (f"{r['id']:>4}  {r['kind']:<12} {r['count']:>5} {r['booklets']:>8} {r['peak_count']:>5}  "
                f"{r['status']:<13} {describe(r['kind'], r['key'])}")
        if r["example"]:
            line += f"   ({r['example']})"
        print(line)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--job", type=int, default=None, help="Limit the report to one job id")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD - only corrections saved on or after this date")
    ap.add_argument("--save", action="store_true", help="Mine every correction and keep the result in the database")
    ap.add_argument("--list", action="store_true", help="Show stored patterns instead of mining")
    ap.add_argument("--status", default="candidate", choices=[*ALL_STATUSES, "all"],
                    help="With --list: which patterns to show (default: candidate, i.e. undecided)")
    ap.add_argument("--kind", choices=["substitution", "vocabulary"], default=None, help="With --list")
    ap.add_argument("--min-count", type=int, default=1, help="With --list: hide patterns with less evidence")
    ap.add_argument("--min-booklets", type=int, default=0,
                    help="With --list: hide patterns seen in fewer different booklets")
    ap.add_argument("--mark", nargs=2, metavar=("ID", "STATUS"),
                    help=f"Record a decision on a stored pattern: STATUS is one of {', '.join(HUMAN_STATUSES)}")
    ap.add_argument("--by", metavar="EMAIL", default=None, help="With --mark: who is deciding")
    args = ap.parse_args(argv)

    if args.save and (args.job is not None or args.since):
        print("--save covers every correction; it can't be combined with --job or --since "
              "(a partial count would overwrite the stored totals).", file=sys.stderr)
        return 2

    db.init_db()

    if args.mark:
        try:
            pattern_id, status = int(args.mark[0]), args.mark[1]
            found = set_status(pattern_id, status, by_email=args.by)
        except (ValueError, LookupError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 2
        if not found:
            print(f"No stored pattern with id {pattern_id}.", file=sys.stderr)
            return 1
        print(f"Pattern {pattern_id} marked {status}.")
        return 0

    if args.list:
        _print_list(list_patterns(args.status, args.kind, args.min_count, args.min_booklets))
        return 0

    since = datetime.fromisoformat(args.since) if args.since else None
    diffs = find_corrections(job_id=args.job, since=since)
    if not diffs and not args.save:
        print("No corrections found in that range.")
        return 0

    if diffs:
        print(f"{len(diffs)} field correction(s) found.\n")
        _print_report(mine_patterns(diffs))
    else:
        print("No corrections found.")  # still saved below, so previously-seen patterns drop to zero
    if args.save:
        stats = mine_and_save()
        print(f"\nSaved: {stats['new']} new, {stats['updated']} updated, "
              f"{stats['no_longer_seen']} lost their evidence, "
              f"{stats['now_in_vocabulary']} now in the vocabulary. "
              f"Review with: python -m webapp.api.mine_corrections --list")
    return 0


if __name__ == "__main__":
    sys.exit(main())
