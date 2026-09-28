"""Batch driver: walks a whole university/year folder of split PDF files.

Most of Akowe's real source material isn't one PDF per convocation booklet -
a single booklet gets scanned and cut into several files (e.g. LASU 2005's
folder holds "LASU 2005 (PGS 20 - 127)...pdf", "LASU 2005 (PGS 128 - 132)
...pdf", "LASU 2005 pg 133...pdf"). Those are one continuous document, not
three separate ones, so header state (faculty/department/course/degree/
grade/year) needs to carry forward across the file boundary exactly the way
it already carries forward across page boundaries within a single file -
the split is an artifact of scanning, not a break in the actual list.

Usage:
    # one university/year folder -> one CSV
    python -m pdf_extractor.batch "path/to/LASU/2005" -o lasu_2005.csv

    # a whole root of <university>/<year>/ folders -> one CSV per folder
    python -m pdf_extractor.batch "path/to/root" --walk -o output_dir/
"""

import argparse
import csv
import re
import sys
from pathlib import Path

from . import cli, parser

# Same source_file convention as Canonical Training Data/akowe_canonical_master.csv.
# It's not just a nicety here: a folder's split files each run their own
# independent page-number detection (see cli.extract_pdf_with_state), so
# when detection fails on a page the fallback label ("idx1", "idx2", ...)
# is only unique WITHIN that file - two different physical PDFs in the same
# folder can each produce a record labeled page "idx1". Once records from
# multiple files are concatenated into one CSV, "page" alone can no longer
# tell those apart, which matters because page is the load-bearing field
# the client report points at. source_file is what restores uniqueness.
BATCH_FIELDS = parser.CANON_FIELDS + ["source_file"]

# "PGS 33 - 132", "pg 133", "(from page 182)" and a bare "(195 - 202)" all
# mark where a split file starts in the booklet.
_PG_PREFIXED_RE = re.compile(
    r"PGS?\.?\s*(\d+)|pages?\s+(\d+)|\(\s*(\d+)\s*-\s*\d+\s*\)", re.IGNORECASE)
_YEAR_FOLDER_RE = re.compile(r"^(19|20)\d{2}$")


def _looks_like_year_folder(folder_name: str) -> bool:
    """A genuine "one booklet split across several files" folder is always
    named a bare 4-digit year in every folder sampled (2005, 2018, ...).
    Not every leaf folder under a university is one of these, though - e.g.
    COVENANT has a "Degrees" folder holding four PDFs
    (covenant-degree-2018.pdf, -2019.pdf, -2021.pdf, -2022.pdf) that are
    each a COMPLETE, independent document for a different year, not split
    fragments of one list. Carrying header state across those the way
    process_folder does for a real split would leak 2018's trailing state
    into 2019's opening pages. This check is what tells process_tree()
    which handling applies to a given leaf folder."""
    return bool(_YEAR_FOLDER_RE.match(folder_name.strip()))


def _filename_sort_key(pdf_path: Path):
    """Orders one folder's split files by the "PGS <start> - <end>" (or
    "pg <n>") marker in the filename, when present.

    This was tried the other way first: OCR-detecting the printed page
    number on each file's first few pages, to avoid trusting filenames at
    all. That measured *less* reliable in practice - detect_page_number()
    occasionally locks onto a stray digit (a footnote number, a list
    numeral) near a page edge and reports it as the page number, which for
    a file with no early, reliably-numbered page produced nonsense
    (e.g. a baseline of -5 for a file that actually starts around page
    196) - all while costing an extra OCR pass per file just to order
    them. The filename marker, by contrast, matched the true physical
    order on every real folder sampled (LASU, UI, COVENANT).

    A file with NO "PGS"/"pg" marker at all sorts first (rank -1): in
    every folder sampled, that unmarked file was the main/undifferentiated
    list, and any marked file was a carved-out sub-range (a "MASTERS AND
    PHD" supplement, a tail-end range) that comes after it - so this is a
    real, if imperfect, empirical convention rather than an arbitrary
    guess. It deliberately does NOT fall back to "the first number in the
    filename" the way an earlier version of this did - that grabbed the
    convocation YEAR (e.g. "2018") out of an unmarked file's name and
    mis-sorted it after a "PGS 24-39" file instead of before it.
    """
    name = pdf_path.stem
    m = _PG_PREFIXED_RE.search(name)
    num = int(next(g for g in m.groups() if g)) if m else -1
    return (num, name.lower())


