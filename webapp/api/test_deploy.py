"""Tests for what a real deployment depends on: the settings that must be safe
before the app will start, the worker's job queue (safe with several workers, and
with one killed mid-job), the health check, S3 credentials, and the database
upgrade. Everything runs against throwaway databases and folders, never the real
ones.

    python -m webapp.api.test_deploy
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, select, text, update

from pdf_extractor import batch, review

from . import check_deploy, config, db, extraction, main as api, storage, worker

_failures = []
REPO_ROOT = Path(__file__).resolve().parents[2]
GOOD_SECRET = "x" * 40


def check(label, got, want):
    if got != want:
        _failures.append(f"FAIL {label}: got {got!r}, want {want!r}")


# --- settings --------------------------------------------------------------------

_SETTING_NAMES = ("DATABASE_URL", "JWT_SECRET", "CORS_ALLOW_ORIGINS", "STORAGE_BACKEND", "S3_BUCKET", "APP_ENV")


def load_config(**env) -> tuple[bool, str]:
    """Imports config in a fresh process with exactly these settings. (started?, what it said)"""
    clean = {k: v for k, v in os.environ.items() if k not in _SETTING_NAMES}
    r = subprocess.run([sys.executable, "-c", "from webapp.api import config; print(config.CORS_ALLOW_ORIGINS)"],
                       cwd=REPO_ROOT, env={**clean, **env}, capture_output=True, text=True, timeout=60)
    return r.returncode == 0, (r.stdout + r.stderr)


GOOD = {"DATABASE_URL": "postgresql+psycopg2://u:p@db/x", "JWT_SECRET": GOOD_SECRET,
        "CORS_ALLOW_ORIGINS": "https://review.example.com"}


def test_settings():
    ok, out = load_config()
    check("local dev with nothing set starts", ok, True)
    check("...allowing only the local dev screen", "http://localhost:3000" in out and "*" not in out, True)

    ok, out = load_config(**GOOD)
    check("a complete deployment starts", ok, True)
    check("...allowing exactly the review screen", "['https://review.example.com']" in out, True)
    ok, out = load_config(**{**GOOD, "CORS_ALLOW_ORIGINS": " https://a.example.com/ , https://b.example.com "})
    check("several origins, with stray spaces and a trailing slash", "['https://a.example.com', 'https://b.example.com']" in out, True)

    ok, out = load_config(DATABASE_URL=GOOD["DATABASE_URL"])
    check("a database address alone is not enough", ok, False)
    check("...it names the missing secret and origin", "JWT_SECRET" in out and "CORS_ALLOW_ORIGINS" in out, True)
    ok, out = load_config(**{**GOOD, "JWT_SECRET": "dev-only-insecure-secret-change-me"})
    check("the built-in development secret is refused", (ok, "JWT_SECRET" in out), (False, True))
    ok, out = load_config(**{**GOOD, "JWT_SECRET": "short"})
    check("a short secret is refused", (ok, "JWT_SECRET" in out), (False, True))
    ok, out = load_config(**{**GOOD, "CORS_ALLOW_ORIGINS": "*"})
    check("any-website CORS is refused", (ok, "must not be *" in out), (False, True))
    ok, out = load_config(**{**GOOD, "CORS_ALLOW_ORIGINS": "https://a.example.com,*"})
    check("...even among others", ok, False)
    ok, out = load_config(**{k: v for k, v in GOOD.items() if k != "CORS_ALLOW_ORIGINS"})
    check("no CORS setting in a deployment is refused", (ok, "CORS_ALLOW_ORIGINS" in out), (False, True))
    ok, out = load_config(**GOOD, STORAGE_BACKEND="s3")
    check("s3 storage without a bucket is refused", (ok, "S3_BUCKET" in out), (False, True))
    ok, out = load_config(**GOOD, STORAGE_BACKEND="s3", S3_BUCKET="b")
    check("s3 storage with a bucket starts", ok, True)
    ok, out = load_config(**GOOD, STORAGE_BACKEND="bogus")
    check("an unknown storage backend is refused", (ok, "STORAGE_BACKEND" in out), (False, True))
    ok, out = load_config(JWT_SECRET=GOOD_SECRET, CORS_ALLOW_ORIGINS=GOOD["CORS_ALLOW_ORIGINS"], APP_ENV="production")
    check("production mode without a database address is refused", (ok, "DATABASE_URL" in out), (False, True))
    ok, out = load_config(STORAGE_BACKEND="s3", S3_BUCKET="b")
    check("a bucket alone makes it a deployment, and the rest is then demanded", (ok, "JWT_SECRET" in out), (False, True))


def test_api_docs_are_off_in_production():
    def docs_url(**env):
        clean = {k: v for k, v in os.environ.items() if k not in _SETTING_NAMES}
        r = subprocess.run([sys.executable, "-c", "from webapp.api import main; print(main.app.docs_url, main.app.openapi_url)"],
                           cwd=REPO_ROOT, env={**clean, **env}, capture_output=True, text=True, timeout=120)
        return r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-300:]

    tmp = Path(tempfile.mkdtemp())
    try:
        # sqlite://... still counts as "a database address was set", so this is production mode
        prod = {**GOOD, "DATABASE_URL": f"sqlite:///{tmp / 'p.db'}"}
        check("production serves no API docs pages", docs_url(**prod), "None None")
        check("local dev still has them", docs_url(), "/docs /openapi.json")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_settings_the_rest_of_the_app_reads_exist():
    names = ("PAGE_IMAGE_ZOOM", "PAGE_IMAGE_NAME_PATTERN", "PAGE_IMAGE_SLUGS", "ZOOM", "WORKER_POLL_SECONDS", "JWT_ALGORITHM",
             "JWT_EXPIRE_HOURS", "DATABASE_URL", "STORAGE_BACKEND", "S3_BUCKET", "S3_REGION", "CORS_ALLOW_ORIGINS",
             "WORKER_HEARTBEAT_SECONDS", "STALE_JOB_SECONDS", "MAX_JOB_ATTEMPTS", "LOCAL_DATA_DIR")
    check("every setting the app reads is defined", [n for n in names if not hasattr(config, n)], [])


def test_deploy_check():
    with _Sandbox():
        results = check_deploy.run_checks()
    by = {}
    for status, label, _ in results:
        by.setdefault(label, []).append(status)
    check("the self-check passes what works here", [by[k] for k in ("database tables", "database upgrades applied",
          "database write and read", "storage (local) write, read, delete", "Tesseract", "PDF rendering and OpenCV")],
          [["OK"]] * 6)
    check("...and fails the unsafe development settings", by["settings"], ["FAIL"] * 3)
    check("...and warns that SQLite and local disk are not for a deployment",
          (by["database reachable"], "WARN" in by["storage"]), (["WARN"], True))


def test_cors_is_wired_to_the_setting():
    cors = [m for m in api.app.user_middleware if m.cls is CORSMiddleware]
    check("the API uses the configured origins, not a wildcard",
          (len(cors), cors and cors[0].kwargs["allow_origins"]), (1, config.CORS_ALLOW_ORIGINS))
    check("...and the default for local dev is not a wildcard", "*" in config.CORS_ALLOW_ORIGINS, False)


# --- throwaway database ----------------------------------------------------------

class _Sandbox:
    def __enter__(self):
        self.dir = Path(tempfile.mkdtemp())
        self.real = (db.engine, config.LOCAL_DATA_DIR)
        db.engine = create_engine(f"sqlite:///{self.dir / 't.db'}", connect_args={"check_same_thread": False})
        config.LOCAL_DATA_DIR = self.dir
        (self.dir / "files").mkdir()
        db.init_db()
        with db.engine.begin() as conn:
            conn.execute(db.users.insert().values(id=1, email="d@akowe.example", password_hash="x"))
        return self

    def __exit__(self, *exc):
        db.engine.dispose()
        db.engine, config.LOCAL_DATA_DIR = self.real
        shutil.rmtree(self.dir, ignore_errors=True)


def add_job(job_id, status="queued", uploaded_offset=0, heartbeat_ago=None, attempts=0, filenames=()):
    now = db._now()
    with db.engine.begin() as conn:
        conn.execute(db.jobs.insert().values(
            id=job_id, university="LASU", year="2019", filenames=list(filenames), storage_prefix=f"jobs/{job_id}",
            uploaded_by=1, status=status, uploaded_at=now + timedelta(seconds=uploaded_offset),
            heartbeat_at=None if heartbeat_ago is None else now - timedelta(seconds=heartbeat_ago), attempts=attempts))


def job(job_id):
    with db.engine.begin() as conn:
        return dict(conn.execute(select(db.jobs).where(db.jobs.c.id == job_id)).mappings().one())


def test_health():
    with _Sandbox():
        check("health says ok when the database answers", api.health(), {"ok": True})
        real = db.engine
        db.engine = create_engine("sqlite:////nonexistent_dir/none.db")
        try:
            try:
                api.health()
                got = None
            except HTTPException as e:
                got = e.status_code
        finally:
            db.engine = real
        check("...and 503 when it doesn't", got, 503)


# --- the worker's queue -----------------------------------------------------------

def test_claiming():
    with _Sandbox():
        add_job(1, uploaded_offset=10)
        add_job(2, uploaded_offset=0)           # older, so first
        add_job(3, status="ready")
        first = worker.claim_next_job()
        check("the oldest queued job is taken first", first, 2)
        j = job(2)
        check("...and is marked processing, counted as an attempt, with a heartbeat",
              (j["status"], j["attempts"], j["heartbeat_at"] is not None), ("processing", 1, True))
        check("the next worker gets the next job, never the same one", worker.claim_next_job(), 1)
        check("nothing queued -> None (ready jobs are not touched)", worker.claim_next_job(), None)
        check("a finished job is left alone", job(3)["status"], "ready")

        add_job(4)
        with db.engine.begin() as conn:
            first_worker = worker._try_claim(conn, 4)
        with db.engine.begin() as conn:
            second_worker = worker._try_claim(conn, 4)
        check("of two workers going for one job exactly one wins", (first_worker, second_worker), (True, False))
        check("...and it is only counted once", job(4)["attempts"], 1)


def test_requeue_stale():
    with _Sandbox():
        stale = config.STALE_JOB_SECONDS + 5
        add_job(1, status="processing", heartbeat_ago=stale, attempts=1)
        add_job(2, status="processing", heartbeat_ago=1, attempts=1)                       # worker is alive
        add_job(3, status="processing", heartbeat_ago=stale, attempts=config.MAX_JOB_ATTEMPTS)
        add_job(4, status="processing", heartbeat_ago=None, attempts=1)                    # no heartbeat ever
        add_job(5, status="queued")
        add_job(6, status="ready", heartbeat_ago=stale)
        add_job(7, status="failed", heartbeat_ago=stale)
        got = dict(worker.requeue_stale_jobs())
        check("silent jobs are handled and live ones are not", sorted(got), [1, 3, 4])
        check("a silent job goes back in the queue", (job(1)["status"], job(1)["heartbeat_at"]), ("queued", None))
        check("a job with a live worker is left alone", job(2)["status"], "processing")
        check("a job that has used up its attempts is failed, not retried",
              (job(3)["status"], "3 times" in (job(3)["error_message"] or "")), ("failed", True))
        check("a job that never reported in is treated as silent", job(4)["status"], "queued")
        check("queued, ready and failed jobs are not touched", [job(5)["status"], job(6)["status"], job(7)["status"]],
              ["queued", "ready", "failed"])
        check("a second sweep finds nothing more", worker.requeue_stale_jobs(), [])
        check("a requeued job is picked up again, as a further attempt",
              (worker.claim_next_job(), job(1)["attempts"]), (1, 2))


def test_heartbeat():
    with _Sandbox():
        add_job(1, status="processing", heartbeat_ago=500, attempts=1)
        before = job(1)["heartbeat_at"]
        with worker.Heartbeat(1, every=0.05):
            time.sleep(0.4)
        after = job(1)["heartbeat_at"]
        check("a working job's heartbeat keeps moving forward", after > before, True)
        add_job(2, status="ready", heartbeat_ago=500)
        before = job(2)["heartbeat_at"]
        with worker.Heartbeat(2, every=0.05):
            time.sleep(0.3)
        check("a job that is no longer processing is not touched", job(2)["heartbeat_at"], before)
        add_job(3, status="processing", heartbeat_ago=0, attempts=1)
        t = time.time()
        with worker.Heartbeat(3, every=60):
            pass
        check("leaving the block stops the heartbeat promptly", time.time() - t < 3, True)


def test_a_reassigned_job_is_not_written_twice():
    real = (batch.process_folder, review.build_review_rows)
    batch.process_folder = lambda *a, **k: []
    review.build_review_rows = lambda records: ([], [])
    try:
        with _Sandbox():
            add_job(1, status="queued")                      # was put back in the queue while this copy still ran
            extraction.process_job(1, require_claim=True)
            check("a worker whose job was re-queued writes nothing", job(1)["status"], "queued")
            add_job(2, status="processing", heartbeat_ago=0, attempts=1)
            extraction.process_job(2, require_claim=True)
            check("a worker that still holds its job finishes it", job(2)["status"], "ready")
            add_job(3, status="queued")
            extraction.process_job(3)
            check("a direct call (no claim) behaves as before", job(3)["status"], "ready")
    finally:
        batch.process_folder, review.build_review_rows = real


# --- storage and database ---------------------------------------------------------

def test_s3_credentials():
    captured = {}

    class FakeBoto3:
        @staticmethod
        def client(name, **kwargs):
            captured.update(kwargs)
            return object()

    real_boto = sys.modules.get("boto3")
    sys.modules["boto3"] = FakeBoto3
    real = (config.S3_ACCESS_KEY, config.S3_SECRET_KEY, config.S3_REGION, config.S3_ENDPOINT_URL)
    try:
        config.S3_ACCESS_KEY = config.S3_SECRET_KEY = config.S3_REGION = config.S3_ENDPOINT_URL = ""
        storage.S3Storage()
        check("blank keys and region are passed as None, so the server's IAM role is used",
              (captured["aws_access_key_id"], captured["aws_secret_access_key"], captured["region_name"],
               captured["endpoint_url"]), (None, None, None, None))
        config.S3_ACCESS_KEY, config.S3_SECRET_KEY, config.S3_REGION = "AK", "SK", "eu-west-1"
        storage.S3Storage()
        check("explicit keys and region are used as given",
              (captured["aws_access_key_id"], captured["aws_secret_access_key"], captured["region_name"]),
              ("AK", "SK", "eu-west-1"))
    finally:
        config.S3_ACCESS_KEY, config.S3_SECRET_KEY, config.S3_REGION, config.S3_ENDPOINT_URL = real
        if real_boto is None:
            sys.modules.pop("boto3", None)
        else:
            sys.modules["boto3"] = real_boto


def test_local_storage_delete():
    d = Path(tempfile.mkdtemp())
    try:
        st = storage.LocalStorage(d)
        st.put_bytes("a/b.txt", b"hi")
        st.delete("a/b.txt")
        st.delete("a/b.txt")   # already gone: not an error
        check("a stored file can be deleted, and deleting twice is harmless", (d / "a" / "b.txt").exists(), False)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_older_code_can_still_insert_jobs():
    """Rolling back to a version that doesn't know the attempts column must not break uploads."""
    with _Sandbox():
        with db.engine.begin() as conn:
            conn.execute(text("INSERT INTO jobs (university, year, filenames, storage_prefix, status, uploaded_by, record_count, "
                              "high_count, medium_count, low_count) VALUES ('X', '1', '[]', 'p', 'queued', 1, 0, 0, 0, 0)"))
            check("a job inserted without the new columns gets attempts 0",
                  conn.execute(text("SELECT attempts FROM jobs")).scalar(), 0)


