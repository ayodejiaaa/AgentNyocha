"""Tests for the stored side of correction-mining (mine_corrections.py):
what persists, what a repeat run must not change, and what a person's
decision protects. Runs against a throwaway in-memory SQLite database, never
the real one.

    python -m webapp.api.test_mine_corrections
"""

import contextlib
import io
import sys
from datetime import datetime, timedelta

from sqlalchemy import create_engine, select, update

from . import db
from . import mine_corrections as mc

_failures = []


def check(label, got, want):
    if got != want:
        _failures.append(f"FAIL {label}: got {got!r}, want {want!r}")


T0 = datetime(2026, 10, 1, 9, 0, 0)
T1 = T0 + timedelta(days=3)


def fresh():
    engine = create_engine("sqlite://")
    db.metadata.create_all(engine)
    return engine


def result(subs=(), names=(), examples=None, sub_booklets=None, name_booklets=None):
    return {"substitutions": list(subs), "new_vocabulary": list(names), "substitution_examples": examples or {},
            "substitution_booklets": sub_booklets or {}, "vocabulary_booklets": name_booklets or {}}


def rows_by_key(engine):
    with engine.begin() as conn:
        return {(r["kind"], r["key"]): r for r in conn.execute(select(db.mined_patterns)).mappings()}


Q_TO_O = mc._substitution_key("Q", "O")
ADDED_I = mc._substitution_key("", "I")


def test_first_save_stores_every_pattern():
    e = fresh()
    stats = mc.persist_patterns(result([(("Q", "O"), 3), (("", "I"), 1)], [("ZQXVTA", 2)],
                                       {("Q", "O"): "QKAFOR -> OKAFOR"}), engine=e, now=T0)
    rows = rows_by_key(e)
    check("three patterns stored", len(rows), 3)
    check("reported as new", stats["new"], 3)
    q = rows[("substitution", Q_TO_O)]
    check("count and peak start equal", (q["count"], q["peak_count"]), (3, 3))
    check("a token-pair example is kept", q["example"], "QKAFOR -> OKAFOR")
    check("everything starts undecided", {r["status"] for r in rows.values()}, {"candidate"})
    check("a dropped-character substitution keeps its empty side",
          mc.describe("substitution", ADDED_I), "'' -> 'I'")


def test_rerunning_changes_nothing():
    e = fresh()
    r = result([(("Q", "O"), 3)], [("ZQXVTA", 2)])
    mc.persist_patterns(r, engine=e, now=T0)
    stats = mc.persist_patterns(r, engine=e, now=T1)
    rows = rows_by_key(e)
    check("no duplicate rows", len(rows), 2)
    check("counts are not added to", rows[("substitution", Q_TO_O)]["count"], 3)
    check("nothing reported new", stats["new"], 0)
    check("first_seen is untouched", rows[("substitution", Q_TO_O)]["first_seen"], T0)
    check("last_seen moves forward", rows[("substitution", Q_TO_O)]["last_seen"], T1)


def test_count_follows_evidence_and_peak_remembers():
    e = fresh()
    mc.persist_patterns(result([(("Q", "O"), 3)]), engine=e, now=T0)
    mc.persist_patterns(result([(("Q", "O"), 5)]), engine=e, now=T0)
    row = rows_by_key(e)[("substitution", Q_TO_O)]
    check("grows with new corrections", (row["count"], row["peak_count"]), (5, 5))
    mc.persist_patterns(result([(("Q", "O"), 2)]), engine=e, now=T0)
    row = rows_by_key(e)[("substitution", Q_TO_O)]
    check("shrinks if corrections are reverted, peak kept", (row["count"], row["peak_count"]), (2, 5))
    stats = mc.persist_patterns(result(), engine=e, now=T0)
    row = rows_by_key(e)[("substitution", Q_TO_O)]
    check("a pattern with no evidence left drops to zero", (row["count"], row["peak_count"]), (0, 5))
    check("...and is reported as such", stats["no_longer_seen"], 1)
    check("a blank example never erases a stored one",
          (mc.persist_patterns(result([(("Q", "O"), 1)], examples={("Q", "O"): "A -> B"}), engine=e, now=T0),
           mc.persist_patterns(result([(("Q", "O"), 1)]), engine=e, now=T0),
           rows_by_key(e)[("substitution", Q_TO_O)]["example"])[2], "A -> B")


