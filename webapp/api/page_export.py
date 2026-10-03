"""Page images for report assembly. A client's report is built by pulling in,
for each person, the image of the booklet page they're on - so each page has to
be saved as its own image and named by the printed page number the report looks
it up by. This module decides the names, says which pages aren't ready, and
packs the zip; main.py serves it.

The page number a page is filed under is the one its RECORDS now carry, not what
extraction first read: a DES who corrects an uncertain page number does it on the
record (that is the only place the review screen lets them), so reading it from
there is what makes a correction reach the exported image.

The zip is only ever built when every page has a confirmed number (see
export_status). A report that silently lacks a page, or has two pages competing
for one name, is worse than no zip.

Nothing here touches the database or storage itself - the caller hands in plain
rows and a function that fetches image bytes - so all of it can be tested without
either.
"""

import csv
import io
import json
import re
import tempfile
import zipfile
from collections import Counter

# What extraction puts in a record's page column when it couldn't read the
# number: "idx" plus the physical page ("idx5", or "idx5a" for half of a spread).
_PLACEHOLDER = re.compile(r"^idx\d+[a-z]?$", re.IGNORECASE)

ALLOWED_PATTERN_FIELDS = ("page", "slug", "university", "year")

DEFAULT_PATTERN = "{slug}-{year}-{page}"

# The short name each university carries in the page image file names, as
# found in the sample files Akowe's report assembly already uses (e.g.
# "covenant-university-2022-28", "unibadan-2016-152", "futa-2024-100"). It is
# NOT derivable from the university name - UI is "unibadan", not
# "university-of-ibadan", the way its ground-truth CSVs are named - hence a
# table. Keys are matched ignoring case, spaces and punctuation. Names not listed here
# fall back to the university as typed, lower-cased with hyphens (see slug_for);
# the entries marked "assumed" have no sample file to confirm them. Extend or
# override from config.PAGE_IMAGE_SLUGS.
UNIVERSITY_SLUGS = {
    "covenant": "covenant-university",
    "covenant university": "covenant-university",
    "futa": "futa",
    "ui": "unibadan",
    "university of ibadan": "unibadan",
    "unibadan": "unibadan",
    "lasu": "lasu",            # assumed
    "oau": "oau",              # assumed
    "oou": "oou",              # its postgraduate booklet follows the unilag-pgs pattern (confirmed); the name itself assumed
    "unilag": "unilag",        # unilag-2019-102 (undergraduate) / unilag-pgs-2019-102 (postgraduate), from the samples
    "unilorin": "unilorin",    # assumed
}


