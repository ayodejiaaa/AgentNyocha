"""The persistent background process - this is the piece that cannot run
on Vercel (see extraction.py's docstring: real OCR, several minutes per
booklet). Deploy this as its own always-on service (a second container from
the same image: `python -m webapp.api.worker`); the FastAPI app in main.py
never does extraction work inline on a request.

Polling a status column, not a message queue: at well under 200 jobs a
month a broker is another paid service bought for no real benefit here.

Safe to run more than one worker, and safe to kill one mid-job:
  - a job is claimed with a single conditional UPDATE, so two workers can never
    take the same one;
  - while a worker has a job it reports in every WORKER_HEARTBEAT_SECONDS;
  - a "processing" job whose worker has been silent for STALE_JOB_SECONDS (the
    process was killed, ran out of memory, the box restarted) goes back in the
    queue - and is marked failed instead after MAX_JOB_ATTEMPTS tries, so a booklet
    that kills the worker every time cannot keep restarting it.
Nothing is written to the review tables until a job has been processed in full,
so a job that died partway leaves nothing half-done behind.
"""

import sys
import threading
import time
import traceback
from datetime import timedelta
from pathlib import Path

from sqlalchemy import or_, select, update

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from . import config, db, extraction  # noqa: E402


def run_forever():
    db.init_db()
    print(f"worker started, polling every {config.WORKER_POLL_SECONDS}s", flush=True)
    while True:
        try:
            for job_id, outcome in requeue_stale_jobs():
                print(f"job {job_id} had no worker for over {config.STALE_JOB_SECONDS:.0f}s - {outcome}", flush=True)
            job_id = claim_next_job()
            if job_id is None:
                time.sleep(config.WORKER_POLL_SECONDS)
                continue
            print(f"processing job {job_id}", flush=True)
            with Heartbeat(job_id):
                extraction.process_job(job_id, require_claim=True)
            print(f"finished job {job_id}", flush=True)
        except Exception:  # noqa: BLE001 - a database blip or restart must not kill the worker; the next pass retries
            print("worker loop error (will retry):\n" + traceback.format_exc(), flush=True)
            time.sleep(config.WORKER_POLL_SECONDS)


def claim_next_job() -> int | None:
    """Takes the oldest queued job for this worker, or None if there isn't one.
    The UPDATE only matches while the job is still queued, so if another worker
    got there first it changes nothing (rowcount 0) and we look at the next."""
    for _ in range(5):
        with db.engine.begin() as conn:
            row = conn.execute(select(db.jobs.c.id).where(db.jobs.c.status == "queued")
                               .order_by(db.jobs.c.uploaded_at, db.jobs.c.id).limit(1)).first()
            if row is None:
                return None
            if _try_claim(conn, row[0]):
                return row[0]
    return None


def _try_claim(conn, job_id: int) -> bool:
    """One conditional UPDATE: matches only while the job is still queued, so of
    two workers going for the same job exactly one gets rowcount 1."""
    return bool(conn.execute(
        update(db.jobs).where(db.jobs.c.id == job_id, db.jobs.c.status == "queued")
        .values(status="processing", heartbeat_at=db._now(), attempts=db.jobs.c.attempts + 1,
                 progress=0, progress_note="Starting")).rowcount)


def requeue_stale_jobs(now=None) -> list[tuple[int, str]]:
    """Puts back in the queue (or fails, past MAX_JOB_ATTEMPTS) every job that is
    "processing" but whose worker has gone silent. Returns [(job_id, what was
    done)]. Conditional UPDATEs, so several workers sweeping at once can't both
    act on one job."""
    cutoff = (now or db._now()) - timedelta(seconds=config.STALE_JOB_SECONDS)
    silent = or_(db.jobs.c.heartbeat_at.is_(None), db.jobs.c.heartbeat_at < cutoff)
    done = []
    with db.engine.begin() as conn:
        stale = conn.execute(select(db.jobs.c.id, db.jobs.c.attempts)
                             .where(db.jobs.c.status == "processing", silent)).all()
        for job_id, attempts in stale:
            if attempts >= config.MAX_JOB_ATTEMPTS:
                values = {"status": "failed", "error_message": (
                    f"Processing stopped {attempts} times without finishing (the worker was killed or ran out of "
                    f"memory - the booklet may be too large for the server). Not retried again.")}
                outcome = f"failed after {attempts} attempts"
            else:
                values = {"status": "queued", "heartbeat_at": None, "progress": 0, "progress_note": None}
                outcome = f"put back in the queue (attempt {attempts} of {config.MAX_JOB_ATTEMPTS} used)"
            changed = conn.execute(update(db.jobs).where(db.jobs.c.id == job_id, db.jobs.c.status == "processing", silent)
                                   .values(**values)).rowcount
            if changed:
                done.append((job_id, outcome))
    return done


class Heartbeat:
    """Reports in on a job every WORKER_HEARTBEAT_SECONDS while the block runs, on
    its own thread, because processing blocks for minutes at a time."""

    def __init__(self, job_id: int, every: float | None = None):
        self.job_id = job_id
        self.every = config.WORKER_HEARTBEAT_SECONDS if every is None else every
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.wait(self.every):
            try:
                with db.engine.begin() as conn:
                    conn.execute(update(db.jobs).where(db.jobs.c.id == self.job_id, db.jobs.c.status == "processing")
                                 .values(heartbeat_at=db._now()))
            except Exception as e:  # noqa: BLE001 - one missed beat is harmless; stopping the thread would not be
                print(f"heartbeat for job {self.job_id} failed: {type(e).__name__}", flush=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)


if __name__ == "__main__":
    run_forever()
