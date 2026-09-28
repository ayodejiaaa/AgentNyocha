"""The persistent background process - this is the piece that cannot run
on Vercel (see extraction.py's docstring: real OCR, several minutes per
booklet). Deploy this as its own always-on service (a second Railway
service from the same repo, `python -m webapp.api.worker`); the FastAPI
app in main.py never does extraction work inline on a request.

Polling a status column, not a message queue: at well under 200 jobs a
month a broker is another paid service bought for no real benefit here.
"""

import sys
import time
from pathlib import Path

from sqlalchemy import select, update

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from . import config, db, extraction  # noqa: E402


def run_forever():
    db.init_db()
    print(f"worker started, polling every {config.WORKER_POLL_SECONDS}s", flush=True)
    while True:
        job_id = _claim_next_job()
        if job_id is None:
            time.sleep(config.WORKER_POLL_SECONDS)
            continue
        print(f"processing job {job_id}", flush=True)
        extraction.process_job(job_id)
        print(f"finished job {job_id}", flush=True)


def _claim_next_job() -> int | None:
    """Atomically flips the oldest queued job to 'processing' so a second
    worker instance (if one is ever run for throughput) never double-picks
    the same job."""
    with db.engine.begin() as conn:
        row = conn.execute(
            select(db.jobs.c.id).where(db.jobs.c.status == "queued")
            .order_by(db.jobs.c.uploaded_at).limit(1)
        ).first()
        if row is None:
            return None
        job_id = row[0]
        conn.execute(update(db.jobs).where(db.jobs.c.id == job_id).values(status="processing"))
    return job_id


if __name__ == "__main__":
    run_forever()