def test_decisions_survive_reruns():
    e = fresh()
    with e.begin() as conn:
        conn.execute(db.users.insert().values(email="jane@akowe.example", password_hash="x", full_name="Jane"))
    mc.persist_patterns(result([(("Q", "O"), 3)]), engine=e, now=T0)
    pid = rows_by_key(e)[("substitution", Q_TO_O)]["id"]
    check("marking a real pattern succeeds", mc.set_status(pid, "rejected", "Jane@Akowe.example", engine=e, now=T0), True)
    mc.persist_patterns(result([(("Q", "O"), 9)]), engine=e, now=T1)
    row = rows_by_key(e)[("substitution", Q_TO_O)]
    check("more evidence doesn't reopen a rejected pattern", (row["status"], row["count"]), ("rejected", 9))
    check("who decided is remembered", row["decided_by"] is not None and row["decided_at"] == T0, True)

    check("an unknown pattern id reports False", mc.set_status(9999, "accepted", engine=e), False)
    for bad in ("in_vocabulary", "maybe"):
        try:
            mc.set_status(pid, bad, engine=e)
            check(f"status {bad!r} is refused", "accepted", "refused")
        except ValueError:
            pass
    try:
        mc.set_status(pid, "accepted", "nobody@akowe.example", engine=e)
        check("an unknown decider is refused", "accepted", "refused")
    except LookupError:
        pass
    check("a refused change leaves the decision alone", rows_by_key(e)[("substitution", Q_TO_O)]["status"], "rejected")
    mc.set_status(pid, "candidate", engine=e)
    row = rows_by_key(e)[("substitution", Q_TO_O)]
    check("reopening clears who and when", (row["status"], row["decided_by"], row["decided_at"]),
          ("candidate", None, None))


def test_vocabulary_graduation():
    e = fresh()
    mc.persist_patterns(result(names=[("ZQXVTA", 2), ("ZQXVTB", 2), ("ZQXVTC", 2)]), engine=e, now=T0)
    ids = {k[1]: r["id"] for k, r in rows_by_key(e).items()}
    mc.set_status(ids["ZQXVTB"], "rejected", engine=e)
    mc.set_status(ids["ZQXVTC"], "accepted", engine=e)
    real = mc._NAME_WORDS
    mc._NAME_WORDS = {**real, "ZQXVTA": 1, "ZQXVTB": 1, "ZQXVTC": 1}   # all three have since joined the vocabulary
    try:
        stats = mc.persist_patterns(result(), engine=e, now=T1)
    finally:
        mc._NAME_WORDS = real
    rows = rows_by_key(e)
    check("an undecided token now in the vocabulary is closed out automatically",
          rows[("vocabulary", "ZQXVTA")]["status"], "in_vocabulary")
    check("...but a person's decision is never overridden",
          (rows[("vocabulary", "ZQXVTB")]["status"], rows[("vocabulary", "ZQXVTC")]["status"]),
          ("rejected", "accepted"))
    check("reported", stats["now_in_vocabulary"], 1)


def test_listing():
    e = fresh()
    mc.persist_patterns(result([(("Q", "O"), 5), (("", "I"), 1)], [("ZQXVTA", 3)]), engine=e, now=T0)
    ids = {(k[0], k[1]): r["id"] for k, r in rows_by_key(e).items()}
    check("default: undecided patterns, most evidence first",
          [r["count"] for r in mc.list_patterns(engine=e)], [5, 3, 1])
    check("kind filter", [r["kind"] for r in mc.list_patterns(kind="vocabulary", engine=e)], ["vocabulary"])
    check("min_count filter", [r["count"] for r in mc.list_patterns(min_count=3, engine=e)], [5, 3])
    mc.set_status(ids[("substitution", Q_TO_O)], "accepted", engine=e)
    check("decided patterns leave the default list", [r["count"] for r in mc.list_patterns(engine=e)], [3, 1])
    check("status filter", [r["count"] for r in mc.list_patterns(status="accepted", engine=e)], [5])
    check("status 'all'", len(mc.list_patterns(status="all", engine=e)), 3)


def test_booklet_spread_ranks_filters_and_follows_evidence():
    e = fresh()
    first = result([(("Q", "O"), 30), (("", "I"), 5), (("A", "B"), 7)], [("ZQXVTA", 4)],
                   sub_booklets={("Q", "O"): 1, ("", "I"): 3, ("A", "B"): 3}, name_booklets={"ZQXVTA": 2})
    mc.persist_patterns(first, engine=e, now=T0)
    rows = mc.list_patterns(engine=e)
    check("a pattern in more booklets outranks a bigger count from one booklet",
          [(r["booklets"], r["count"]) for r in rows], [(3, 7), (3, 5), (2, 4), (1, 30)])
    check("--min-booklets drops the single-booklet pattern",
          [r["count"] for r in mc.list_patterns(min_booklets=2, engine=e)], [7, 5, 4])
    check("...and combines with --min-count", [r["count"] for r in mc.list_patterns(min_booklets=3, min_count=6, engine=e)], [7])

    mc.persist_patterns(first, engine=e, now=T1)
    check("running it again leaves the spread as it was",
          [(r["booklets"], r["count"]) for r in mc.list_patterns(engine=e)], [(3, 7), (3, 5), (2, 4), (1, 30)])

    grown = result([(("Q", "O"), 33)], sub_booklets={("Q", "O"): 4})
    mc.persist_patterns(grown, engine=e, now=T1)
    top = mc.list_patterns(engine=e)[0]
    check("a pattern that spreads to more booklets moves to the top", (top["booklets"], top["count"]), (4, 33))

    row = rows_by_key(e)[("vocabulary", "ZQXVTA")]
    check("a pattern whose evidence is gone loses its spread as well as its count",
          (row["count"], row["booklets"]), (0, 0))
    check("...and is out of the default list", [r["kind"] for r in mc.list_patterns(kind="vocabulary", engine=e)], [])

    mc.persist_patterns(result([(("X", "Y"), 2)]), engine=e, now=T1)   # no booklet info at all
    check("a result without booklet information stores a spread of 0, not an error",
          rows_by_key(e)[("substitution", mc._substitution_key("X", "Y"))]["booklets"], 0)


