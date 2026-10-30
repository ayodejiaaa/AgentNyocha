"""Schema and engine. SQLAlchemy Core (not the ORM) - the tables are simple
and this keeps every query visible as plain SQL-shaped Python rather than
hidden behind model classes, which matters here since the worker, the API,
and the corrections-mining script all touch these tables independently.

Runs unchanged against SQLite (local dev, zero setup) or Postgres
(production - see config.py) - nothing below uses a Postgres-only feature.
"""

from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, Column, DateTime, ForeignKey, Integer,
                         MetaData, String, Table, Text, UniqueConstraint, create_engine, inspect, text)

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
    # uploading (files still being stored) -> queued -> processing -> ready
    # -> (failed on error, staying visible with error_message set)
    Column("status", String, nullable=False, default="queued"),
    Column("error_message", Text, nullable=True),
    Column("uploaded_by", Integer, ForeignKey("users.id"), nullable=False),
    Column("uploaded_at", DateTime, default=_now),
    Column("processed_at", DateTime, nullable=True),
    Column("record_count", Integer, nullable=False, default=0),
    Column("high_count", Integer, nullable=False, default=0),
    Column("medium_count", Integer, nullable=False, default=0),
    Column("low_count", Integer, nullable=False, default=0),
    # Set while a worker has the job: when it last reported in, and how many times
    # a worker has started on it (see worker.py - a job whose worker went silent is
    # put back in the queue, up to config.MAX_JOB_ATTEMPTS times).
    Column("heartbeat_at", DateTime, nullable=True),
    # How far along a "processing" job is (0-100) and what it is doing right now
    # ("Reading page 12 of 80"). Written by the worker, shown in the queue.
    Column("progress", Integer, nullable=False, default=0, server_default=text("0")),
    Column("progress_note", String, nullable=True),
    Column("attempts", Integer, nullable=False, default=0, server_default=text("0")),   # DB-side default too: an older version of the code, rolled back to, can still insert jobs
)

# One row per physical page of the booklet that has names on it (a spread
# counts as two). Holds the page's two images - the whole corrected page, which
# is what a client's report pulls in, and the strip of its top and bottom edges
# a reviewer checks the printed number against - plus what extraction made of
# the printed page number. The number that actually counts for the report is the
# one on the page's records (a DES can correct it there); "printed_page" is only
# what the tool first read, and is "" when it couldn't.
pages = Table(
    "pages", metadata,
    Column("id", Integer, primary_key=True),
    Column("job_id", Integer, ForeignKey("jobs.id"), nullable=False),
    Column("source_file", String, nullable=False, default=""),
    Column("file_order", Integer, nullable=False, default=0),    # which uploaded file, in the order it was read
    Column("page_index", Integer, nullable=False),               # physical page within that file, from 0
    Column("spread_half", Integer, nullable=True),               # 0/1 for the halves of a two-page scan, else NULL
    Column("printed_page", String, nullable=False, default=""),
    Column("page_status", String, nullable=False, default=""),   # detected / interpolated / uncertain
    Column("image_key", String, nullable=True),                  # storage key of the full page PNG
    Column("number_image_key", String, nullable=True),           # storage key of the edge-strip PNG
    # Set when the scan shows another page's number too (it runs into the
    # neighbouring page): "two_numbers" / "neighbour_number", the numbers seen as
    # JSON ([{"side": "left", "value": 32}, ...]), and the number the booklet's
    # sequence points to - a prefilled answer for the reviewer, never applied.
    Column("page_note", String, nullable=True),
    Column("seen_numbers", Text, nullable=True),
    Column("suggested_page", String, nullable=True),
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
    # A row taken out by a reviewer (a leftover fragment of a name that wrapped onto another line, a
    # stray line...). Hidden, not deleted: it is left out of the export, the page-number checks and the
    # corrections report, and can be put back. updated_by / updated_at say who and when.
    Column("removed", Boolean, nullable=False, default=False, server_default=text("false")),
    Column("page_id", Integer, ForeignKey("pages.id"), nullable=True),   # NULL for jobs processed before pages existed
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


# Columns added to a table after a database may already have been created from
# an older version of this file. create_all() only ever adds whole missing
# tables, never a column on an existing one, so each is added here if absent.
_ADDED_COLUMNS = [("records", "removed", "BOOLEAN NOT NULL DEFAULT FALSE"),
                  ("jobs", "progress", "INTEGER NOT NULL DEFAULT 0"), ("jobs", "progress_note", "VARCHAR"),
                  ("jobs", "heartbeat_at", "TIMESTAMP"), ("jobs", "attempts", "INTEGER NOT NULL DEFAULT 0"),
                  ("records", "page_id", "INTEGER"),
                  ("pages", "page_note", "VARCHAR"), ("pages", "seen_numbers", "TEXT"),
                  ("pages", "suggested_page", "VARCHAR")]


def init_db():
    """Creates what is missing and adds any column an older database lacks. The API
    and the worker both call this as they start - on a fresh Postgres, together - so
    on Postgres the whole thing runs under one advisory lock: the second to arrive
    waits, then finds nothing left to do, instead of failing on a table or column
    the first has just made."""
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(727350)"))   # released at commit
        metadata.create_all(conn)
        existing = inspect(conn)
        for table, column, sql_type in _ADDED_COLUMNS:
            if column not in {c["name"] for c in existing.get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
