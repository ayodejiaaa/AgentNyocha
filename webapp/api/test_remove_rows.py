"""Tests for removing a row (a leftover fragment of a wrapped name, a stray line): it is hidden and
left out of the export, the page-number checks and the corrections report, and can be put back.
Nothing is ever deleted. Throwaway database only.

    python -m webapp.api.test_remove_rows
"""

import asyncio
import csv
import io
import shutil
import sys
import tempfile
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import create_engine, select, text

from . import db, extraction, main as api, mine_corrections
from .test_deploy import _Sandbox

_failures = []

FIELDS = ["name", "faculty", "department", "course", "degree", "grade", "distinction_notes", "year", "year_note",
          "page", "programme", "affiliated_institution", "country"]


def check(label, got, want):
    if got != want:
        _failures.append(f"FAIL {label}: got {got!r}, want {want!r}")


def add_record(conn, rid, name, page="45", page_id=None, **extra):
    blank = {f: "" for f in FIELDS}
    row = {**blank, "name": name, "page": page}
    conn.execute(db.records.insert().values(
        id=rid, job_id=1, row_index=rid, **row, original_fields={**blank, "name": extra.pop("original_name", name), "page": page},
        confidence="low", flag_reason="single_word_name", reviewed=False, page_id=page_id, **extra))


def seed(conn, names):
    conn.execute(db.jobs.insert().values(id=1, university="LASU", year="2019", filenames=["a.pdf"],
                                         storage_prefix="jobs/1", uploaded_by=1, status="ready"))
    for i, name in enumerate(names, start=1):
        add_record(conn, i, name)


def exported_names():
    api._auth_header_or_query = lambda request, token: 1
    resp = api.export_job(1, request=None, token=None)

    async def read():
        return b"".join([c if isinstance(c, bytes) else c.encode() async for c in resp.body_iterator]).decode()
    return [r["name"] for r in csv.DictReader(io.StringIO(asyncio.run(read())))]


def patch(rid, **kw):
    return api.update_record(rid, api.RecordUpdate(**kw), user_id=1)


def patch_status(rid, **kw):
    try:
        patch(rid, **kw)
    except HTTPException as e:
        return e.status_code
    return 200


def test_removed_row_is_hidden_from_the_export_and_can_come_back():
    real = api._auth_header_or_query
    try:
        with _Sandbox() as box:
            with db.engine.begin() as conn:
                seed(conn, ["ADEYEMI Olufemi", "Samuel", "OKAFOR Chidi Emeka"])
            check("everything is exported to begin with", exported_names(), ["ADEYEMI Olufemi", "Samuel", "OKAFOR Chidi Emeka"])
            patch(2, removed=True)
            check("a removed row is left out of the export", exported_names(), ["ADEYEMI Olufemi", "OKAFOR Chidi Emeka"])
            check("...and the helper the export uses agrees", [r["name"] for r in extraction.export_clean_rows(1)],
                  ["ADEYEMI Olufemi", "OKAFOR Chidi Emeka"])
            with db.engine.begin() as conn:
                still = conn.execute(select(db.records.c.name, db.records.c.removed).where(db.records.c.id == 2)).one()
            check("nothing is deleted: the row is still there, marked removed", tuple(still), ("Samuel", True))
            check("it is recorded who and when", _who(2), (1, True))
            patch(2, removed=False)
            check("putting it back puts it back in the export, in its place",
                  exported_names(), ["ADEYEMI Olufemi", "Samuel", "OKAFOR Chidi Emeka"])
            check("removing a row twice is harmless", (patch_status(1, removed=True), patch_status(1, removed=True)), (200, 200))
            check("a row can be edited and removed together", patch_status(3, name="OKAFOR Chidi Emeka Jr", removed=True), 200)
            check("...and the edit is kept for when it is restored", _name(3), "OKAFOR Chidi Emeka Jr")
            check("an unknown row", patch_status(999, removed=True), 404)
            check("a request that sets nothing is refused", patch_status(1), 400)
    finally:
        api._auth_header_or_query = real


def _who(rid):
    with db.engine.begin() as conn:
        r = conn.execute(select(db.records.c.updated_by, db.records.c.updated_at).where(db.records.c.id == rid)).one()
    return (r[0], r[1] is not None)


def _name(rid):
    with db.engine.begin() as conn:
        return conn.execute(select(db.records.c.name).where(db.records.c.id == rid)).scalar()