def _add_job(conn, uploaded_by, university, year, filename):
    return conn.execute(db.jobs.insert().values(
        university=university, year=year, filenames=[filename], storage_prefix="p",
        uploaded_by=uploaded_by)).inserted_primary_key[0]


def _add_record(conn, job_id, index, original_name, current_name):
    conn.execute(db.records.insert().values(
        job_id=job_id, row_index=index, name=current_name,
        original_fields={"name": original_name, "faculty": "", "department": "", "course": "", "degree": "",
                         "grade": "", "distinction_notes": "", "year": "", "affiliated_institution": ""}))


def test_end_to_end_from_records():
    e = fresh()
    with e.begin() as conn:
        uid = conn.execute(db.users.insert().values(email="d@akowe.example", password_hash="x")).inserted_primary_key[0]
        first = _add_job(conn, uid, "U", "2020", "part1.pdf")
        later = _add_job(conn, uid, "  u ", "2020", "part2.pdf")   # the rest of the SAME booklet, typed a little differently
        other = _add_job(conn, uid, "V", "2021", "b.pdf")          # a different booklet
        _add_record(conn, first, 0, "QKAFOR Chidi", "OKAFOR Chidi")
        _add_record(conn, first, 1, "QKAFOR Emeka", "OKAFOR Emeka")
        _add_record(conn, first, 2, "ADEYEMI Zqxvtb", "ADEYEMI Zqxvta")
        _add_record(conn, first, 3, "BELLO Tunde", "BELLO Tunde")        # untouched: teaches nothing
        _add_record(conn, later, 10, "QKAFOR Ngozi", "OKAFOR Ngozi")
        _add_record(conn, other, 20, "QKAFOR Chidi", "OKAFOR Chidi")
    stats = mc.mine_and_save(engine=e, now=T0)
    rows = rows_by_key(e)
    q = rows[("substitution", Q_TO_O)]
    check("only edited rows are counted", stats["corrections"], 5)
    check("the recurring Q->O confusion is stored with its count", q["count"], 4)
    check("a second upload of the same booklet (its other pages) is still one booklet: 4 corrections, 2 booklets",
          q["booklets"], 2)
    check("its example is a token pair, not a full name", q["example"].startswith("QKAFOR ->"), True)
    check("an unrecognized confirmed name becomes a vocabulary candidate, seen in 1 booklet",
          (rows[("vocabulary", "ZQXVTA")]["count"], rows[("vocabulary", "ZQXVTA")]["booklets"]), (1, 1))
    before = len(rows)
    mc.mine_and_save(engine=e, now=T1)
    check("running it again adds nothing", len(rows_by_key(e)), before)

    with e.begin() as conn:   # a DES undoes one of the Q->O fixes in the first booklet
        conn.execute(update(db.records).where(db.records.c.row_index == 1).values(name="QKAFOR Emeka"))
    mc.mine_and_save(engine=e, now=T1)
    row = rows_by_key(e)[("substitution", Q_TO_O)]
    check("a reverted correction stops counting but the peak remembers it; the spread is unchanged",
          (row["count"], row["peak_count"], row["booklets"]), (3, 4, 2))

    with e.begin() as conn:   # ...and the only correction from the other booklet is undone too
        conn.execute(update(db.records).where(db.records.c.row_index == 20).values(name="QKAFOR Chidi"))
    mc.mine_and_save(engine=e, now=T1)
    row = rows_by_key(e)[("substitution", Q_TO_O)]
    check("when a booklet's evidence is gone the spread narrows to what remains", (row["count"], row["booklets"]), (2, 1))


def test_save_refuses_partial_runs():
    # main() talks to db.engine, the real database. Point that at a throwaway one for the
    # duration, so that if the guard under test ever breaks, the run it wrongly allows lands
    # in the throwaway and not in whatever database this machine has configured.
    real_engine, db.engine = db.engine, fresh()
    try:
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            check("--save with --job is refused before touching anything", mc.main(["--save", "--job", "1"]), 2)
            check("--save with --since is refused", mc.main(["--save", "--since", "2026-01-01"]), 2)
        with db.engine.begin() as conn:
            check("...and stored nothing", conn.execute(select(db.mined_patterns)).all(), [])
    finally:
        db.engine = real_engine


def main():
    for t in (test_first_save_stores_every_pattern, test_rerunning_changes_nothing,
              test_count_follows_evidence_and_peak_remembers, test_decisions_survive_reruns,
              test_vocabulary_graduation, test_listing, test_booklet_spread_ranks_filters_and_follows_evidence,
              test_end_to_end_from_records,
              test_save_refuses_partial_runs):
        t()
    if _failures:
        print("\n".join(_failures))
        print(f"{len(_failures)} FAILED")
        sys.exit(1)
    print("all mine_corrections tests passed")


if __name__ == "__main__":
    main()
