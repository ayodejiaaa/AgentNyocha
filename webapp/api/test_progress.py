"""Tests for the processing percentage: what the worker writes to a job as it goes, and that
it never lies (never backwards, never 100 before ready, never fails a job). Throwaway
database only.

    python -m webapp.api.test_progress
"""

import sys

from sqlalchemy import select

from pdf_extractor import batch, review

from . import db, extraction, progress, worker
from .test_deploy import _Sandbox, add_job, job

_failures = []


def check(label, got, want):
    if got != want:
        _failures.append(f"FAIL {label}: got {got!r}, want {want!r}")


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def shown(job_id):
    j = job(job_id)
    return j["progress"], j["progress_note"]


def test_writer():
    with _Sandbox():
        add_job(1, status="processing", heartbeat_ago=0, attempts=1)
        clock = Clock()
        p = progress.JobProgress(1, min_interval=1.5, clock=clock)
        p.set(10, "Reading page 1 of 20")
        check("the first update is written at once", shown(1), (10, "Reading page 1 of 20"))
        clock.t += 0.2
        p.set(12, "Reading page 2 of 20")
        check("a second one straight after is held back", shown(1), (10, "Reading page 1 of 20"))
        clock.t += 2.0
        p.set(14, "Reading page 3 of 20")
        check("...and the next one after a pause carries the newest value", shown(1), (14, "Reading page 3 of 20"))
        clock.t += 0.1
        p.set(20, "Done reading", force=True)
        check("a forced update ignores the pause", shown(1), (20, "Done reading"))
        p.set(5, "late event from an earlier stage")
        check("the number never goes backwards", p.percent, 20)
        clock.t += 5
        p.set(5, "late event from an earlier stage")
        check("...even when it is allowed to write", shown(1)[0], 20)
        clock.t += 5
        p.set(150, "way over")
        check("it never shows 100 or more before the job is ready", shown(1)[0], 99)
        p.set(-3, "x")
        check("a negative value changes nothing", shown(1)[0], 99)


def test_writer_leaves_other_jobs_alone_and_never_raises():
    with _Sandbox():
        add_job(1, status="ready")
        add_job(2, status="processing", heartbeat_ago=0, attempts=1)
        p = progress.JobProgress(1, clock=Clock())
        p.set(40, "late write after the job finished", force=True)
        check("a job that is no longer processing is not touched", shown(1), (0, None))
        broken = progress.JobProgress(2, clock=Clock())
        real = db.engine
        db.engine = None   # every write now fails
        try:
            try:
                broken.set(30, "x", force=True)
                got = "carried on"
            except Exception:
                got = "raised"
        finally:
            db.engine = real
        check("a failing database write never raises into the job", got, "carried on")
        broken.set(31, "y", force=True)
        check("...and the next write goes through", shown(2), (31, "y"))


def test_post_plan_and_steps():
    plan = progress.post_plan(flagged_rows=1000, pages=10, total_rows=1500, remote_storage=False)
    check("the steps run in this order", list(plan), ["snapshots", "save_snapshots", "page_images", "save_page_images", "database"])
    check("many flagged rows make snapshots the big step", plan["snapshots"] > plan["page_images"], True)
    few = progress.post_plan(flagged_rows=10, pages=10, total_rows=1500, remote_storage=False)
    check("few flagged rows make page images the big step", few["page_images"] > few["snapshots"], True)
    check("saving to S3 costs more than to local disk",
          progress.post_plan(100, 10, 100, True)["save_snapshots"] > progress.post_plan(100, 10, 100, False)["save_snapshots"], True)
    check("nothing flagged costs nothing for snapshots", progress.post_plan(0, 10, 100, False)["snapshots"], 0)

    with _Sandbox():
        add_job(1, status="processing", heartbeat_ago=0, attempts=1)
        p = progress.JobProgress(1, clock=Clock())
        steps = progress.PostSteps(p, {"a": 1.0, "b": 3.0})
        check("the steps share 78-99 in proportion to their size",
              [(round(lo, 2), round(hi, 2)) for lo, hi in steps.ranges.values()], [(78.0, 83.25), (83.25, 99.0)])
        steps.callback("a", "Creating things")(5, 10)
        check("half way through the first step", shown(1), (80, "Creating things (5 of 10)"))
        steps.callback("b", "Saving things")(10, 10)
        check("finishing the last step reaches the top of the range, not 100", shown(1)[0], 99)
        empty = progress.PostSteps(p, {"a": 0.0, "b": 0.0})
        empty.callback("a", "Nothing to do")(0, 0)   # zero total must not divide by zero