def test_the_review_screen_still_gets_removed_rows():
    with _Sandbox():
        with db.engine.begin() as conn:
            seed(conn, ["ADEYEMI Olufemi", "Samuel"])
        patch(2, removed=True)
        got = {r["id"]: r["removed"] for r in api.get_job(1, user_id=1)["records"]}
        check("the screen is sent every row with its removed flag, so it can show and restore them", got, {1: False, 2: True})
        check("a row that was never removed says so", all(isinstance(v, bool) for v in got.values()), True)


def test_removed_rows_no_longer_hold_up_the_page_images():
    """A fragment row often carries the wrong page value; it must not block the download once removed."""
    with _Sandbox():
        with db.engine.begin() as conn:
            conn.execute(db.jobs.insert().values(id=1, university="LASU", year="2019", filenames=["a.pdf"],
                                                 storage_prefix="jobs/1", uploaded_by=1, status="ready"))
            pid = conn.execute(db.pages.insert().values(
                job_id=1, source_file="a.pdf", file_order=0, page_index=0, printed_page="", page_status="detected",
                image_key="jobs/1/pages/p.png", number_image_key="jobs/1/pages/s.png")).inserted_primary_key[0]
            for rid, name, page in ((1, "ADEYEMI Olufemi Samuel", "45"), (2, "Samuel", "45"), (3, "OKAFOR Chidi", "46")):
                add_record(conn, rid, name, page=page, page_id=pid)
        status = api.page_images_status(1, user_id=1)
        check("rows that disagree about the page number block the download", (status["ready"], status["problems"][0]["kind"]), (False, "conflict"))
        patch(3, removed=True)
        status = api.page_images_status(1, user_id=1)
        check("removing the odd row out clears it", (status["ready"], status["problems"]), (True, []))
        patch(3, removed=False)
        check("restoring it brings the problem back", api.page_images_status(1, user_id=1)["ready"], False)
        patch(3, removed=True)
        patch(1, removed=True)
        patch(2, removed=True)
        check("a page with every row removed has nothing to export, and is not 'ready'",
              (api.page_images_status(1, user_id=1)["pages"], api.page_images_status(1, user_id=1)["ready"]), (0, False))


def test_removed_rows_are_left_out_of_the_corrections_report():
    with _Sandbox():
        with db.engine.begin() as conn:
            conn.execute(db.jobs.insert().values(id=1, university="LASU", year="2019", filenames=["a.pdf"],
                                                 storage_prefix="jobs/1", uploaded_by=1, status="ready"))
            add_record(conn, 1, "ADEYEMI Olufemi Samuel", original_name="ADEYEMI Olufemi")   # a real correction
            add_record(conn, 2, "XX junk typed by mistake", original_name="Samuel")            # an edit, then removed
        patch(2, removed=True)
        diffs = mine_corrections.find_corrections()
        check("a correction on a kept row is reported", [d["after"] for d in diffs], ["ADEYEMI Olufemi Samuel"])
        patch(2, removed=False)
        check("a restored row's edits count again", sorted(d["after"] for d in mine_corrections.find_corrections()),
              ["ADEYEMI Olufemi Samuel", "XX junk typed by mistake"])


def test_old_databases_get_the_column_and_keep_their_rows():
    tmp = Path(tempfile.mkdtemp())
    real = db.engine
    try:
        db.engine = create_engine(f"sqlite:///{tmp / 'old.db'}")
        with db.engine.begin() as conn:
            conn.execute(text("CREATE TABLE records (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, row_index INTEGER, "
                              "name TEXT, original_fields JSON)"))
            conn.execute(text("INSERT INTO records (id, job_id, row_index, name, original_fields) VALUES (7, 1, 0, 'Kept', '{}')"))
        db.init_db()
        db.init_db()
        with db.engine.begin() as conn:
            row = conn.execute(text("SELECT id, name, removed FROM records")).one()
        check("an older database gains the column, and every existing row counts as not removed", tuple(row), (7, "Kept", 0))
    finally:
        db.engine.dispose()
        db.engine = real
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    for t in (test_removed_row_is_hidden_from_the_export_and_can_come_back, test_the_review_screen_still_gets_removed_rows,
              test_removed_rows_no_longer_hold_up_the_page_images, test_removed_rows_are_left_out_of_the_corrections_report,
              test_old_databases_get_the_column_and_keep_their_rows):
        t()
    if _failures:
        print("\n".join(_failures))
        print(f"{len(_failures)} FAILED")
        sys.exit(1)
    print("all remove-row tests passed")


if __name__ == "__main__":
    main()
