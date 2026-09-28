"""Schema and engine. SQLAlchemy Core (not the ORM) - the tables are simple
and this keeps every query visible as plain SQL-shaped Python rather than
hidden behind model classes, which matters here since the worker, the API,
and the corrections-mining script all touch these tables independently.

Runs unchanged against SQLite (local dev, zero setup) or Postgres
(production - see config.py) - nothing below uses a Postgres-only feature.
"""

from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, Column, DateTime, ForeignKey, Integer,
                         MetaData, String, Table, Text, UniqueConstraint, create_engine)

from . import config

metadata = MetaData()


def _now():
    return datetime.now(timezone.utc)


users = Table(
    "users", metadata,
    Column("id", Integer, primary_key=True),
    Column("email", String, unique=True, nullable=False),
    Column("password_hash", String, nullable=False),
    Column("full_name", String, nullable=False, default=""),
    Column("is_admin", Boolean, nullable=False, default=False),
    Column("created_at", DateTime, default=_now),
)

# One job = one booklet a DES uploaded (one PDF, or several parts of the
# same split booklet uploaded together - see api/extraction.py, which
# reuses pdf_extractor.batch.process_folder's exact multi-file stitching
# logic rather than re-deriving it).
jobs = Table(
    "jobs", metadata,
    Column("id", Integer, primary_key=True),
    Column("university", String, nullable=False),
    Column("year", String, nullable=False),
    Column("filenames", JSON, nullable=False),          # original uploaded filenames, in upload order
    Column("storage_prefix", String, nullable=False),    # where this job's files/images live in storage
    # queued -> processing -> ready -> (failed on error, staying visible with error_message set)
    Column("status", String, nullable=False, default="queued"),
    Column("error_message", Text, nullable=True),
    Column("uploaded_by", Integer, ForeignKey("users.id"), nullable=False),
    Column("uploaded_at", DateTime, default=_now),
    Column("processed_at", DateTime, nullable=True),
    Column("record_count", Integer, nullable=False, default=0),
    Column("high_count", Integer, nullable=False, default=0),
    Column("medium_count", Integer, nullable=False, default=0),
    Column("low_count", Integer, nullable=False, default=0),
)

# One row per extracted name. "original_fields" is a frozen snapshot of
# what the tool first produced (the canonical columns only) - everything
# else on the row (name, faculty, ...) is what a DES sees and edits
# directly. That split is what lets corrections be found later with a
# plain query (current != original) instead of diffing two CSV files - see
# api/mine_corrections.py.
records = Table(
    "records", metadata,
    Column("id", Integer, primary_key=True),
    Column("job_id", Integer, ForeignKey("jobs.id"), nullable=False),
    Column("row_index", Integer, nullable=False),
    Column("name", Text, nullable=False, default=""),
    Column("faculty", Text, nullable=False, default=""),
    Column("department", Text, nullable=False, default=""),
    Column("course", Text, nullable=False, default=""),
    Column("degree", Text, nullable=False, default=""),
    Column("grade", Text, nullable=False, default=""),
    Column("distinction_notes", Text, nullable=False, default=""),
    Column("year", Text, nullable=False, default=""),
    Column("year_note", Text, nullable=False, default=""),
    Column("page", Text, nullable=False, default=""),
    Column("programme", Text, nullable=False, default=""),
    Column("affiliated_institution", Text, nullable=False, default=""),
    Column("country", Text, nullable=False, default=""),
    Column("original_fields", JSON, nullable=False),     # {field: original_value, ...}, canonical fields only
    Column("confidence", String, nullable=False, default="high"),
    Column("flag_reason", String, nullable=False, default=""),
    Column("review_image", String, nullable=True),       # storage key, not a public URL - see api/main.py's /files route
    Column("reviewed", Boolean, nullable=False, default=False),
    Column("updated_by", Integer, ForeignKey("users.id"), nullable=True),
    Column("updated_at", DateTime, nullable=True),
)

# What correction-mining has learned so far, kept across runs so the list of
# candidates has a memory: a person decides once whether a pattern is worth
# folding into the tool, and it stays decided. One row per pattern -
#   kind "substitution": key is JSON ["Q", "O"] (OCR read Q where O was meant;
#                        "" on either side for a dropped/added character)
#   kind "vocabulary":   key is a name token DES confirmed that the tool's
#                        name vocabulary didn't know
# "count" is the evidence right now, recomputed from the records' current
# corrections on every run (so re-running never double counts, and a
# correction a DES later reverts stops counting); "peak_count" is the most it
# has ever been, so evidence isn't lost if the source records are purged.
# "booklets" is how many different booklets (distinct university + year) the
# corrections came from - the guard against one bad scan looking like a
# pattern: 30 corrections in 1 booklet is weaker evidence than 5 across 3.
# "status" is candidate -> accepted / rejected by a person, or in_vocabulary
# when a token later appeared in the vocabulary by itself. Nothing here is
# ever applied to the tool automatically.
mined_patterns = Table(
    "mined_patterns", metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", String, nullable=False),
    Column("key", String, nullable=False),
    Column("count", Integer, nullable=False, default=0),
    Column("peak_count", Integer, nullable=False, default=0),
    Column("booklets", Integer, nullable=False, default=0),
    Column("example", String, nullable=False, default=""),   # one token pair, never a full name
    Column("status", String, nullable=False, default="candidate"),
    Column("decided_by", Integer, ForeignKey("users.id"), nullable=True),
    Column("decided_at", DateTime, nullable=True),
    Column("first_seen", DateTime, default=_now),
    Column("last_seen", DateTime, default=_now),
    UniqueConstraint("kind", "key", name="uq_mined_patterns_kind_key"),
)

engine = create_engine(config.DATABASE_URL, connect_args=(
    {"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {}
))


def init_db():
    metadata.create_all(engine)