def _norm(text: str) -> str:
    """For matching a university name: letters and digits only, so spacing and
    punctuation ("F.U.T.A.", "Covenant-University", "CovenantUniversity") never
    stop two spellings of the same name matching."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


# "UNILAG PG", "UNILAG (Postgraduate)", "Unilag PGS": a postgraduate booklet. The
# sample files name them "unilag-pgs-2019-102" beside the undergraduate
# "unilag-2019-102", so the postgraduate marker is taken off before the
# university is looked up and "-pgs" is put back after its short name.
_POSTGRAD = re.compile(r"(?<![a-z])(?:pgs?|post[\s._\-]*grad(?:uates?)?)(?![a-z])", re.IGNORECASE)
_UNDERGRAD = re.compile(r"(?<![a-z])(?:ugs?|under[\s._\-]*grad(?:uates?)?)(?![a-z])", re.IGNORECASE)


def slug_for(university: str, overrides: dict | None = None) -> str:
    """The short name a university carries in image file names, with "-pgs"
    after it for a postgraduate booklet."""
    text = university or ""
    postgrad = bool(_POSTGRAD.search(text))
    text = _UNDERGRAD.sub(" ", _POSTGRAD.sub(" ", text))   # undergraduate is the unmarked case
    key = _norm(text)
    table = {**{_norm(k): v for k, v in UNIVERSITY_SLUGS.items()}, **{_norm(k): v for k, v in (overrides or {}).items()}}
    slug = table[key] if key in table else (re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "booklet")
    return f"{slug}-pgs" if postgrad else slug


def year_for(year: str) -> str:
    """The four-digit year for a file name, from whatever was typed at upload
    ("2022", "2022 (after fix)", "2021/2022" -> the first one). Falls back to
    the text itself if it holds no year at all."""
    m = re.search(r"(?:19|20)\d{2}", year or "")
    return m.group(0) if m else (year or "").strip()


def normalize_page_number(text: str) -> str | None:
    """A page number a person typed, cleaned up ("045" -> "45"), or None if it
    isn't one. These booklets never run to a thousand pages, so a page number is
    1 to 3 digits - which also keeps a printed year, or a stray "idx3"
    placeholder, from ever being entered as one."""
    text = (text or "").strip()
    if not re.fullmatch(r"\d{1,3}", text) or int(text) == 0:
        return None
    return str(int(text))


def is_resolved(label: str) -> bool:
    label = (label or "").strip()
    return bool(label) and not _PLACEHOLDER.match(label)


def page_label(record_pages: list[str]) -> tuple[str, int]:
    """The printed page number a physical page should be filed under, from the
    page values of the records on it, and how many of them disagree with it.
    The most common real value wins (a DES may have corrected only some of the
    rows); placeholders and blanks never count. ("", 0) if none is real."""
    real = [p.strip() for p in record_pages if is_resolved(p)]
    if not real:
        return "", 0
    counts = Counter(real)
    top = max(counts.values())
    label = next(p for p in real if counts[p] == top)   # a tie goes to the one seen first
    return label, len(real) - counts[label]


def safe_filename(name: str) -> str:
    """A name safe on any filesystem and inside a zip: no path separators or
    reserved characters, no leading/trailing dots or spaces."""
    cleaned = re.sub(r"[^A-Za-z0-9._ \-()]+", "_", name).strip(" .")
    return cleaned or "page"


def render_name(pattern: str, **fields: str) -> str:
    """Fills a naming pattern ("{slug}-{year}-{page}" ...). Only
    ALLOWED_PATTERN_FIELDS may appear; anything else is a configuration mistake
    and raises ValueError saying so, rather than quietly producing wrongly named
    files that the report would then fail to find."""
    try:
        return pattern.format(**{k: fields[k] for k in ALLOWED_PATTERN_FIELDS})
    except (KeyError, IndexError, ValueError) as e:
        raise ValueError(f"invalid page image name pattern {pattern!r} ({e!r}); "
                         f"it may use only {', '.join('{' + f + '}' for f in ALLOWED_PATTERN_FIELDS)}") from e


def _half_suffix(spread_half) -> str:
    return "" if spread_half is None else "ab"[spread_half] if spread_half in (0, 1) else str(spread_half)


def describe_page(page: dict) -> str:
    """Where a page is in the upload, the way a person would look for it."""
    return f"page {page['page_index'] + 1}{_half_suffix(page['spread_half'])} of {page['source_file']}"


def plan_page_files(job: dict, pages: list[dict], records_by_page: dict[int, list[dict]], pattern: str,
                    slug_overrides: dict | None = None) -> list[dict]:
    """One entry per page that has records, in booklet order, each saying what
    its file will be called and whether that can be trusted:

      status "ok"          filed under the page number its records agree on
      status "conflict"    its records disagree about the number (some rows were
                           corrected and others not) - the majority is used,
                           but a person should make them agree
      status "duplicate"   another page already took this number; a "-2", "-3"...
                           was added so nothing is overwritten - one of the two
                           is wrong ("clashes_with" says which page has it)
      status "unresolved"  no real page number on any record yet; named
                           UNRESOLVED_... so it can't be mistaken for a numbered
                           page

    Only "ok" pages are fit to export - see export_status."""
    slug, year = slug_for(job["university"], slug_overrides), year_for(job["year"])
    ordered = sorted(pages, key=lambda p: (p["file_order"], p["page_index"], p["spread_half"] or 0))
    taken: dict[str, dict] = {}
    plan = []
    for page in ordered:
        records = records_by_page.get(page["id"], [])
        if not records:
            continue   # nothing on it for a report to look up (e.g. every name was a re-print)
        label, conflicts = page_label([r["page"] for r in records])
        if label:
            base = safe_filename(render_name(pattern, page=label, slug=slug, university=job["university"], year=year))
            status = "conflict" if conflicts else "ok"
        else:
            base = f"UNRESOLVED_file{page['file_order'] + 1}_page{page['page_index'] + 1}{_half_suffix(page['spread_half'])}"
            status = "unresolved"
        name, n, clashes_with = base, 1, None
        while name.lower() in taken:
            clashes_with = clashes_with or taken[name.lower()]
            n += 1
            name = f"{base}-{n}"
        if n > 1 and status in ("ok", "conflict"):
            status = "duplicate"
        taken[name.lower()] = page
        plan.append({"filename": f"{name}.png", "page": page, "label": label, "status": status,
                     "records": len(records), "conflicts": conflicts,
                     "clashes_with": describe_page(clashes_with) if clashes_with else None})
    return plan


def _seen_numbers(page: dict) -> list[dict]:
    """The numbers a scan showed in its corners, as stored: [{"side", "value"}]."""
    try:
        seen = json.loads(page.get("seen_numbers") or "[]")
    except ValueError:
        return []
    return [s for s in seen if isinstance(s, dict) and "side" in s and "value" in s] if isinstance(seen, list) else []


def _seen_text(page: dict) -> str:
    return " and ".join(f"{s['value']} in the {s['side']} corner" for s in _seen_numbers(page))


def _unresolved_text(e: dict) -> str:
    page, note = e["page"], e["page"].get("page_note")
    suggested = page.get("suggested_page")
    hint = f" The booklet's sequence points to {suggested}." if suggested else ""
    if note == "two_numbers":
        return (f"This scan shows two different page numbers ({_seen_text(page)}) - it runs into the "
                f"neighbouring page. Check which one is this page's own.{hint}")
    if note == "neighbour_number":
        return (f"The only number read ({_seen_text(page)}) is in the corner this booklet doesn't keep its "
                f"numbers in, so it is probably the neighbouring page's. This page's own number wasn't read.{hint}")
    return "No page number has been confirmed for this page yet."


_PROBLEM_TEXT = {
    "unresolved": _unresolved_text,
    "duplicate": lambda e: f"Number {e['label']} is also used by {e['clashes_with']} - one of them is wrong.",
    "conflict": lambda e: (f"Its rows don't agree on the number: {e['conflicts']} of {e['records']} "
                           f"differ from {e['label']}."),
}


def export_status(plan: list[dict]) -> dict:
    """Whether the page images can be downloaded, and if not, what's in the way.
    Every page must be filed under exactly one confirmed number: a page with none,
    a number two pages both claim, or rows that disagree about it each hold the
    download back. "problems" lists them for the review screen."""
    problems = [{"page_id": e["page"]["id"], "kind": e["status"], "where": describe_page(e["page"]),
                 "label": e["label"], "records": e["records"], "detail": _PROBLEM_TEXT[e["status"]](e),
                 "number_image": e["page"].get("number_image_key"),   # what the reviewer checks the number against
                 "note": e["page"].get("page_note") or "", "seen": _seen_numbers(e["page"]),
                 "suggested": e["page"].get("suggested_page") or ""}   # prefilled for the reviewer, never applied
                for e in plan if e["status"] != "ok"]
    return {"pages": len(plan), "ready": bool(plan) and not problems, "problems": problems}


class MissingPageImage(Exception):
    """A page's image can't be read from storage."""


def build_zip(plan: list[dict], get_bytes):
    """Writes every planned page, plus manifest.csv (one row per file: what it
    was filed under and where it came from), into a temporary file and returns
    it rewound, ready to read; the caller closes it. Only for a plan that
    export_status says is ready. An image that can't be read raises
    MissingPageImage rather than being left out - a report built from a zip with
    a page quietly missing is worse than one that fails and says why. Disk, not
    memory: a 100-page booklet is hundreds of megabytes."""
    tmp = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
    manifest = io.StringIO()
    writer = csv.writer(manifest)
    writer.writerow(["file", "page_number", "source_file", "physical_page", "records"])
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:   # PNG is already compressed
            for entry in plan:
                page = entry["page"]
                try:
                    data = get_bytes(page["image_key"])
                except Exception as e:   # noqa: BLE001 - any storage failure means the same thing here
                    raise MissingPageImage(f"The image for {describe_page(page)} could not be read "
                                           f"({type(e).__name__}).") from e
                z.writestr(entry["filename"], data)
                writer.writerow([entry["filename"], entry["label"], page["source_file"],
                                 f"{page['page_index'] + 1}{_half_suffix(page['spread_half'])}", entry["records"]])
            z.writestr("manifest.csv", manifest.getvalue())
    except BaseException:
        tmp.close()
        raise
    tmp.seek(0)
    return tmp
