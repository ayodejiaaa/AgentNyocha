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


def result(subs=(), names=(), examples=None):
    return {"substitutions": list(subs), "new_vocabulary": list(names), "substitution_examples": examples or {}}


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


def _add_record(conn, job_id, index, original_name, current_name):
    conn.execute(db.records.insert().values(
        job_id=job_id, row_index=index, name=current_name,
        original_fields={"name": original_name, "faculty": "", "department": "", "course": "", "degree": "",
                         "grade": "", "distinction_notes": "", "year": "", "affiliated_institution": ""}))


def test_end_to_end_from_records():
    e = fresh()
    with e.begin() as conn:
        uid = conn.execute(db.users.insert().values(email="d@akowe.example", password_hash="x")).inserted_primary_key[0]
        jid = conn.execute(db.jobs.insert().values(
            university="U", year="2020", filenames=["a.pdf"], storage_prefix="p", uploaded_by=uid)).inserted_primary_key[0]
        _add_record(conn, jid, 0, "QKAFOR Chidi", "OKAFOR Chidi")
        _add_record(conn, jid, 1, "QKAFOR Emeka", "OKAFOR Emeka")
        _add_record(conn, jid, 2, "ADEYEMI Zqxvtb", "ADEYEMI Zqxvta")
        _add_record(conn, jid, 3, "BELLO Tunde", "BELLO Tunde")        # untouched: teaches nothing
    stats = mc.mine_and_save(engine=e, now=T0)
    rows = rows_by_key(e)
    check("only edited rows are counted", stats["corrections"], 3)
    check("the recurring Q->O confusion is stored with its count", rows[("substitution", Q_TO_O)]["count"], 2)
    check("its example is a token pair, not a full name", rows[("substitution", Q_TO_O)]["example"].startswith("QKAFOR ->"), True)
    check("an unrecognized confirmed name becomes a vocabulary candidate", rows[("vocabulary", "ZQXVTA")]["count"], 1)
    before = len(rows)
    mc.mine_and_save(engine=e, now=T1)
    check("running it again adds nothing", len(rows_by_key(e)), before)

    with e.begin() as conn:   # a DES undoes one of the two Q->O fixes
        conn.execute(update(db.records).where(db.records.c.row_index == 1).values(name="QKAFOR Emeka"))
    mc.mine_and_save(engine=e, now=T1)
    row = rows_by_key(e)[("substitution", Q_TO_O)]
    check("a reverted correction stops counting but the peak remembers it", (row["count"], row["peak_count"]), (1, 2))


def test_save_refuses_partial_runs():
    with contextlib.redirect_stderr(io.StringIO()):   # the refusal message is expected output here
        check("--save with --job is refused before touching anything", mc.main(["--save", "--job", "1"]), 2)
        check("--save with --since is refused", mc.main(["--save", "--since", "2026-01-01"]), 2)


def main():
    for t in (test_first_save_stores_every_pattern, test_rerunning_changes_nothing,
              test_count_follows_evidence_and_peak_remembers, test_decisions_survive_reruns,
              test_vocabulary_graduation, test_listing, test_end_to_end_from_records,
              test_save_refuses_partial_runs):
        t()
    if _failures:
        print("\n".join(_failures))
        print(f"{len(_failures)} FAILED")
        sys.exit(1)
    print("all mine_corrections tests passed")


if __name__ == "__main__":
    main()
