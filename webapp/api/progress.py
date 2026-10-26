"""How far along a job is, written to the jobs table for the queue to show.

A job's life, as a percentage:
    0-2      fetching the uploaded files
    2-78     reading the booklet (batch.process_folder, which reports its own adaptive fraction)
    78-99    what the web app does afterwards: snapshot images, page images, saving them to
             storage, writing the rows to the database. The size of each of those is known by
             then (how many rows were flagged, how many pages), so each gets its fair share of
             this range - see post_plan.
    100      ready (set together with the status, never earlier)

The number never goes backwards, and it is written at most every couple of seconds: it is
for a person to look at, not a log, and a database write per snapshot would be wasteful.
Writing it must never be able to fail a job, so errors are swallowed.
"""

import time

from sqlalchemy import update

from . import db

EXTRACT_START, EXTRACT_END, POST_END = 2.0, 78.0, 99.0

# Relative cost, in seconds on the machine they were measured on (12-page slices of two real
# booklets), of each later step. Only the proportions between them matter.
_SNAPSHOT_PER_ROW = 0.045
_PAGE_IMAGE_PER_PAGE = 1.0
_SAVE_PER_FILE_LOCAL, _SAVE_PER_FILE_S3 = 0.005, 0.05
_DB_BASE, _DB_PER_ROW = 0.5, 0.0003


class JobProgress:
    def __init__(self, job_id: int, min_interval: float = 1.5, clock=time.monotonic):
        self.job_id = job_id
        self.min_interval = min_interval
        self.clock = clock
        self.percent = 0          # the highest value asked for so far
        self.note = ""
        self._written = None      # (percent, note) last stored
        self._last_write = None

    def set(self, percent: float, note: str, force: bool = False) -> None:
        percent = int(min(POST_END, max(float(percent), 0.0)))
        percent = max(percent, self.percent)          # never backwards
        self.percent, self.note = percent, note
        if self._written == (percent, note):
            return
        now = self.clock()
        if not force and self._last_write is not None and now - self._last_write < self.min_interval:
            return                                     # a later call will carry the newest value
        self._write(percent, note, now)

    def _write(self, percent: int, note: str, now: float) -> None:
        try:
            with db.engine.begin() as conn:
                conn.execute(update(db.jobs).where(db.jobs.c.id == self.job_id, db.jobs.c.status == "processing")
                             .values(progress=percent, progress_note=note))
            self._written, self._last_write = (percent, note), now
        except Exception:  # noqa: BLE001 - a missed update is harmless; failing the job over it would not be
            pass


def post_plan(flagged_rows: int, pages: int, total_rows: int, remote_storage: bool) -> dict[str, float]:
    """Relative size of each step after the reading, in the order they run."""
    save = _SAVE_PER_FILE_S3 if remote_storage else _SAVE_PER_FILE_LOCAL
    return {
        "snapshots": _SNAPSHOT_PER_ROW * flagged_rows,
        "save_snapshots": save * flagged_rows,
        "page_images": _PAGE_IMAGE_PER_PAGE * pages,
        "save_page_images": save * 2 * pages,
        "database": _DB_BASE + _DB_PER_ROW * total_rows,
    }


class PostSteps:
    """Divides the 78-99% range between the later steps in proportion to their weights, and hands
    out one callback(done, total) per step."""

    def __init__(self, progress: JobProgress, weights: dict[str, float], start: float = EXTRACT_END, end: float = POST_END):
        self.progress = progress
        total = sum(weights.values()) or 1.0
        self.ranges: dict[str, tuple[float, float]] = {}
        at = start
        for name, weight in weights.items():
            self.ranges[name] = (at, at + (end - start) * weight / total)
            at = self.ranges[name][1]

    def callback(self, name: str, label: str):
        lo, hi = self.ranges[name]

        def report(done: int, total: int) -> None:
            fraction = done / total if total else 1.0
            self.progress.set(lo + (hi - lo) * fraction, f"{label} ({done} of {total})", force=done >= total)
        return report

    def at(self, name: str, label: str) -> None:
        """The start of a step that has no count of its own."""
        self.progress.set(self.ranges[name][0], label, force=True)
