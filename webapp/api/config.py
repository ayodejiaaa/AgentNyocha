"""Settings, all overridable by environment variable so the same code runs
unchanged in local dev (SQLite + local-disk storage, both zero-setup) and in
production (Postgres + an S3 bucket, set via environment variables - see
webapp/DEPLOY_AWS.md). Nothing here should ever hold a real secret - those come
from the deploy environment, not this file.
"""

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOCAL_DATA_DIR = BASE_DIR / "_local_data"  # dev-only: uploaded PDFs, generated images, sqlite db

DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{LOCAL_DATA_DIR / 'app.db'}")

# "local": files live under LOCAL_DATA_DIR/files, served back through the API.
# "s3": an S3-compatible bucket (Cloudflare R2, AWS S3, ...) - set STORAGE_BACKEND=s3
# plus S3_BUCKET / S3_ENDPOINT_URL / S3_ACCESS_KEY / S3_SECRET_KEY.
STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "local")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "")
# Blank means "let boto3 work it out" (AWS_REGION / the instance's own region). A
# Cloudflare R2 bucket needs the literal "auto".
S3_REGION = os.environ.get("S3_REGION", "")

# Files are never served from a public URL - see storage.py and main.py's
# /files route - because these are real people's academic records. This key
# only signs the short-lived API auth token; it does not gate file access on
# its own. MUST be set in any real deployment - see _check_production_settings,
# which refuses to start without it.
_DEV_JWT_SECRET = "dev-only-insecure-secret-change-me"
JWT_SECRET = os.environ.get("JWT_SECRET", _DEV_JWT_SECRET)
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.environ.get("JWT_EXPIRE_HOURS", "12"))

# How often the worker polls for newly queued jobs. Polling, not a message
# queue, is a deliberate simplification at this volume (well under 200
# jobs/month) - a message broker would be another service to run and pay
# for, for no real benefit at this scale.
WORKER_POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS", "5"))

ZOOM = float(os.environ.get("EXTRACTION_ZOOM", "2.0"))

# Resolution (PyMuPDF zoom, 2.0 = 144 dpi) of the full-page images saved for each
# page - the ones exported for report assembly. Independent of EXTRACTION_ZOOM,
# which only governs how well the page is OCR'd. Higher is sharper and larger:
# at 2.0 a page is roughly 1,200 x 1,700 px.
PAGE_IMAGE_ZOOM = float(os.environ.get("PAGE_IMAGE_ZOOM", "2.0"))

# What the exported page images are called inside the zip (always PNG). Fields:
# {slug} the university's short name as it appears in Akowe's existing page image
# files (covenant-university, unibadan, futa ... - see page_export.UNIVERSITY_SLUGS),
# {year} the four-digit year, {page} the printed page number, {university} the
# university exactly as typed at upload. The default gives e.g.
# "covenant-university-2022-28.png".
PAGE_IMAGE_NAME_PATTERN = os.environ.get("PAGE_IMAGE_NAME_PATTERN", "{slug}-{year}-{page}")

# Extra or corrected university short names, as JSON: {"University Name": "slug"}.
# Added on top of page_export.UNIVERSITY_SLUGS, matched ignoring case and punctuation.
PAGE_IMAGE_SLUGS = json.loads(os.environ.get("PAGE_IMAGE_SLUGS", "{}"))

# The exact address(es) of the review screen, comma-separated
# ("https://review.example.com"). Only these websites may call the API from a
# browser. Left unset, only the local dev screen (localhost:3000) is allowed.
_LOCAL_DEV_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]
CORS_ALLOW_ORIGINS = ([o.strip().rstrip("/") for o in os.environ.get("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()]
                      or _LOCAL_DEV_ORIGINS)

# A job whose worker has not been heard from for this long is taken to have died
# (crash, out-of-memory kill, the box restarting) and goes back in the queue. The
# worker reports in every WORKER_HEARTBEAT_SECONDS while it works, so this can be
# short without ever re-running a job that is merely slow. After MAX_JOB_ATTEMPTS
# tries it is marked failed instead, so a booklet that kills the worker every time
# can't keep restarting it.
WORKER_HEARTBEAT_SECONDS = float(os.environ.get("WORKER_HEARTBEAT_SECONDS", "30"))
STALE_JOB_SECONDS = float(os.environ.get("STALE_JOB_SECONDS", "600"))
MAX_JOB_ATTEMPTS = int(os.environ.get("MAX_JOB_ATTEMPTS", "3"))

LOCAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
(LOCAL_DATA_DIR / "files").mkdir(parents=True, exist_ok=True)


def is_production() -> bool:
    """True as soon as anything points away from the zero-setup local dev setup: a
    real database, a bucket, or APP_ENV=production. Local dev sets none of them."""
    return (bool(os.environ.get("DATABASE_URL")) or STORAGE_BACKEND != "local"
            or os.environ.get("APP_ENV", "").lower() == "production")


def production_problems() -> list[str]:
    """Everything wrong with the settings for a real deployment, one sentence each
    (empty if fine). A deployment that would otherwise run with a publicly known
    signing key, or let any website talk to the API, must not start at all."""
    problems = []
    if JWT_SECRET == _DEV_JWT_SECRET or len(JWT_SECRET) < 32:
        problems.append("JWT_SECRET must be set to a random string of at least 32 characters "
                        "(for example: openssl rand -hex 32)")
    if not os.environ.get("CORS_ALLOW_ORIGINS", "").strip():
        problems.append("CORS_ALLOW_ORIGINS must be set to the review screen's exact address, "
                        "e.g. https://review.example.com")
    elif "*" in CORS_ALLOW_ORIGINS:
        problems.append("CORS_ALLOW_ORIGINS must not be * - name the review screen's exact address")
    if STORAGE_BACKEND == "s3" and not S3_BUCKET:
        problems.append("S3_BUCKET must be set when STORAGE_BACKEND=s3")
    if STORAGE_BACKEND not in ("local", "s3"):
        problems.append(f"STORAGE_BACKEND must be 'local' or 's3', not {STORAGE_BACKEND!r}")
    if not os.environ.get("DATABASE_URL"):
        problems.append("DATABASE_URL must be set (a postgresql+psycopg2://... address); without it "
                        "the app would use a throwaway SQLite file")
    return problems


if is_production() and (_problems := production_problems()):
    raise RuntimeError("Refusing to start - the deployment settings are not safe: " + "; ".join(_problems))
