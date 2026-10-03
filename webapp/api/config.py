"""Settings, all overridable by environment variable so the same code runs
unchanged in local dev (SQLite + local-disk storage, both zero-setup) and in
production (Postgres on Neon + object storage on R2/S3, set via env vars on
Railway/Vercel). Nothing here should ever hold a real secret - those come
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
S3_REGION = os.environ.get("S3_REGION", "auto")

# Files are never served from a public URL - see storage.py and main.py's
# /files route - because these are real people's academic records. This key
# only signs the short-lived API auth token; it does not gate file access on
# its own. MUST be overridden in any real deployment (Railway env var).
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-only-insecure-secret-change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.environ.get("JWT_EXPIRE_HOURS", "12"))

# How often the worker polls for newly queued jobs. Polling, not a message
# queue, is a deliberate simplification at this volume (well under 200
# jobs/month) - a message broker would be another service to run and pay
# for, for no real benefit at this scale.
WORKER_POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS", "5"))

ZOOM = float(os.environ.get("EXTRACTION_ZOOM", "2.0"))

LOCAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
(LOCAL_DATA_DIR / "files").mkdir(parents=True, exist_ok=True)

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
