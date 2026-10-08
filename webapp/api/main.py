"""The HTTP API the Next.js review screen talks to. Deployed as its own
container, separate from worker.py - a slow/stuck OCR job must
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
from sqlalchemy import select, text, update

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pdf_extractor.parser import CANON_FIELDS  # noqa: E402

from . import config, db, page_export, security, storage  # noqa: E402

# The interactive API docs list every endpoint to anyone who can reach the server;
# useful on a laptop, not something to publish for a system holding student records.
_docs = {} if not config.is_production() else {"docs_url": None, "redoc_url": None, "openapi_url": None}
app = FastAPI(title="Akowe Extraction Review API", **_docs)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ALLOW_ORIGINS,   # the review screen's exact address(es); see config.py
    allow_methods=["*"], allow_headers=["*"],
)


@app.get("/health")
def health():
    """For a load balancer's health check. Unauthenticated and reveals nothing but
    whether the API can reach its database."""
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - any failure to reach the database means the same thing
        raise HTTPException(503, "database unreachable")
    return {"ok": True}


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


def _load_pages(conn, job_id: int):
    pages = [dict(p) for p in conn.execute(select(db.pages).where(db.pages.c.job_id == job_id)).mappings()]
    recs = conn.execute(select(db.records.c.id, db.records.c.page, db.records.c.page_id)
                         .where(db.records.c.job_id == job_id)).mappings().all()
    by_page: dict[int, list[dict]] = {}
    for r in recs:
        if r["page_id"] is not None:
            by_page.setdefault(r["page_id"], []).append(dict(r))
    return pages, by_page


@app.get("/jobs/{job_id}")
def get_job(job_id: int, user_id: int = Depends(security.current_user_id)):
    with db.engine.begin() as conn:
        job = conn.execute(select(db.jobs).where(db.jobs.c.id == job_id)).mappings().first()
        if not job:
            raise HTTPException(404, "Job not found")
        recs = conn.execute(select(db.records).where(db.records.c.job_id == job_id)
                             .order_by(db.records.c.row_index)).mappings().all()
        pages, by_page = _load_pages(conn, job_id)
    page_by_id = {p["id"]: p for p in pages}
    records = []
    for r in recs:
        page = page_by_id.get(r["page_id"]) or {}
        # The edge strip is what a reviewer checks an uncertain page number against;
        # None for a job processed before page images were kept.
        records.append(dict(r) | {"pageno_image": page.get("number_image_key")})
    return {"job": dict(job), "records": records}


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


def _plan_for_job(job_id: int):
    """(job, plan) for a job's page images, or an HTTP error saying why not."""
    with db.engine.begin() as conn:
        job = conn.execute(select(db.jobs).where(db.jobs.c.id == job_id)).mappings().first()
        if not job:
            raise HTTPException(404, "Job not found")
        pages, by_page = _load_pages(conn, job_id)
    if not pages:
        raise HTTPException(409, "This job was processed before page images were kept - upload it again to get them")
    try:
        return dict(job), page_export.plan_page_files(dict(job), pages, by_page, config.PAGE_IMAGE_NAME_PATTERN,
                                                      config.PAGE_IMAGE_SLUGS)
    except ValueError as e:
        raise HTTPException(500, str(e))


class PageNumberUpdate(BaseModel):
    page: str


@app.patch("/pages/{page_id}")
def set_page_number(page_id: int, body: PageNumberUpdate, user_id: int = Depends(security.current_user_id)):
    """Sets one page's printed number on every record on it at once. A page holds
    dozens to hundreds of names and its number is a fact about the page, so
    resolving an uncertain one row by row would be absurd - this is the single
    action a reviewer takes for it. The page images are filed under the number
    on the records, so this is also what unblocks the download."""
    value = page_export.normalize_page_number(body.page)
    if value is None:
        raise HTTPException(422, "A page number is 1 to 3 digits, for example 45")
    with db.engine.begin() as conn:
        if not conn.execute(select(db.pages.c.id).where(db.pages.c.id == page_id)).first():
            raise HTTPException(404, "Page not found")
        updated = conn.execute(update(db.records).where(db.records.c.page_id == page_id).values(
            page=value, updated_by=user_id, updated_at=datetime.now(timezone.utc))).rowcount
        conn.execute(update(db.pages).where(db.pages.c.id == page_id).values(printed_page=value))
    return {"ok": True, "records": updated, "page": value}


@app.get("/jobs/{job_id}/page-images/status")
def page_images_status(job_id: int, user_id: int = Depends(security.current_user_id)):
    """Whether this job's page images can be downloaded yet, and the pages in the
    way if not. Read from the records' current page numbers every time, so it
    changes the moment a reviewer fixes one. "available" is False for a job
    processed before page images were kept."""
    with db.engine.begin() as conn:
        has_pages = conn.execute(select(db.pages.c.id).where(db.pages.c.job_id == job_id).limit(1)).first()
    if not has_pages:
        return {"available": False, "pages": 0, "ready": False, "problems": []}
    _, plan = _plan_for_job(job_id)
    return {"available": True, **page_export.export_status(plan)}


@app.get("/jobs/{job_id}/page-images")
def export_page_images(job_id: int, request: Request, token: Optional[str] = Query(default=None)):
    """Every page that has names on it, as its own PNG named by printed page
    number, in one zip (plus manifest.csv) - what report assembly pulls from.
    Refused (409) until every page has exactly one confirmed number: a report
    built from a zip with unnumbered pages, or two pages competing for one name,
    would silently pull the wrong page."""
    _auth_header_or_query(request, token)
    job, plan = _plan_for_job(job_id)
    status = page_export.export_status(plan)
    if not status["ready"]:
        n = len(status["problems"])
        raise HTTPException(409, f"{n} page{'s' if n != 1 else ''} still need{'' if n != 1 else 's'} a confirmed "
                                 f"page number before the images can be downloaded - e.g. "
                                 f"{status['problems'][0]['where']}: {status['problems'][0]['detail']}")
    try:
        archive = page_export.build_zip(plan, storage.get_storage().get_bytes)
    except page_export.MissingPageImage as e:
        raise HTTPException(500, str(e))
    size = archive.seek(0, 2)
    archive.seek(0)

    def chunks():
        try:
            while block := archive.read(1024 * 1024):
                yield block
        finally:
            archive.close()

    safe = page_export.safe_filename(f"{page_export.slug_for(job['university'], config.PAGE_IMAGE_SLUGS)}"
                                     f"-{page_export.year_for(job['year'])}-pages")
    return StreamingResponse(chunks(), media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{safe}.zip"', "Content-Length": str(size)})


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
