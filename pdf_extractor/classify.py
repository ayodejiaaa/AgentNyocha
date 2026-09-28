"""Line classification: is this a header line (faculty/department/course/
degree/grade/year banner) or a name/data row? And if a header, which field
does it update?

This matters for reading order, not just for the eventual field parser: a
page can hold several short, independent sections stacked vertically (e.g.
two departments each with their own tiny left/right sub-columns), and in
that layout "read all of column 0, then all of column 1" is wrong - it
would interleave one department's data with another's. Detecting header
lines lets the reading-order step treat each header-to-header span as its
own section and interleave columns within that span only.
"""

import json
import re
from pathlib import Path

from .normalize import normalize_grade, normalize_year

_VOCAB_PATH = Path(__file__).parent / "vocab.json"
_TOKEN_RE = re.compile(r"[A-Z]{2,}")


def _load_vocab() -> dict:
    """Loads the two word lists mined from the 535k-row canonical training
    corpus (see build_vocab.py): words that occur almost exclusively in
    institutional fields (faculty/department/course/degree/grade) versus
    words that occur almost exclusively in real people's names. This is an
    empirical substitute for guessing "what does a name look like" -
    Nigerian given/surnames don't follow English naming conventions, but
    the corpus already has hundreds of thousands of real examples."""
    if not _VOCAB_PATH.exists():
        return {"institutional_words": {}, "name_words": {}}
    with open(_VOCAB_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


_VOCAB = _load_vocab()
_INSTITUTIONAL_WORDS = _VOCAB["institutional_words"]
_NAME_WORDS = _VOCAB["name_words"]


def vocab_score(text: str) -> tuple[int, int]:
    """Counts words in text that match the institutional vocabulary vs. the
    name vocabulary. Returns (institutional_hits, name_hits)."""
    tokens = _TOKEN_RE.findall(text.upper())
    institutional_hits = sum(1 for tok in tokens if tok in _INSTITUTIONAL_WORDS)
    name_hits = sum(1 for tok in tokens if tok in _NAME_WORDS)
    return institutional_hits, name_hits

GRADE_KEYWORDS = re.compile(
    r"\b(FIRST|SECOND|THIRD)\s+CLASS\b|\bCLASS\s+HONOURS\b|\bHONOURS\b|\bPASS\b|"
    r"\bDISTINCTION\b|\bMERIT\b|\bCREDIT\b|\bAEGROTAT\b",
    re.IGNORECASE,
)
YEAR_PATTERN = re.compile(
    r"(19|20)\d{2}\s*[/\-]\s*(19|20)\d{2}|\b(19|20)\d{2}\b|"
    r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\w*\s+(19|20)\d{2}\b|"
    r"\bSESSION\b",
    re.IGNORECASE,
)
DEGREE_PATTERN = re.compile(
    r"\bBACHELOR\s+OF\b|\bMASTER\s+OF\b|\bDOCTOR\s+OF\b|"
    # Abbreviated form, e.g. "B.Sc. (Architecture)" or "B. Sc. (Architecture)"
    # - OCR/printing is inconsistent about a space between the degree letter
    # and the discipline abbreviation (and around each period), so allow
    # free spacing/periods between them rather than requiring them adjacent.
    r"\b[BMD]\s*\.?\s*(?:SC|A|ED|ENG|PHIL|ARCH|CH\s*\.?\s*D|AGRIC|TECH|LAWS?)\s*\.?\s*\(|"
    r"\bLL\s*\.?\s*[BM]\s*\.?\s*\(|"
    r"\bMBA\b|\bHND\b|\bOND\b|"
    r"\bDEGREE\b|\bPGD\b|\bPOSTGRADUATE\s+DIPLOMA\b",
    re.IGNORECASE,
)
FACULTY_PATTERN = re.compile(r"\bFACULTY\s+OF\b|\bCOLLEGE\s+OF\b|\bSCHOOL\s+OF\b", re.IGNORECASE)
DEPARTMENT_PATTERN = re.compile(r"\bDEPARTMENT\s+OF\b", re.IGNORECASE)

# Affiliated colleges/seminaries the main institution's degree is issued
# through (e.g. "ARCHBISHOP VINING COLLEGE OF THEOLOGY, AKURE",
# "BIGARD MEMORIAL SEMINARY, ENUGU") - checked before FACULTY_PATTERN
# because "COLLEGE OF EDUCATION/THEOLOGY" would otherwise be swallowed by
# its generic "COLLEGE OF" branch as if it were a faculty of the main
# institution rather than a separate affiliated one.
AFFILIATED_INSTITUTION_PATTERN = re.compile(
    r"\bAFFILIAT(?:E|ED|ION|ES)\b|\bSEMINARY\b|\bTHEOLOG(?:Y|ICAL)\b|"
    r"\bTRAINING\s+(?:CENTRE|CENTER|SCHOOL)\b|"
    r"\bCOLLEGE\s+OF\s+(?:EDUCATION|PRIMARY\s+EDUCATION|THEOLOGY)\b",
    re.IGNORECASE,
)

# Mode of study, e.g. "(Full Time)", "(Part-Time) - Supplementary",
# "ADDENDUM (Sandwich)". Checked after year (a programme line often also
# carries an embedded session year, which apply_header extracts regardless
# of the line's primary field) so a year-shaped line isn't mis-routed here.
PROGRAMME_PATTERN = re.compile(
    r"\bFULL[\s-]*TIME\b|\bPART[\s-]*TIME\b|\bSANDWICH\b|\bDISTANCE\s+LEARNING\b|"
    r"\bSUPPLEMENTARY\b|\bADDENDUM\b",
    re.IGNORECASE,
)

# A name row's strongest tell isn't its exact shape (surnames run one to
# three words, with or without a comma before the given names) - it's that
# header/department/course banners are printed in solid capitals, while a
# name row almost always has a comma (surname, given names) or at least one
# properly-cased given name (Muritala, Olagoke) even when the surname
# itself is capitalized.
NAME_ROW_PATTERN = re.compile(r",|[a-z]{2,}")


def classify_line(text: str) -> dict:
    """Returns {'is_header': bool, 'field': str|None, 'value': str|None}.
    field is one of 'grade', 'year', 'degree', 'faculty', 'department',
    or None for an unrecognized header-ish line / a name row.
    """
    t = text.strip()
    if not t:
        return {"is_header": False, "field": None, "value": None}

    if AFFILIATED_INSTITUTION_PATTERN.search(t):
        return {"is_header": True, "field": "affiliated_institution", "value": t}
    if FACULTY_PATTERN.search(t):
        return {"is_header": True, "field": "faculty", "value": t}
    if DEPARTMENT_PATTERN.search(t):
        return {"is_header": True, "field": "department", "value": t}

    grade_res = normalize_grade(t)
    if grade_res["flag"] == "ok" and GRADE_KEYWORDS.search(t):
        return {"is_header": True, "field": "grade", "value": grade_res["canonical"]}

    if DEGREE_PATTERN.search(t):
        return {"is_header": True, "field": "degree", "value": t}

    year_res = normalize_year(t)
    if year_res["flag"] in ("ok", "ok-single-year") and YEAR_PATTERN.search(t):
        return {"is_header": True, "field": "year", "value": year_res["canonical"]}

    if PROGRAMME_PATTERN.search(t):
        return {"is_header": True, "field": "programme", "value": t}

    # No keyword pattern fired - this is the genuinely ambiguous case (an
    # all-caps department/course banner we have no regex for, vs. an
    # all-caps name with no comma). Rather than guess from shape, check
    # which vocabulary the line's words actually belong to: institutional
    # terms (DEPARTMENT, ENGINEERING, ADMINISTRATION, ...) essentially never
    # appear in a person's name, and real Nigerian given/surnames
    # (OLUWASEUN, CHIDINMA, FAVOUR, ...) essentially never appear in course/
    # department text - both mined empirically from 535k real rows.
    institutional_hits, name_hits = vocab_score(t)
    if institutional_hits > name_hits:
        return {"is_header": True, "field": "department_or_course", "value": t}
    if name_hits > institutional_hits:
        return {"is_header": False, "field": None, "value": t}

    # Vocab was a tie (usually 0-0: a line with no recognizable word at all,
    # e.g. mostly OCR noise or a name not seen in the training corpus) -
    # fall back to shape as a last resort.
    looks_like_name = bool(NAME_ROW_PATTERN.search(t))
    is_shouty = t.upper() == t and re.search(r"[A-Z]{3,}", t) and not looks_like_name
    if is_shouty and len(t) < 80:
        return {"is_header": True, "field": "department_or_course", "value": t}

    return {"is_header": False, "field": None, "value": t}


def is_header_line(text: str) -> bool:
    return classify_line(text)["is_header"]