def write_batch_csv(records: list[dict], out_path: str):
    """Like cli.write_csv, but also carries the source_file column (see
    BATCH_FIELDS) - needed anywhere records from more than one PDF can end
    up in the same CSV."""
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=BATCH_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            writer.writerow(r)


def find_pdfs_in_folder(folder_path) -> list[Path]:
    """Non-recursive: the split PDFs directly inside one university/year
    folder, in best-guess document order (see _filename_sort_key)."""
    folder = Path(folder_path)
    pdfs = [p for p in folder.iterdir() if p.suffix.lower() == ".pdf"]
    return sorted(pdfs, key=_filename_sort_key)


def find_year_folders(base_path) -> list[Path]:
    """Two levels deep - <base>/<university>/<year>/ - each leaf folder that
    holds PDFs directly, for --walk mode."""
    base = Path(base_path)
    folders = []
    for university_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        for year_dir in sorted(p for p in university_dir.iterdir() if p.is_dir()):
            if any(p.suffix.lower() == ".pdf" for p in year_dir.iterdir()):
                folders.append(year_dir)
    return folders


_DEDUPE_FIELDS = ("name", "faculty", "department", "course", "degree", "grade", "year")


# Measured on every multi-file folder in the corpus (88 file pairs): files that
# are just different parts of a booklet share 0-4% of their names (common
# names only); the two real overlaps found shared 30% and 83%. 15% sits well
# clear of both.
_OVERLAP_MIN_SHARED = 0.15
_NEAR_COPY_SHARED = 0.6


def _name_key(name: str) -> frozenset:
    return frozenset(re.findall(r"[a-z]{2,}", str(name).lower()))


def drop_overlapping_files(records: list[dict]) -> list[dict]:
    """Handles files that overlap or repeat, where matching whole records
    fails because a re-scan starts without the earlier headers and so carries
    different faculty/course/year context for the same people (UI 2021's
    "PGS 319 - 321" file is a ~4,000-record copy of "PGS 286 - 352" whose
    records differ in course/degree/year on ~half the rows).

    A later file that shares at least _OVERLAP_MIN_SHARED of its names with an
    earlier one (measured against the smaller file) has those names removed;
    if it shares _NEAR_COPY_SHARED or more it is a copy of the earlier file
    and is dropped entirely - its unshared remainder is only OCR variants of
    the same people."""
    order: list[str] = []
    names: dict[str, set] = {}
    for r in records:
        f = r.get("source_file", "")
        if f not in names:
            order.append(f)
            names[f] = set()
        k = _name_key(r.get("name", ""))
        if k:
            names[f].add(k)
    dropped_files: set[str] = set()
    remove: dict[str, set] = {f: set() for f in order}
    for j, later in enumerate(order):
        for earlier in order[:j]:
            if earlier in dropped_files or not names[earlier] or not names[later]:
                continue
            shared = names[earlier] & names[later]
            frac = len(shared) / min(len(names[earlier]), len(names[later]))
            if frac >= _NEAR_COPY_SHARED:
                dropped_files.add(later)
            elif frac >= _OVERLAP_MIN_SHARED:
                remove[later] |= shared
    return [r for r in records
            if r.get("source_file", "") not in dropped_files
            and _name_key(r.get("name", "")) not in remove[r.get("source_file", "")]]


