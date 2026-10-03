"""Tests for the page-image side of the review app: the edge strip a reviewer
checks a page number against, the per-page images, the naming and zip export,
and the database upgrade that makes them possible. Everything runs against
throwaway files and databases, never the real ones.

    python -m webapp.api.test_page_export
"""

import asyncio
import io
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import fitz
from PIL import Image
from sqlalchemy import create_engine, text

from pdf_extractor import review

from . import config, db, main as api, page_export as pe, storage

_failures = []


def check(label, got, want):
    if got != want:
        _failures.append(f"FAIL {label}: got {got!r}, want {want!r}")


def png_bytes(color="white", size=(300, 400)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


def page(i, file_order=0, page_index=0, half=None, source="a.pdf", image_key="k"):
    return {"id": i, "file_order": file_order, "page_index": page_index, "spread_half": half,
            "source_file": source, "image_key": image_key}


def recs(*pages):
    return [{"page": p} for p in pages]


JOB = {"university": "LASU", "year": "2019"}


def test_labels():
    check("a real number is resolved", pe.is_resolved("45"), True)
    for placeholder in ("idx5", "idx12a", "IDX7", "", "  ", None):
        check(f"{placeholder!r} is not a real number", pe.is_resolved(placeholder), False)
    check("the most common real value wins", pe.page_label(["45", "45", "46"]), ("45", 1))
    check("placeholders and blanks never count against it", pe.page_label(["45", "idx3", "", "45"]), ("45", 0))
    check("all placeholders -> no label", pe.page_label(["idx3", "idx3"]), ("", 0))
    check("a tie goes to the value seen first", pe.page_label(["46", "45"])[0], "46")
    check("empty", pe.page_label([]), ("", 0))


def test_names():
    check("plain number", pe.render_name("{page}", page="45", slug="s", university="LASU", year="2019"), "45")
    check("fields combine", pe.render_name("{university}_{year}_{page}", page="45", slug="s", university="LASU", year="2019"),
          "LASU_2019_45")
    check("padding works through the pattern", pe.render_name("{page:0>3}", page="5", slug="s", university="u", year="y"), "005")
    for bad in ("{pagee}", "{0}", "{page", "{page!x}"):
        try:
            pe.render_name(bad, page="1", slug="s", university="u", year="y")
            check(f"pattern {bad!r} is refused", "accepted", "refused")
        except ValueError:
            pass
    escaped = pe.safe_filename("../../etc/passwd")
    check("a traversal attempt can't survive as a path", ("/" in escaped, "\\" in escaped, escaped.startswith(".")),
          (False, False, False))
    check("a backslash is neutralised too", "\\" in pe.safe_filename("a/b\\c"), False)
    check("a name is never empty", pe.safe_filename("..."), "page")


def name_for(university, year, page):
    """The file name a page would get, the way the real default pattern builds it."""
    return pe.render_name(pe.DEFAULT_PATTERN, page=page, slug=pe.slug_for(university), university=university,
                          year=pe.year_for(year))


def test_names_match_the_real_samples():
    # the sample files Akowe's report assembly already uses
    check("covenant", name_for("COVENANT", "2022", "28"), "covenant-university-2022-28")
    check("covenant, typed in full", name_for("Covenant University", "2022", "59"), "covenant-university-2022-59")
    check("futa", name_for("FUTA", "2024", "100"), "futa-2024-100")
    check("UI is 'unibadan', not the 'university-of-ibadan' its ground-truth CSVs use", name_for("UI", "2016", "152"), "unibadan-2016-152")
    check("...however it was typed", name_for("University of Ibadan", "2019", "137"), "unibadan-2019-137")
    check("case and punctuation don't matter", pe.slug_for("  f.u.t.a  "), "futa")
    check("nor do spaces or hyphens", [pe.slug_for(n) for n in ("Covenant-University", "CovenantUniversity", "covenant  university")],
          ["covenant-university"] * 3)
    check("a university with no entry falls back to what was typed, hyphenated",
          pe.slug_for("Some New University"), "some-new-university")
    check("nothing typed still gives a usable name", pe.slug_for(""), "booklet")
    check("an override beats the built-in table", pe.slug_for("UI", {"ui": "ibadan"}), "ibadan")
    check("...and adds new universities", pe.slug_for("Bells", {"BELLS": "bells-univ"}), "bells-univ")
    check("UNILAG undergraduate (the sample unilag-2019-102)", name_for("UNILAG", "2019", "102"), "unilag-2019-102")
    check("UNILAG postgraduate (the sample unilag-pgs-2019-102), however typed",
          [name_for(u, "2019", "102") for u in ("UNILAG PG", "Unilag (Postgraduate)", "unilag PGS", "UNILAG Post Graduate", "UNILAG-postgrad")],
          ["unilag-pgs-2019-102"] * 5)
    check("'undergraduate' is the unmarked case, not postgraduate",
          [pe.slug_for(u) for u in ("UNILAG Undergraduate", "UNILAG UG", "Unilag (under grad)")], ["unilag"] * 3)
    check("a word that merely contains pg isn't taken for it", pe.slug_for("Upgrade University"), "upgrade-university")
    check("postgraduate on a university with no entry", pe.slug_for("Some New University PG"), "some-new-university-pgs")
    check("year: plain", pe.year_for("2022"), "2022")
    check("year: extra text", pe.year_for("2022 (after fix)"), "2022")
    check("year: a session takes the first", pe.year_for("2021/2022"), "2021")
    check("year: no year at all is kept as typed", pe.year_for(" unknown "), "unknown")


def test_normalize_page_number():
    for typed, want in [("45", "45"), (" 45 ", "45"), ("045", "45"), ("7", "7"), ("999", "999")]:
        check(f"{typed!r} is a page number", pe.normalize_page_number(typed), want)
    for typed in ("", "  ", "0", "000", "1000", "2022", "idx5", "45a", "4 5", "-3", "4.5", None):
        check(f"{typed!r} is not", pe.normalize_page_number(typed), None)


def test_plan():
    P = "{page}"
    pages = [page(1, page_index=0), page(2, page_index=1), page(3, page_index=2), page(4, page_index=3)]
    by_page = {1: recs("45", "45"), 2: recs("idx1", "idx1"), 3: recs("47", "47", "48"), 4: recs("45")}
    plan = pe.plan_page_files(JOB, pages, by_page, P)
    check("files in booklet order, always PNG", [e["filename"] for e in plan],
          ["45.png", "UNRESOLVED_file1_page2.png", "47.png", "45-2.png"])
    check("statuses", [e["status"] for e in plan], ["ok", "unresolved", "conflict", "duplicate"])
    check("the page with a disagreeing record still gets the majority number", plan[2]["label"], "47")
    check("a duplicate says which page it clashes with", plan[3]["clashes_with"], "page 1 of a.pdf")
    check("only a clash has one", [e["clashes_with"] for e in plan[:3]], [None, None, None])

    default = pe.plan_page_files(JOB, pages[:1], {1: recs("45")}, pe.DEFAULT_PATTERN)
    check("the default pattern gives the real naming", default[0]["filename"], "lasu-2019-45.png")
    custom = pe.plan_page_files(JOB, pages[:1], {1: recs("45")}, "{university}_{year}_{page}")
    check("a custom pattern can use the other fields", custom[0]["filename"], "LASU_2019_45.png")
    overridden = pe.plan_page_files(JOB, pages[:1], {1: recs("45")}, pe.DEFAULT_PATTERN, {"LASU": "lagos-state"})
    check("slug overrides reach the file name", overridden[0]["filename"], "lagos-state-2019-45.png")

    shuffled = pe.plan_page_files(JOB, [pages[3], pages[0], pages[2], pages[1]], by_page, P)
    check("input order doesn't matter - output follows the booklet", [e["page"]["id"] for e in shuffled], [1, 2, 3, 4])

    two_files = [page(1, file_order=1, page_index=0), page(2, file_order=0, page_index=5)]
    check("an earlier file comes first even with a higher page index",
          [e["page"]["id"] for e in pe.plan_page_files(JOB, two_files, {1: recs("9"), 2: recs("8")}, P)], [2, 1])

    spread = [page(1, half=1), page(2, half=0)]
    plan = pe.plan_page_files(JOB, spread, {1: recs("idx0b"), 2: recs("idx0a")}, P)
    check("the halves of a spread are ordered and named apart",
          [e["filename"] for e in plan], ["UNRESOLVED_file1_page1a.png", "UNRESOLVED_file1_page1b.png"])

    check("a page with no records is not exported", pe.plan_page_files(JOB, [page(1)], {}, P), [])
    check("case-insensitive duplicates are caught (Windows would overwrite)",
          [e["filename"] for e in pe.plan_page_files(JOB, [page(1), page(2, page_index=1)],
                                                     {1: recs("1a"), 2: recs("1A")}, P)],
          ["1a.png", "1A-2.png"])


def test_export_status():
    P = "{page}"
    pages = [page(1, page_index=0), page(2, page_index=1), page(3, page_index=2)]
    ok = pe.export_status(pe.plan_page_files(JOB, pages, {1: recs("45"), 2: recs("46"), 3: recs("47", "47")}, P))
    check("every page numbered once -> ready", (ok["ready"], ok["pages"], ok["problems"]), (True, 3, []))

    blocked = pe.export_status(pe.plan_page_files(JOB, pages, {1: recs("45"), 2: recs("idx1"), 3: recs("47")}, P))
    check("one page still unnumbered -> not ready", blocked["ready"], False)
    check("...and it says which page and why",
          [(q["page_id"], q["kind"], q["where"]) for q in blocked["problems"]], [(2, "unresolved", "page 2 of a.pdf")])

    dup = pe.export_status(pe.plan_page_files(JOB, pages, {1: recs("45"), 2: recs("46"), 3: recs("45")}, P))
    check("two pages claiming one number -> not ready", (dup["ready"], dup["problems"][0]["kind"]), (False, "duplicate"))
    check("...and the problem points at the other page", "page 1 of a.pdf" in dup["problems"][0]["detail"], True)

    conflict = pe.export_status(pe.plan_page_files(JOB, pages, {1: recs("45", "45", "46"), 2: recs("46"), 3: recs("47")}, P))
    check("rows disagreeing about a page's number -> not ready", (conflict["ready"], conflict["problems"][0]["kind"]),
          (False, "conflict"))
    check("...saying how many differ", "1 of 3" in conflict["problems"][0]["detail"], True)

    many = pe.export_status(pe.plan_page_files(JOB, pages, {1: recs("idx0"), 2: recs("idx1"), 3: recs("idx2")}, P))
    check("every problem is listed, not just the first", len(many["problems"]), 3)
    check("nothing to export is not 'ready'", pe.export_status([])["ready"], False)


def test_two_number_pages_are_explained():
    import json
    seen = json.dumps([{"side": "left", "value": 32}, {"side": "right", "value": 33}])
    two = {**page(1), "page_note": "two_numbers", "seen_numbers": seen, "suggested_page": "33", "number_image_key": "strip1"}
    only = {**page(2, page_index=1), "page_note": "neighbour_number",
            "seen_numbers": json.dumps([{"side": "left", "value": 33}]), "suggested_page": "34"}
    plain = page(3, page_index=2)
    status = pe.export_status(pe.plan_page_files(JOB, [two, only, plain], {1: recs("idx0"), 2: recs("idx1"), 3: recs("idx2")}, "{page}"))
    p1, p2, p3 = status["problems"]
    check("two numbers: says so, naming both and where",
          ("two different page numbers" in p1["detail"], "32 in the left corner" in p1["detail"],
           "33 in the right corner" in p1["detail"]), (True, True, True))
    check("...and is not ready until a person sets it", status["ready"], False)
    check("...with the suggestion handed to the screen to prefill",
          (p1["note"], p1["suggested"], p1["seen"]),
          ("two_numbers", "33", [{"side": "left", "value": 32}, {"side": "right", "value": 33}]))
    check("...and the sequence hint in the text", "points to 33" in p1["detail"], True)
    check("neighbour's number only: explains it is probably the neighbour's",
          ("neighbouring page's" in p2["detail"], "33 in the left corner" in p2["detail"], p2["suggested"]), (True, True, "34"))
    check("a page with no note reads as before",
          (p3["detail"], p3["note"], p3["suggested"], p3["seen"]), ("No page number has been confirmed for this page yet.", "", "", []))
    broken = {**page(4), "page_note": "two_numbers", "seen_numbers": "not json"}
    q = pe.export_status(pe.plan_page_files(JOB, [broken], {4: recs("idx0")}, "{page}"))["problems"][0]
    check("damaged stored numbers don't break the status", (q["seen"], q["kind"]), ([], "unresolved"))
    # setting the number is what confirms it: the page is then fine whatever its note says
    confirmed = pe.export_status(pe.plan_page_files(JOB, [two], {1: recs("33", "33")}, "{page}"))
    check("once a person has set the number the page is ready", (confirmed["ready"], confirmed["problems"]), (True, []))


def test_zip():
    pages = [page(1, image_key="img1"), page(2, page_index=1, image_key="img2")]
    plan = pe.plan_page_files(JOB, pages, {1: recs("45"), 2: recs("46", "46")}, pe.DEFAULT_PATTERN)
    store = {"img1": png_bytes("red"), "img2": png_bytes("blue")}

    with pe.build_zip(plan, store.__getitem__) as f, zipfile.ZipFile(f) as z:
        check("the pages and the manifest are in the zip", z.namelist(), ["lasu-2019-45.png", "lasu-2019-46.png", "manifest.csv"])
        check("pages are stored untouched", z.read("lasu-2019-45.png"), store["img1"])
        check("the right image is behind each name", z.read("lasu-2019-46.png"), store["img2"])
        check("manifest", z.read("manifest.csv").decode().splitlines(),
              ["file,page_number,source_file,physical_page,records",
               "lasu-2019-45.png,45,a.pdf,1,1", "lasu-2019-46.png,46,a.pdf,2,2"])

    try:
        pe.build_zip(plan, {"img1": store["img1"]}.__getitem__)
        check("an unreadable page image fails the whole export", "built", "refused")
    except pe.MissingPageImage as e:
        check("...and says which page", "page 2 of a.pdf" in str(e), True)


def test_strip_image():
    img = Image.new("RGB", (400, 1000), "white")
    for y in range(930, 1000):          # bottom edge: a number at the foot of the page
        for x in range(180, 220):
            img.putpixel((x, y), (255, 0, 0))
    for y in range(500, 560):           # the middle of the page: must NOT appear
        for x in range(180, 220):
            img.putpixel((x, y), (0, 0, 255))
    strip = review.page_number_strip(img)
    colors = {c for _, c in strip.getcolors(maxcolors=1_000_000)}
    check("strip is full width", strip.width, 400)
    check("strip height is two 15% bands plus labels and a gap", strip.height, 2 * (22 + 150) + 10)
    check("the number printed at the foot of the page is in the strip", (255, 0, 0) in colors, True)
    check("the middle of the page is not", (0, 0, 255) in colors, False)
    red_rows = [y for y in range(strip.height) if strip.getpixel((200, y)) == (255, 0, 0)]
    check("it appears in the BOTTOM block, which comes first", red_rows and max(red_rows) < 22 + 150, True)


def _make_pdf(path: Path, pages=2):
    doc = fitz.open()
    for i in range(pages):
        pg = doc.new_page(width=300, height=400)
        pg.insert_text((100, 200), f"Row {i}")
        pg.insert_text((140, 385), f"{i + 41}")
    doc.save(str(path))
    doc.close()


def test_generate_page_images():
    tmp = Path(tempfile.mkdtemp())
    try:
        pdf = tmp / "b.pdf"
        _make_pdf(pdf)

        def geom(i):
            return {"pdf_path": str(pdf), "page_index": i, "zoom": 2.0, "dewarped": False, "rotated_by": 0,
                    "spread_halves": 1, "spread_half": None, "page_number_status": "uncertain"}
        records = [{"source_file": "b.pdf", "_geom": geom(0), "page": "idx0"},
                   {"source_file": "b.pdf", "_geom": geom(0), "page": "idx0"},   # second name on the same page
                   {"source_file": "b.pdf", "_geom": geom(1), "page": "idx1"},
                   {"source_file": "b.pdf", "page": "7"}]                          # no geometry at all
        out = review.generate_page_images(records, tmp / "_pages", zoom=3.0)
        check("one entry per physical page, not per record", list(out), [("b.pdf", 0, None), ("b.pdf", 1, None)])
        first = out[("b.pdf", 0, None)]
        check("both images are written", (tmp / first["page"].split("/")[-2] / Path(first["page"]).name).exists()
              and (tmp / first["strip"].split("/")[-2] / Path(first["strip"]).name).exists(), True)
        check("the page image uses the requested zoom, not the OCR zoom",
              Image.open(tmp / first["page"]).size, (900, 1200))
        check("the strip is a different, shorter image", Image.open(tmp / first["strip"]).height < 1200, True)
        check("the page's geometry is carried through", first["geom"]["page_number_status"], "uncertain")
        check("each page gets its own files", out[("b.pdf", 1, None)]["page"] != first["page"], True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class _Sandbox:
    """A throwaway database file and storage folder, swapped in for the real ones."""

    def __enter__(self):
        self.dir = Path(tempfile.mkdtemp())
        self.real = (db.engine, config.LOCAL_DATA_DIR)
        db.engine = create_engine(f"sqlite:///{self.dir / 't.db'}", connect_args={"check_same_thread": False})
        config.LOCAL_DATA_DIR = self.dir
        (self.dir / "files").mkdir()
        self.real_auth = api._auth_header_or_query
        api._auth_header_or_query = lambda request, token: 1
        db.init_db()
        return self

    def __exit__(self, *exc):
        api._auth_header_or_query = self.real_auth
        db.engine.dispose()
        db.engine, config.LOCAL_DATA_DIR = self.real
        shutil.rmtree(self.dir, ignore_errors=True)


def _seed(conn, with_pages=True):
    conn.execute(db.users.insert().values(id=1, email="d@akowe.example", password_hash="x"))
    conn.execute(db.jobs.insert().values(id=1, university="LASU", year="2019", filenames=["a.pdf"],
                                         storage_prefix="jobs/1", uploaded_by=1, status="ready"))
    page_ids = []
    if with_pages:
        for i in range(2):
            page_ids.append(conn.execute(db.pages.insert().values(
                job_id=1, source_file="a.pdf", file_order=0, page_index=i, printed_page="",
                page_status="uncertain", image_key=f"jobs/1/pages/pg{i}.png",
                number_image_key=f"jobs/1/pages/pg{i}_number.png")).inserted_primary_key[0])
    for r, (name, pg, pid) in enumerate([("A One", "idx0", 0), ("B Two", "idx0", 0), ("C Three", "idx1", 1)]):
        conn.execute(db.records.insert().values(
            job_id=1, row_index=r, name=name, page=pg, original_fields={}, page_id=page_ids[pid] if with_pages else None))
    return page_ids


def _download():
    resp = api.export_page_images(1, request=None, token=None)

    async def collect():
        return b"".join([chunk async for chunk in resp.body_iterator])
    return resp, zipfile.ZipFile(io.BytesIO(asyncio.run(collect())))


def _refused(call):
    """The (status, message) an endpoint refuses with, or None if it didn't."""
    try:
        call()
    except api.HTTPException as e:
        return e.status_code, e.detail
    return None


def test_api_flow():
    with _Sandbox():
        st = storage.get_storage()
        for i, color in enumerate(("red", "blue")):
            st.put_bytes(f"jobs/1/pages/pg{i}.png", png_bytes(color))
            st.put_bytes(f"jobs/1/pages/pg{i}_number.png", png_bytes("white", (300, 100)))
        with db.engine.begin() as conn:
            _seed(conn)

        got = api.get_job(1, user_id=1)
        check("every record carries its page's edge-strip image",
              [r["pageno_image"] for r in got["records"]],
              ["jobs/1/pages/pg0_number.png", "jobs/1/pages/pg0_number.png", "jobs/1/pages/pg1_number.png"])

        status = api.page_images_status(1, user_id=1)
        check("nothing numbered yet: available but not ready", (status["available"], status["ready"], status["pages"]), (True, False, 2))
        check("both pages are listed as in the way", [(q["kind"], q["where"]) for q in status["problems"]],
              [("unresolved", "page 1 of a.pdf"), ("unresolved", "page 2 of a.pdf")])
        check("each problem names a page the screen can jump to", sorted(q["page_id"] for q in status["problems"]), [1, 2])
        check("...and carries the image to check its number against",
              sorted(q["number_image"] for q in status["problems"]), ["jobs/1/pages/pg0_number.png", "jobs/1/pages/pg1_number.png"])

        refused = _refused(lambda: _download())
        check("the download is refused while any page lacks a number", refused and refused[0], 409)
        check("...and says what is in the way", refused and "2 pages still need a confirmed page number" in refused[1], True)

        # a reviewer fixes ONE of the two pages, with the page-level action
        check("a bad number is refused with a reason",
              (_refused(lambda: api.set_page_number(1, api.PageNumberUpdate(page="2022"), user_id=1)) or (None,))[0], 422)
        check("an unknown page is a 404",
              (_refused(lambda: api.set_page_number(99, api.PageNumberUpdate(page="45"), user_id=1)) or (None,))[0], 404)
        done = api.set_page_number(1, api.PageNumberUpdate(page=" 045 "), user_id=1)
        check("one action sets the number on every row of the page, cleaned up", (done["records"], done["page"]), (2, "45"))
        with db.engine.begin() as conn:
            rows = conn.execute(text("SELECT page, updated_by FROM records ORDER BY row_index")).all()
            printed = conn.execute(text("SELECT printed_page FROM pages WHERE id = 1")).scalar()
        check("the other page's rows are untouched; the edited rows record who did it",
              [tuple(r) for r in rows], [("45", 1), ("45", 1), ("idx1", None)])
        check("the page remembers its number too", printed, "45")
        check("fixing one page isn't enough", api.page_images_status(1, user_id=1)["ready"], False)
        refused = _refused(lambda: _download())
        check("the refusal now names the remaining page", refused and "page 2 of a.pdf" in refused[1], True)

        api.set_page_number(2, api.PageNumberUpdate(page="46"), user_id=1)
        status = api.page_images_status(1, user_id=1)
        check("once every page is numbered the status is ready", (status["ready"], status["problems"]), (True, []))
        resp, z = _download()
        check("the zip holds each page under its real name, with the manifest",
              sorted(z.namelist()), ["lasu-2019-45.png", "lasu-2019-46.png", "manifest.csv"])
        check("...with the right image behind each name",
              z.read("lasu-2019-45.png") == png_bytes("red") and z.read("lasu-2019-46.png") == png_bytes("blue"), True)
        check("it downloads as a zip named for the booklet",
              (resp.media_type, resp.headers["content-disposition"]),
              ("application/zip", 'attachment; filename="lasu-2019-pages.zip"'))

        # a reviewer then mistypes one page's number to match the other
        api.set_page_number(2, api.PageNumberUpdate(page="45"), user_id=1)
        status = api.page_images_status(1, user_id=1)
        check("two pages with the same number block the download again",
              (status["ready"], [q["kind"] for q in status["problems"]]), (False, ["duplicate"]))
        check("...and the download is refused, not silently renamed", (_refused(lambda: _download()) or (None,))[0], 409)
        api.set_page_number(2, api.PageNumberUpdate(page="46"), user_id=1)
        check("fixing it lifts the block", api.page_images_status(1, user_id=1)["ready"], True)

        # one row on a page corrected, its neighbours not
        with db.engine.begin() as conn:
            conn.execute(text("UPDATE records SET page = '48' WHERE row_index = 0"))
        status = api.page_images_status(1, user_id=1)
        check("rows that disagree about a page's number block the download",
              (status["ready"], [q["kind"] for q in status["problems"]]), (False, ["conflict"]))
        with db.engine.begin() as conn:
            conn.execute(text("UPDATE records SET page = '45' WHERE row_index = 0"))
        check("agreeing again lifts it", api.page_images_status(1, user_id=1)["ready"], True)

        check("unknown job -> 404", (_refused(lambda: api.export_page_images(99, request=None, token=None)) or (None,))[0], 404)

        real = config.PAGE_IMAGE_NAME_PATTERN
        config.PAGE_IMAGE_NAME_PATTERN = "{nonsense}"
        check("a broken name pattern is reported, not silently used",
              (_refused(lambda: _download()) or (None,))[0], 500)
        config.PAGE_IMAGE_NAME_PATTERN = real

        real_slugs = config.PAGE_IMAGE_SLUGS
        config.PAGE_IMAGE_SLUGS = {"LASU": "lagos-state"}
        resp, z = _download()
        check("a configured university short name reaches the file names",
              sorted(n for n in z.namelist() if n != "manifest.csv"), ["lagos-state-2019-45.png", "lagos-state-2019-46.png"])
        config.PAGE_IMAGE_SLUGS = real_slugs

        (config.LOCAL_DATA_DIR / "files" / "jobs/1/pages/pg1.png").unlink()   # a stored page image goes missing
        refused = _refused(lambda: _download())
        check("a page image missing from storage fails the download and names the page",
              (refused and refused[0], refused and "page 2 of a.pdf" in refused[1]), (500, True))


def test_job_without_pages():
    with _Sandbox():
        with db.engine.begin() as conn:
            _seed(conn, with_pages=False)
        got = api.get_job(1, user_id=1)
        check("an older job's records simply have no strip", {r["pageno_image"] for r in got["records"]}, {None})
        check("its status says page images aren't available", api.page_images_status(1, user_id=1),
              {"available": False, "pages": 0, "ready": False, "problems": []})
        refused = _refused(lambda: api.export_page_images(1, request=None, token=None))
        check("the download says why it can't work", (refused and refused[0], refused and "upload it again" in refused[1]), (409, True))


def test_existing_database_is_upgraded():
    tmp = Path(tempfile.mkdtemp())
    real = db.engine
    try:
        db.engine = create_engine(f"sqlite:///{tmp / 'old.db'}")
        with db.engine.begin() as conn:   # a database from before pages existed: records has no page_id
            conn.execute(text("CREATE TABLE records (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, row_index INTEGER, "
                              "name TEXT, original_fields JSON)"))
            conn.execute(text("INSERT INTO records (id, job_id, row_index, name, original_fields) VALUES (7, 1, 0, 'Kept', '{}')"))
        db.init_db()
        db.init_db()   # running it again must be harmless
        with db.engine.begin() as conn:
            cols = {r[1] for r in conn.execute(text("PRAGMA table_info(records)"))}
            row = conn.execute(text("SELECT id, name, page_id FROM records")).one()
            tables = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        check("the new column is added", "page_id" in cols, True)
        check("existing rows survive, with no page", tuple(row), (7, "Kept", None))
        check("the pages table is created", "pages" in tables, True)
    finally:
        db.engine.dispose()
        db.engine = real
        shutil.rmtree(tmp, ignore_errors=True)


def test_database_from_before_page_notes_is_upgraded():
    """The state of a dev database that already has the pages table, written before
    the two-numbers columns existed."""
    tmp = Path(tempfile.mkdtemp())
    real = db.engine
    try:
        db.engine = create_engine(f"sqlite:///{tmp / 'mid.db'}")
        with db.engine.begin() as conn:
            conn.execute(text("CREATE TABLE pages (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, source_file TEXT, "
                              "file_order INTEGER, page_index INTEGER, spread_half INTEGER, printed_page TEXT, "
                              "page_status TEXT, image_key TEXT, number_image_key TEXT)"))
            conn.execute(text("INSERT INTO pages (id, job_id, source_file, file_order, page_index, printed_page, page_status) "
                              "VALUES (3, 1, 'a.pdf', 0, 0, '45', 'detected')"))
        db.init_db()
        db.init_db()
        with db.engine.begin() as conn:
            cols = {r[1] for r in conn.execute(text("PRAGMA table_info(pages)"))}
            row = conn.execute(text("SELECT id, printed_page, page_note, seen_numbers, suggested_page FROM pages")).one()
        check("the new page columns are added", {"page_note", "seen_numbers", "suggested_page"} <= cols, True)
        check("the existing page survives, with no note", tuple(row), (3, "45", None, None, None))
    finally:
        db.engine.dispose()
        db.engine = real
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    for t in (test_labels, test_names, test_names_match_the_real_samples, test_normalize_page_number, test_plan, test_export_status, test_two_number_pages_are_explained, test_zip, test_strip_image, test_generate_page_images,
              test_api_flow, test_job_without_pages, test_existing_database_is_upgraded,
              test_database_from_before_page_notes_is_upgraded):
        t()
    if _failures:
        print("\n".join(_failures))
        print(f"{len(_failures)} FAILED")
        sys.exit(1)
    print("all page_export tests passed")


if __name__ == "__main__":
    main()