def test_claim_and_requeue_reset_progress():
    with _Sandbox():
        add_job(1, status="queued")
        worker.claim_next_job()
        check("a job just taken by a worker starts at 0", shown(1), (0, "Starting"))
        p = progress.JobProgress(1, clock=Clock())
        p.set(55, "Reading page 11 of 20", force=True)
        with db.engine.begin() as conn:
            from sqlalchemy import update
            conn.execute(update(db.jobs).where(db.jobs.c.id == 1).values(heartbeat_at=db._now() - __import__("datetime").timedelta(seconds=99999)))
        worker.requeue_stale_jobs()
        check("a job put back in the queue starts again from 0", (job(1)["status"], shown(1)), ("queued", (0, None)))


def test_whole_job_reports_and_ends_at_100():
    """process_job with the heavy steps replaced by fast stand-ins that report like the real ones."""
    real = (batch.process_folder, review.build_review_rows, review.generate_snapshots, review.generate_page_images)
    seen = []

    class Recorder(progress.JobProgress):
        def set(self, percent, note, force=False):
            super().set(percent, note, force=True)   # record every step, unthrottled
            seen.append((self.percent, note))

    def fake_folder(folder, zoom=2.0, verbose=True, on_progress=None):
        for i in range(1, 5):
            on_progress(i / 4, f"Reading page {i} of 4")
        return []

    def fake_snaps(flagged, images_dir, on_progress=None):
        return {}

    batch.process_folder = fake_folder
    review.build_review_rows = lambda records: ([], [])
    review.generate_snapshots = fake_snaps
    review.generate_page_images = lambda records, d, zoom=2.0, on_progress=None: {}
    real_cls = progress.JobProgress
    progress.JobProgress = Recorder
    try:
        with _Sandbox():
            add_job(1, status="processing", heartbeat_ago=0, attempts=1)
            extraction.process_job(1, require_claim=True)
            done = job(1)
    finally:
        progress.JobProgress = real_cls
        batch.process_folder, review.build_review_rows, review.generate_snapshots, review.generate_page_images = real
    percents = [p for p, _ in seen]
    check("the percentage never goes backwards through a whole job", all(b >= a for a, b in zip(percents, percents[1:])), True)
    check("it starts low and passes through the reading range", (percents[0] <= 2, any(10 < p < 78 for p in percents)), (True, True))
    check("it never claims 100 while still working", max(percents) <= 99, True)
    check("the notes say what is happening", [n for _, n in seen if n.startswith("Reading")][-1], "Reading page 4 of 4")
    check("the last step announced is saving the results", seen[-1][1], "Saving the results")
    check("a finished job is at 100 with no leftover note", (done["status"], done["progress"], done["progress_note"]), ("ready", 100, None))


def test_a_failed_job_keeps_where_it_got_to():
    real = batch.process_folder

    def boom(folder, zoom=2.0, verbose=True, on_progress=None):
        on_progress(0.5, "Reading page 2 of 4")
        raise RuntimeError("tesseract fell over")

    batch.process_folder = boom
    try:
        with _Sandbox():
            add_job(1, status="processing", heartbeat_ago=0, attempts=1)
            extraction.process_job(1, require_claim=True)
            j = job(1)
    finally:
        batch.process_folder = real
    check("a failed job is failed, not 100%", (j["status"], j["progress"] < 100), ("failed", True))


def main():
    for t in (test_writer, test_writer_leaves_other_jobs_alone_and_never_raises, test_post_plan_and_steps,
              test_claim_and_requeue_reset_progress, test_whole_job_reports_and_ends_at_100,
              test_a_failed_job_keeps_where_it_got_to):
        t()
    if _failures:
        print("\n".join(_failures))
        print(f"{len(_failures)} FAILED")
        sys.exit(1)
    print("all progress tests passed")


if __name__ == "__main__":
    main()