def drop_cross_file_duplicates(records: list[dict]) -> list[dict]:
    """Removes a record that is identical to one already taken from a
    DIFFERENT source file of the same folder. Split-booklet folders
    sometimes overlap or repeat: UI 2021's "PGS 130 - 200" overlaps "PGS 62 -
    150", and its "PGS 319 - 321" file actually holds ~4,000 records - a
    copy of "PGS 286 - 352" - so the same 3,300+ people were extracted
    twice (4,100 duplicate rows in one year). Records within a single file
    are never touched: two graduates can legitimately share a name and
    context on one page, whereas the same record showing up again in another
    file is the same person listed twice."""
    seen: dict[tuple, str] = {}
    out = []
    for r in records:
        key = tuple(str(r.get(f, "")).strip().lower() for f in _DEDUPE_FIELDS)
        first_file = seen.setdefault(key, r.get("source_file", ""))
        if first_file != r.get("source_file", ""):
            continue
        out.append(r)
    return out


def _physical_page(r: dict):
    g = r.get("_geom") or {}
    if g.get("page_index") is None:
        return None
    return (g["page_index"], g.get("spread_half"))


# The same 15% that separates re-printed FILES from chance name overlap between
# parts of one booklet (see _OVERLAP_MIN_SHARED) applies between PAGES of one
# file - measured on OOU 2020's first 20 pages: three re-printed pages share
# 72-87% of their names with the page they repeat, every other pair of pages
# shares none. The floor stops a couple of coincidental namesakes on a very
# short page from looking like a re-print.
_REPRINT_MIN_SHARED = 0.15
_REPRINT_MIN_NAMES = 5


def drop_repeats_across_pages(records: list[dict]) -> list[dict]:
    """A booklet can print the same people on more than one page - OOU 2020
    re-prints its opening pages 90-92 (names and all) as 93-95 - and the
    ground truth keeps such a person once, at the first page they appear.

    Whether a name is a repeat is confirmed at the PAGE level, not name by
    name: a later page that shares at least _REPRINT_MIN_SHARED of its names
    with an earlier page (measured against the smaller of the two) is a
    re-print of it, and the shared names are dropped from the later page. Two
    unrelated pages that happen to share one common name are left alone -
    that's two graduates with the same name. Matching on name only, not on
    faculty/course/grade as well, is deliberate: the same person printed twice
    routinely gets a different grade or course out of the extractor, because
    header context is carried forward differently on the two pages (on OOU
    2020 that hid all but 42 of ~210 repeats). Names are compared regardless
    of order and punctuation, like drop_overlapping_files.

    `records` are one file's, in page order, so "first" is the earliest
    physical page; a record with no page information is never dropped."""
    page_names: dict[tuple, set] = {}
    for r in records:
        page = _physical_page(r)
        key = _name_key(r.get("name", ""))
        if page is not None and key:
            page_names.setdefault(page, set()).add(key)

    order = list(page_names)  # first appearance == page order
    drop: dict[tuple, set] = {p: set() for p in order}
    for j, later in enumerate(order):
        for earlier in order[:j]:
            shared = page_names[earlier] & page_names[later]
            if len(shared) >= _REPRINT_MIN_NAMES and                     len(shared) / min(len(page_names[earlier]), len(page_names[later])) >= _REPRINT_MIN_SHARED:
                drop[later] |= shared

    out = []
    for r in records:
        page = _physical_page(r)
        if page is not None and _name_key(r.get("name", "")) in drop.get(page, ()):
            continue
        out.append(r)
    return out


