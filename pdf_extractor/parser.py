"""Stateful row builder: walks a page's (or a whole document's) ordered line
stream and turns it into canonical-schema records, tracking header state
(faculty -> department -> course -> degree -> grade -> year) the way a
human reader would - a header line updates the state and every name row
after it inherits that state until the next header changes it.

State-reset rule of thumb, matching the hierarchy observed across every
booklet sampled during the manual exploration phase: changing a
higher-level field invalidates the fields below it (they need to be
re-stated for the new context); changing a lower-level field never
resets anything above it.
"""

import re
from dataclasses import dataclass, field as dc_field

from .classify import classify_line
from .normalize import normalize_grade, normalize_year

NAME_PREFIX_JUNK = re.compile(r"^[\W_]*\d{0,4}[.\)\-]?\s*")

# Some booklets (FUTA's, found during validation against real ground-truth
# data) print each row as "<matric number> <name>" rather than a bare list
# number, e.g. "APH/18/5328 AJAYI Oluwadunsin Oluwafemi". NAME_PREFIX_JUNK
# alone leaves that whole matric number glued onto the name - it only
# strips a run of digits, and a matric number starts with letters. Strip it
# as its own step, after the generic numeric-prefix cleanup (see
# clean_name_text for why the order matters).
#
# The faculty-code part tolerates a stray OCR digit after the first letter
# ("FWT1/15/1797" for "FWT/15/1797" - seen on a real FUTA scan).
_MATRIC_CORE = r"[A-Z][A-Z0-9]{1,5}\s*/\s*\d{2,4}\s*/\s*\d{2,7}"
MATRIC_PREFIX = re.compile(rf"^[\W_]*{_MATRIC_CORE}[\W_]*", re.IGNORECASE)
# Some FUTA pages print the matric number AFTER the name instead
# ("VITALIS Miracle Utibe-Abasi 14 PMT/14/9181"), optionally preceded by a
# list number.
MATRIC_SUFFIX = re.compile(rf"[\s|]+(?:\d{{1,4}}[\s.|]+)?{_MATRIC_CORE}[\W_]*$", re.IGNORECASE)
# A name never ends in digits or stray pipes ("Ekene Uchenna 1.", "Name |").
# Requires an actual digit (or a pipe/underscore) so a legitimate period
# after an initial ("Olusola A.") is left alone.
TRAILING_JUNK = re.compile(r"(?:[\s.|\-_)\]]*\d[\s\d.|\-_)\]]*|[\s|_]+)$")

# All-caps surname fused onto its Capitalised given name with no space
# ("OYEKANMILekan", "ORUMAHCharles"): Tesseract loses a tight word gap on
# some scans. The boundary is before the LAST capital of the run - a run of
# 2+ capitals followed by a lowercase letter means the final capital is
# really the start of the next word.
_FUSED_CAPS_RUN = re.compile(r"(?<=[A-Z]{2})([A-Z])(?=[a-z])")
_CAMEL_HUMP = re.compile(r"(?<=[a-z])(?=[A-Z][a-z])")


def _fix_ocr_confusions(text: str) -> str:
    """Repairs character misreads that are unambiguous in a name context.
    In the sans-serif fonts these booklets use, a capital I, a lowercase l
    and a pipe/exclamation mark are near-identical glyphs, and Tesseract
    picks between them badly - so "AJAYI" comes back as "AJAY!" / "AJAY|",
    and "Iyanuoluwa" as "lyanuoluwa" / "|yanuoluwa".
      - a '!' or '|' closing a word after a capital is a capital I
      - a '!' or '|' after a lowercase letter is a lowercase l
      - a token that STARTS with a lowercase l or a pipe/bang followed by
        lowercase letters is a capitalised name beginning with I (a name
        never starts with a lowercase letter, and a genuine capital L
        doesn't get misread this way)
      - digits embedded inside an otherwise alphabetic word are noise
        (e.g. "OLABAN4JI" for "OLABANJI")."""
    out = []
    for w in text.split(" "):
        # "ADEBIY|Adewale": the pipe is the surname's final I, fused to the
        # next (capitalised) word.
        w = re.sub(r"(?<=[A-Z])[!|](?=[A-Z][a-z])", "I ", w)
        w = re.sub(r"(?<=[A-Z])[!|](?![A-Za-z])", "I", w)
        w = re.sub(r"(?<=[a-z])[!|](?![A-Za-z])", "l", w)
        if len(w) >= 4:
            w = re.sub(r"^[l!|](?=[a-z]{3,})", "I", w)
        if len(re.findall(r"[A-Za-z]", w)) >= 4:
            w = re.sub(r"(?<=[A-Za-z])\d+(?=[A-Za-z])", "", w)
        # Stray punctuation inserted inside a long capitalised surname
        # ("ADEDE.JI", "OWONIY!I") - gated on length so initials ("A.O.")
        # keep their dots.
        if len(re.findall(r"[A-Z]", w)) >= 6:
            w = re.sub(r"(?<=[A-Z])[.!|](?=[A-Z])", "", w)
        out.append(w)
    return " ".join(out)


