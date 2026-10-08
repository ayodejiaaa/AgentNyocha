"""Checks a deployment is set up properly, on the real machine, with the real
settings. Run it inside the API container after deploying, and again after any
change to the environment:

    docker compose exec api python -m webapp.api.check_deploy

Each line is OK, WARN (works, but look at it) or FAIL (will break). Exit status is
1 if anything FAILed. It writes one small test file to storage and removes it, and
touches the database only inside a transaction it rolls back - it leaves nothing
behind and never reads anyone's records.
"""

import sys
import uuid
from pathlib import Path

from sqlalchemy import func, inspect, select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from . import config, db, storage  # noqa: E402

EXPECTED_TABLES = {"users", "jobs", "records", "pages", "mined_patterns"}


def run_checks() -> list[tuple[str, str, str]]:
    """[(status, what was checked, detail)]"""
    out: list[tuple[str, str, str]] = []

    def ok(label, detail=""):
        out.append(("OK", label, detail))

    def warn(label, detail):
        out.append(("WARN", label, detail))

    def fail(label, detail):
        out.append(("FAIL", label, detail))

    # settings
    problems = config.production_problems()
    for p in problems:
        fail("settings", p)
    if not problems:
        ok("settings", f"JWT secret set, CORS limited to {', '.join(config.CORS_ALLOW_ORIGINS)}")
    if config.JWT_EXPIRE_HOURS > 12:
        warn("login lifetime", f"JWT_EXPIRE_HOURS is {config.JWT_EXPIRE_HOURS}; downloads carry the login token in the "
                               "web address, so keep it short")

    # database
    try:
        dialect = db.engine.dialect.name
        with db.engine.connect() as conn:
            version = conn.execute(text("SELECT version()")).scalar() if dialect == "postgresql" else dialect
        (ok if dialect == "postgresql" else warn)(
            "database reachable", str(version).split(",")[0] if dialect == "postgresql"
            else "this is SQLite - fine for a laptop, not for a deployment")
        db.init_db()
        tables = set(inspect(db.engine).get_table_names())
        missing = EXPECTED_TABLES - tables
        (fail if missing else ok)("database tables", f"missing: {sorted(missing)}" if missing else "all present")
        columns = {c["name"] for c in inspect(db.engine).get_columns("jobs")}
        gap = {"heartbeat_at", "attempts"} - columns
        (fail if gap else ok)("database upgrades applied", f"jobs is missing {sorted(gap)}" if gap else "up to date")
        with db.engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(db.users.insert().values(email=f"check-{uuid.uuid4().hex}@invalid", password_hash="x"))
                n = conn.execute(select(func.count()).select_from(db.users)).scalar()
                ok("database write and read", f"{n} user row(s) visible inside the test transaction")
            finally:
                trans.rollback()
        with db.engine.connect() as conn:
            admins = conn.execute(select(func.count()).select_from(db.users).where(db.users.c.is_admin.is_(True))).scalar()
        if not admins:
            warn("accounts", "no admin user yet - create one with: python -m webapp.api.seed_users add EMAIL \"Name\" --admin")
        else:
            ok("accounts", f"{admins} admin user(s)")
    except Exception as e:  # noqa: BLE001 - report it, keep checking the rest
        fail("database", f"{type(e).__name__}: {str(e)[:300]}")

    # storage
    key = f"_check_deploy/{uuid.uuid4().hex}.txt"
    try:
        st = storage.get_storage()
        st.put_bytes(key, b"check")
        got = st.get_bytes(key)
        st.delete(key)
        (ok if got == b"check" else fail)(f"storage ({config.STORAGE_BACKEND}) write, read, delete",
                                          "round trip matches" if got == b"check" else "read back different bytes")
        if config.STORAGE_BACKEND == "local":
            warn("storage", "files are on this machine's disk only - lost with the machine; use S3 for a deployment")
    except Exception as e:  # noqa: BLE001
        fail(f"storage ({config.STORAGE_BACKEND})", f"{type(e).__name__}: {str(e)[:300]}")

    # OCR and PDF rendering: what extraction needs and a plain web app does not
    try:
        from pdf_extractor import _tesseract  # noqa: F401 - locates the binary
        import pytesseract
        ok("Tesseract", f"version {pytesseract.get_tesseract_version()}")
        from PIL import Image
        pytesseract.image_to_string(Image.new("RGB", (200, 80), "white"))
        ok("Tesseract runs on an image")
    except Exception as e:  # noqa: BLE001
        fail("Tesseract", f"{type(e).__name__}: {str(e)[:300]}")
    try:
        import cv2
        import fitz
        doc = fitz.open()
        doc.new_page(width=200, height=200)
        pix = doc[0].get_pixmap()
        ok("PDF rendering and OpenCV", f"PyMuPDF {fitz.VersionBind}, OpenCV {cv2.__version__}, "
                                       f"rendered a {pix.width}x{pix.height} page")
    except Exception as e:  # noqa: BLE001
        fail("PDF rendering and OpenCV", f"{type(e).__name__}: {str(e)[:300]}")
    return out


def main():
    results = run_checks()
    width = max(len(label) for _, label, _ in results)
    for status, label, detail in results:
        print(f"{status:<4}  {label:<{width}}  {detail}")
    failed = sum(1 for s, _, _ in results if s == "FAIL")
    warned = sum(1 for s, _, _ in results if s == "WARN")
    print(f"\n{failed} failed, {warned} warning(s), {len(results) - failed - warned} ok")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
