"""The HTTP API the Next.js frontend (on Vercel) talks to. Deployed as its
own Railway service, separate from worker.py - a slow/stuck OCR job must
never be able to make the API (login, the review screen) unresponsive.
This process never does extraction work itself; POST /jobs only stores the
upload and queues a row for worker.py to pick up.
"""

import csv
import io
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, update

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pdf_extractor.parser import CANON_FIELDS  # noqa: E402

from . import config, db, security, storage  # noqa: E402

app = FastAPI(title="Akowe Extraction Review API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to the deployed frontend's exact origin once that URL is known
    allow_methods=["*"], allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    db.init_db()


# ---- auth -------------------------------------------------------------

class LoginBody(BaseModel):
    email: str
    password: str


@app.post("/auth/login")
def login(body: LoginBody):
    with db.engine.begin() as conn:
        user = conn.execute(select(db.users).where(db.users.c.email == body.email.lower())).mappings().first()
    if not user or not security.verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "Wrong email or password")
    return {"token": security.create_token(user["id"], user["email"]), "full_name": user["full_name"]}


# ---- jobs ---------------------------------------------------------------

@app.get("/jobs")
def list_jobs(user_id: int = Depends(security.current_user_id)):
    with db.engine.begin() as conn:
        rows = conn.execute(select(db.jobs).order_by(db.jobs.c.uploaded_at.desc())).mappings().all()
        uploader_ids = {r["uploaded_by"] for r in rows}
        uploaders = {}
        if uploader_ids:
            u_rows = conn.execute(select(db.users.c.id, db.users.c.full_name)
                                   .where(db.users.c.id.in_(uploader_ids))).all()
            uploaders = dict(u_rows)
    return [dict(r) | {"uploaded_by_name": uploaders.get(r["uploaded_by"], "?")} for r in rows]


@app.post("/jobs")
async def create_job(university: str, year: str, files: list[UploadFile],
                      user_id: int = Depends(security.current_user_id)):
    if not files:
        raise HTTPException(400, "At least one PDF is required")
    st = storage.get_storage()
    with db.engine.begin() as conn:
        job_id = conn.execute(db.jobs.insert().values(
            university=university, year=year, filenames=[f.filename for f in files],
            storage_prefix="jobs/pending", status="queued", uploaded_by=user_id,
        )).inserted_primary_key[0]
        # storage_prefix embeds the real job id once we have one
        conn.execute(update(db.jobs).where(db.jobs.c.id == job_id)
                     .values(storage_prefix=f"jobs/{job_id}"))
    for f in files:
        st.put_bytes(f"jobs/{job_id}/source/{f.filename}", await f.read())
    return {"id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}")
def get_job(job_id: int, user_id: int = Depends(security.current_user_id)):
    with db.engine.begin() as conn:
        job = conn.execute(select(db.jobs).where(db.jobs.c.id == job_id)).mappings().first()
        if not job:
            raise HTTPException(404, "Job not found")
        recs = conn.execute(select(db.records).where(db.records.c.job_id == job_id)
                             .order_by(db.records.c.row_index)).mappings().all()
    return {"job": dict(job), "records": [dict(r) for r in recs]}


def _auth_header_or_query(request: Request, token: Optional[str]) -> int:
    """Some browser navigations (a plain <a href> download link, an <img
    src>) can't attach an Authorization header - see /files and /export,
    both reachable either way: a normal API call with the header, or a
    direct link/tag with ?token=."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return security.decode_token(auth[7:])
    if token:
        return security.decode_token(token)
    raise HTTPException(401, "Missing or invalid token")


@app.get("/jobs/{job_id}/export")
def export_job(job_id: int, request: Request, token: Optional[str] = Query(default=None)):
    _auth_header_or_query(request, token)
    with db.engine.begin() as conn:
        job = conn.execute(select(db.jobs).where(db.jobs.c.id == job_id)).mappings().first()
        if not job:
            raise HTTPException(404, "Job not found")
        recs = conn.execute(select(db.records).where(db.records.c.job_id == job_id)
                             .order_by(db.records.c.row_index)).mappings().all()
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CANON_FIELDS)
    writer.writeheader()
    for r in recs:
        writer.writerow({f: r[f] for f in CANON_FIELDS})
    buf.seek(0)
    filename = f"{job['university']}_{job['year']}_clean.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                              headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---- records (DES corrections) ------------------------------------------

# Every canonical field is DES-editable, including "page" - it was excluded
# here originally on the assumption it was the tool's own bookkeeping, which
# turned out to be wrong: Akowe's existing ground truth has a page number on
# essentially every one of its 535,288 rows (verified directly against it -
# 2 blank rows total), because report assembly matches a client's requested
# pages by this exact number against a same-numbered PNG. A record flagged
# "page_number_uncertain" (pages.resolve_page_numbers found no confirmed
# reading) is exactly the case a DES needs to be able to fix here.
_EDITABLE_FIELDS = set(CANON_FIELDS)


class RecordUpdate(BaseModel):
    name: Optional[str] = None
    faculty: Optional[str] = None
    department: Optional[str] = None
    course: Optional[str] = None
    degree: Optional[str] = None
    grade: Optional[str] = None
    distinction_notes: Optional[str] = None
    year: Optional[str] = None
    year_note: Optional[str] = None
    page: Optional[str] = None
    programme: Optional[str] = None
    affiliated_institution: Optional[str] = None
    country: Optional[str] = None
    reviewed: Optional[bool] = None


@app.patch("/records/{record_id}")
def update_record(record_id: int, body: RecordUpdate, user_id: int = Depends(security.current_user_id)):
    values = {k: v for k, v in body.model_dump(exclude_unset=True).items()
              if k == "reviewed" or (k in _EDITABLE_FIELDS and v is not None)}
    if not values:
        raise HTTPException(400, "Nothing to update")
    values["updated_by"] = user_id
    values["updated_at"] = datetime.now(timezone.utc)
    with db.engine.begin() as conn:
        result = conn.execute(update(db.records).where(db.records.c.id == record_id).values(**values))
        if result.rowcount == 0:
            raise HTTPException(404, "Record not found")
    return {"ok": True}


# ---- files (snapshot images, proxied - never a public URL) --------------

@app.get("/files/{key:path}")
def get_file(key: str, request: Request, token: Optional[str] = Query(default=None)):
    _auth_header_or_query(request, token)
    st = storage.get_storage()
    data = st.get_bytes(key)
    media_type = "application/pdf" if key.endswith(".pdf") else "image/png"
    return StreamingResponse(io.BytesIO(data), media_type=media_type)