def test_database_without_job_heartbeat_is_upgraded():
    tmp = Path(tempfile.mkdtemp())
    real = db.engine
    try:
        db.engine = create_engine(f"sqlite:///{tmp / 'old.db'}")
        with db.engine.begin() as conn:
            conn.execute(text("CREATE TABLE jobs (id INTEGER PRIMARY KEY, university TEXT NOT NULL, year TEXT NOT NULL, "
                              "filenames JSON NOT NULL, storage_prefix TEXT NOT NULL, status TEXT NOT NULL, "
                              "error_message TEXT, uploaded_by INTEGER NOT NULL, uploaded_at TIMESTAMP, "
                              "processed_at TIMESTAMP, record_count INTEGER NOT NULL, high_count INTEGER NOT NULL, "
                              "medium_count INTEGER NOT NULL, low_count INTEGER NOT NULL)"))
            conn.execute(text("INSERT INTO jobs (id, university, year, filenames, storage_prefix, status, uploaded_by, "
                              "record_count, high_count, medium_count, low_count) "
                              "VALUES (9, 'OAU', '2019', '[]', 'jobs/9', 'processing', 1, 0, 0, 0, 0)"))
        db.init_db()
        db.init_db()
        with db.engine.begin() as conn:
            row = conn.execute(text("SELECT id, status, attempts, heartbeat_at FROM jobs")).one()
        check("an older database gains the job columns, keeping its jobs", tuple(row), (9, "processing", 0, None))
        check("...and a job left 'processing' by the old version is then recovered, not stuck",
              [(j, o.split(" (")[0]) for j, o in worker.requeue_stale_jobs()], [(9, "put back in the queue")])
    finally:
        db.engine.dispose()
        db.engine = real
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    for t in (test_settings, test_api_docs_are_off_in_production, test_settings_the_rest_of_the_app_reads_exist, test_deploy_check, test_cors_is_wired_to_the_setting, test_health, test_claiming, test_requeue_stale,
              test_heartbeat, test_a_reassigned_job_is_not_written_twice, test_s3_credentials,
              test_local_storage_delete, test_older_code_can_still_insert_jobs, test_database_without_job_heartbeat_is_upgraded):
        t()
    if _failures:
        print("\n".join(_failures))
        print(f"{len(_failures)} FAILED")
        sys.exit(1)
    print("all deploy tests passed")


if __name__ == "__main__":
    main()
