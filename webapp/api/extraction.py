"""Turns one queued job into review rows in the database. This is
deliberately a thin adapter, not a reimplementation - it materializes the
job's uploaded files into a plain temp folder and hands that straight to
pdf_extractor.batch.process_folder, exactly the function the CLI tools use,
so a booklet split across several PDF parts gets the same file-order
stitching and carried-forward header state here as it does run from the
command line. Everything downstream (confidence scoring, snapshot
cropping) is review.py, unmodified.
"""

import shutil
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, for `import pdf_extractor`
from pdf_extractor import batch, review  # noqa: E402
from pdf_extractor.parser import CANON_FIELDS  # noqa: E402

from . import config, db, storage  # noqa: E402


def process_job(job_id: int):
    st = storage.get_storage()
    with db.engine.begin() as conn:
        job = conn.execute(select(db.jobs).where(db.jobs.c.id == job_id)).mappings().first()
    if job is None:
        return

    tmp_dir = Path(tempfile.mkdtemp(prefix=f"job{job_id}_"))
    try:
        for filename in job["filenames"]:
            data = st.get_bytes(f"{job['storage_prefix']}/source/{filename}")
            (tmp_dir / filename).write_bytes(data)

        records = batch.process_folder(str(tmp_dir), zoom=config.ZOOM, verbose=False)
        rows, flagged = review.build_review_rows(records)

        if flagged:
            images_dir = tmp_dir / "_images"
            paths = review.generate_snapshots(flagged, images_dir)
            for idx, rel_path in paths.items():
                local_file = tmp_dir / rel_path
                key = f"{job['storage_prefix']}/images/{Path(rel_path).name}"
                st.put(key, str(local_file))
                rows[idx]["review_image"] = key

        counts = {"high": 0, "medium": 0, "low": 0}
        db_rows = []
        for i, row in enumerate(rows):
            counts[row["confidence"]] = counts.get(row["confidence"], 0) + 1
            original = {f: row.get(f, "") for f in CANON_FIELDS}
            db_rows.append({
                "job_id": job_id, "row_index": i, **original,
                "original_fields": original,
                "confidence": row["confidence"], "flag_reason": row["flag_reason"],
                "review_image": row["review_image"] or None, "reviewed": row["confidence"] == "high",
            })

        with db.engine.begin() as conn:
            if db_rows:
                conn.execute(db.records.insert(), db_rows)
            conn.execute(
                update(db.jobs).where(db.jobs.c.id == job_id).values(
                    status="ready", processed_at=datetime.now(timezone.utc),
                    record_count=len(rows), high_count=counts["high"],
                    medium_count=counts["medium"], low_count=counts["low"],
                )
            )
    except Exception as e:  # noqa: BLE001 - a bad job must surface, not vanish or crash the worker loop
        err = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-2000:]}"
        with db.engine.begin() as conn:
            conn.execute(update(db.jobs).where(db.jobs.c.id == job_id).values(status="failed", error_message=err))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def export_clean_rows(job_id: int) -> list[dict]:
    """The canonical-columns-only handoff for the CTO's team - current
    (possibly DES-corrected) field values, not the original extraction."""
    with db.engine.begin() as conn:
        rows = conn.execute(
            select(db.records).where(db.records.c.job_id == job_id).order_by(db.records.c.row_index)
        ).mappings().all()
    return [{f: r[f] for f in CANON_FIELDS} for r in rows]