def process_folder(folder_path, zoom: float = 2.0, verbose: bool = True) -> list[dict]:
    """Extracts every split PDF in one university/year folder as a single
    continuous document. Banner detection and content-page filtering still
    run fresh per file (each scan run can crop/position the letterhead
    differently), but header state threads through every file in order."""
    pdfs = find_pdfs_in_folder(folder_path)
    if verbose:
        print(f"[{folder_path}] {len(pdfs)} PDF part(s), in order: {[p.name for p in pdfs]}", file=sys.stderr)

    all_records: list[dict] = []
    state = parser.HeaderState()
    for pdf_path in pdfs:
        if verbose:
            print(f"--- {pdf_path.name} ---", file=sys.stderr)
        records, state = cli.extract_pdf_with_state(
            str(pdf_path), zoom=zoom, verbose=verbose, initial_state=state)
        before = len(records)
        records = drop_repeats_across_pages(records)
        if verbose and len(records) < before:
            print(f"Dropped {before - len(records)} record(s) repeated on a later page "
                  f"(kept at their first appearance)", file=sys.stderr)
        for r in records:
            r["source_file"] = pdf_path.name
        all_records.extend(records)
    return drop_cross_file_duplicates(drop_overlapping_files(all_records))


def process_tree(base_path, out_dir, zoom: float = 2.0, verbose: bool = True):
    """Walks <base_path>/<university>/<year>/ and writes one CSV per leaf
    folder - except a leaf folder that isn't named a bare year (see
    _looks_like_year_folder), where each PDF inside it is extracted as its
    own independent document (no header state carried between them) and
    gets its own CSV, since such a folder holds separate complete
    documents rather than split fragments of one."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    folders = find_year_folders(base_path)
    print(f"Found {len(folders)} folder(s) under {base_path}", file=sys.stderr)

    for folder in folders:
        university = folder.parent.name
        label = folder.name

        if _looks_like_year_folder(label):
            records = process_folder(folder, zoom=zoom, verbose=verbose)
            out_path = out_dir / f"{university}_{label}.csv"
            write_batch_csv(records, str(out_path))
            print(f"[{university}/{label}] wrote {len(records)} record(s) to {out_path}", file=sys.stderr)
        else:
            if verbose:
                print(f"[{university}/{label}] not a year folder - "
                      f"treating each file as an independent document", file=sys.stderr)
            for pdf_path in find_pdfs_in_folder(folder):
                records = cli.extract_pdf(str(pdf_path), zoom=zoom, verbose=verbose)
                for r in records:
                    r["source_file"] = pdf_path.name
                out_path = out_dir / f"{university}_{label}_{pdf_path.stem}.csv"
                write_batch_csv(records, str(out_path))
                print(f"[{university}/{label}] (independent) wrote {len(records)} record(s) "
                      f"from {pdf_path.name} to {out_path}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(
        description="Batch-extract a university/year folder (or a whole "
                     "<root>/<university>/<year> tree) of split convocation-"
                     "booklet PDFs into canonical-schema CSV(s).")
    ap.add_argument("path", help="A single university/year folder, or "
                                  "(with --walk) a root containing "
                                  "<university>/<year>/ subfolders")
    ap.add_argument("-o", "--out", required=True,
                     help="Output CSV path (single-folder mode) or output "
                          "directory (--walk mode)")
    ap.add_argument("--walk", action="store_true",
                     help="Treat `path` as a root and process every "
                          "<university>/<year>/ folder beneath it")
    ap.add_argument("--zoom", type=float, default=2.0)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    verbose = not args.quiet

    if args.walk:
        process_tree(args.path, args.out, zoom=args.zoom, verbose=verbose)
    else:
        if not _looks_like_year_folder(Path(args.path).name):
            print(f"Warning: '{Path(args.path).name}' isn't named a bare year - "
                  f"process_folder carries header state across every PDF in it "
                  f"as if they were one continuous document. If this folder "
                  f"actually holds several independent documents (like "
                  f"COVENANT's 'Degrees' folder, one file per year), run "
                  f"--walk on the root above the university folder instead - "
                  f"it handles a non-year folder like this one safely, file "
                  f"by file.", file=sys.stderr)
        records = process_folder(args.path, zoom=args.zoom, verbose=verbose)
        write_batch_csv(records, args.out)
        print(f"Wrote {len(records)} record(s) to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
