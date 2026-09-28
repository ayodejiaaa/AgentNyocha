"""Settings, all overridable by environment variable so the same code runs
unchanged in local dev (SQLite + local-disk storage, both zero-setup) and in
production (Postgres on Neon + object storage on R2/S3, set via env vars on
Railway/Vercel). Nothing here should ever hold a real secret - those come
from the deploy environment, not this file.
"""

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