def _split_fused_words(text: str) -> str:
    t = _FUSED_CAPS_RUN.sub(r" \1", text)
    words = t.split(" ")
    # Given names fused to each other ("AlabaGbemisola") - only touched when
    # the row has the all-caps-surname shape, since a genuine camel-case
    # first word ("McDonald") must be left alone.
    if words and re.fullmatch(r"[A-Z][A-Z'\-]{1,},?", words[0]):
        words = [words[0]] + [_CAMEL_HUMP.sub(" ", w) for w in words[1:]]
    return " ".join(words)


def clean_header_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


# Some booklets (COVENANT's postgraduate lists) print a Gender column next to
# the name. OCR then either emits the bare value as its own "record" (96 of
# them in COVENANT 2024 - a third of that year's over-count) or glues it onto
# the name ("IME Ubong Abasi Ifiok Male"). Neither is ever part of a name.
_GENDER_WORD = re.compile(r"^(?:male|female)[\W_]*$", re.IGNORECASE)

# Other booklets print the same column as a trailing "(F)"/"(M)" marker
# instead of a spelled-out word (UNILORIN's undergraduate lists -
# "ADEWUYI, Oluwatobi Damilola (F)"). OCR also mismatches the bracket pair
# ("(F}", "{F)"), so both are accepted; the letter itself is kept strict to
# F/M (not e.g. the "{T)" seen on a badly garbled row nearby) since a wrong
# guess at what a noisier bracketed letter "really" meant would corrupt the
# name rather than just leave it alone - that residual case is exactly what
# confidence.py's own _GENDER_MARK is for: flag it instead of guessing.
#
# This was visible in a real sample (UNILORIN 2018, found while chasing
# missing pages) well before it got fixed here - confidence.py's flag for
# it existed first and was silently carrying the load; this is the actual
# fix, not just the detector.
_GENDER_MARK_TRAILING = re.compile(r"[\(\{]\s*[FfMm]\s*[\)\}]\s*$")


def _strip_gender_column(t: str) -> str:
    t = _GENDER_MARK_TRAILING.sub("", t).rstrip()
    words = t.split()
    while words and _GENDER_WORD.match(words[-1]):
        words.pop()
    while words and _GENDER_WORD.match(words[0]):
        words.pop(0)
    return " ".join(words)


# Lines that are page furniture, not people: a "Supervisor:" / "Thesis:" label,
# the professor named after it, an empty-category placeholder ("NIL" under a
# grade with no graduates). UI's Ph.D pages print all of these between
# graduands and they came out as ~1 in 20 of that year's non-matching
# records. Anything ending in ":" is a label, never a name.
_NON_NAME = re.compile(
    r"^(?:nil|n/?a|none)\.?$"                       # empty-category placeholder
    r"|^(?:supervisors?|thesis|title|topic|bervisor)\b"   # labels (incl. an OCR'd "Supervisor")
    r"|^(?:prof|professor|dr)\b\.?"                   # a supervisor's title; a graduand's never starts with one
    r"|:\s*$",                                       # any trailing-colon label
    re.IGNORECASE)


def is_non_name(name: str) -> bool:
    return bool(_NON_NAME.search(name.strip()))


def clean_name_text(text: str) -> str:
    # Order matters: a line can carry BOTH a leading list number and a
    # matric number ("1 AGE/18/6529 SHITTU ...", the "1" a stray/repeated
    # OCR list marker) - NAME_PREFIX_JUNK's leading-digit strip has to run
    # first, or MATRIC_PREFIX's anchor never reaches the matric number
    # because \W (its allowed lead-in) doesn't match a digit.
    t = NAME_PREFIX_JUNK.sub("", text.strip(), count=1)
    t = MATRIC_PREFIX.sub("", t, count=1)
    t = MATRIC_SUFFIX.sub("", t, count=1)
    t = TRAILING_JUNK.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = _strip_gender_column(t)
    return _split_fused_words(_fix_ocr_confusions(t))


@dataclass
class HeaderState:
    faculty: str = ""
    department: str = ""
    course: str = ""
    degree: str = ""
    grade: str = ""
    distinction_notes: str = ""
    year: str = ""
    year_note: str = ""
    programme: str = ""
    affiliated_institution: str = ""
    country: str = ""

    def apply_header(self, field_type: str, raw_text: str, canonical_value: str | None):
        text_for_field = raw_text

        # Header lines routinely combine a field with an embedded session
        # year on the same line ("DEPARTMENT OF POLITICAL SCIENCE 2001/2002")
        # - pull that out as a year update regardless of the line's primary
        # field, and strip it from the text used for that field's own value.
        if field_type != "year":
            embedded_year = re.search(r"(?:19|20)\d{2}\s*[/\-]\s*(?:19|20)\d{2}", raw_text)
            if embedded_year:
                y = normalize_year(embedded_year.group(0))
                if y["flag"] == "ok":
                    self.year = y["canonical"]
                    self.year_note = y["note"]
                text_for_field = (raw_text[:embedded_year.start()] + raw_text[embedded_year.end():]).strip()

        if field_type == "faculty":
            self.faculty = clean_header_text(text_for_field)
            self.department = self.course = self.degree = self.grade = self.distinction_notes = ""
        elif field_type == "department":
            self.department = clean_header_text(text_for_field)
            self.course = self.grade = self.distinction_notes = ""
        elif field_type == "department_or_course":
            text = clean_header_text(text_for_field)
            if not self.department:
                self.department = text
            else:
                self.course = text
            self.grade = self.distinction_notes = ""
        elif field_type == "degree":
            self.degree = clean_header_text(text_for_field)
            self.grade = self.distinction_notes = ""
        elif field_type == "grade":
            self.grade = canonical_value or ""
            g = normalize_grade(raw_text)
            self.distinction_notes = g["notes"]
        elif field_type == "year":
            y = normalize_year(raw_text)
            self.year = y["canonical"]
            self.year_note = y["note"]
        elif field_type == "affiliated_institution":
            self.affiliated_institution = clean_header_text(text_for_field)
        elif field_type == "programme":
            self.programme = clean_header_text(text_for_field)


CANON_FIELDS = ["name", "faculty", "department", "course", "degree", "grade",
                "distinction_notes", "year", "year_note", "page", "programme",
                "affiliated_institution", "country"]


def parse_lines_to_records(lines, page_number, initial_state: HeaderState | None = None,
                            page_meta: dict | None = None) -> tuple[list[dict], HeaderState]:
    """Converts an ordered line stream (from columns.reconstruct_reading_order)
    into canonical-schema record dicts, carrying header state across the
    page. Returns (records, final_state) so the caller can carry state
    forward into the next page of the same document.

    `page_meta` (from cli._process_page: pdf_path, page_index, rotated_by,
    dewarped, zoom, image_size) is folded together with each line's own
    bounding box/OCR confidence/repair flag into a private "_geom" key on
    every record - not part of the canonical schema (write_csv/
    write_batch_csv drop it), but exactly what review.py needs later to
    score confidence and re-render a snapshot of the source page around
    this one row, without having to keep every rendered page image around."""
    state = initial_state or HeaderState()
    records = []

    for line in lines:
        cls = classify_line(line.text)
        if cls["is_header"]:
            state.apply_header(cls["field"], line.text, cls.get("value"))
            continue

        name = clean_name_text(line.text)
        # Filter obvious noise: too short, no letters, or just a stray
        # page-number/footer digit that leaked into the column.
        if len(name) < 3 or not re.search(r"[A-Za-z]{2,}", name) or is_non_name(name):
            continue

        record = {
            "name": name,
            "faculty": state.faculty,
            "department": state.department,
            "course": state.course or state.department,
            "degree": state.degree,
            "grade": state.grade,
            "distinction_notes": state.distinction_notes,
            "year": state.year,
            "year_note": state.year_note,
            "page": page_number,
            "programme": state.programme,
            "affiliated_institution": state.affiliated_institution,
            "country": state.country,
            "_geom": {
                "top": line.top, "bottom": line.bottom, "left": line.left, "right": line.right,
                "conf": line.conf, "repaired": line.repaired,
                **(page_meta or {}),
            },
        }
        records.append(record)

    return records, state
