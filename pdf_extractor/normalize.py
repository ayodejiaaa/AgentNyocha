"""Python port of normalize_grades.js / normalize_years.js (schema/ folder,
built during the training-data cleanup). Kept logically identical on
purpose: this is the same canonical-value contract the training data was
built against, now reused for live extraction output rather than
retrofitted onto already-OCR'd text.
"""

import re

CANON_GRADE = {
    "FIRST": "First Class Honours",
    "SECOND_UPPER": "Second Class Honours (Upper Division)",
    "SECOND_LOWER": "Second Class Honours (Lower Division)",
    "THIRD": "Third Class Honours",
    "PASS": "Pass",
    "DISTINCTION": "Distinction",
    "MERIT": "Merit",
    "UPPER_CREDIT": "Upper Credit",
    "LOWER_CREDIT": "Lower Credit",
    "CREDIT": "Credit",
    "AEGROTAT": "Aegrotat",
    "HONOURS": "Honours",
    "NA": "N/A",
}


def _extract_distinction_notes(s: str):
    notes = []
    pattern = re.compile(r"distinction\s+in\s+([^,;]+)", re.IGNORECASE)
    for m in pattern.finditer(s):
        notes.append(re.sub(r"\s+", " ", m.group(1).strip()))
    stripped = pattern.sub("", s)
    stripped = re.sub(r"[,;]", " ", stripped)
    return stripped, notes


def normalize_grade(raw_value: str) -> dict:
    original = raw_value
    s = (raw_value or "").strip()

    if s == "":
        return {"canonical": "", "notes": "", "flag": "blank"}
    if re.match(r"^-?\d{4,}$", s):
        return {"canonical": "", "notes": original, "flag": "GARBAGE_NUMERIC"}
    if re.match(r"^[^A-Za-z0-9]+$", s):
        return {"canonical": "", "notes": original, "flag": "GARBAGE_SYMBOLS"}
    if re.match(r"^[A-Za-z]$", s):
        return {"canonical": "", "notes": original, "flag": "GARBAGE_SINGLE_CHAR"}
    if re.match(r"^N\s*/?\s*A\.?$", s, re.IGNORECASE):
        return {"canonical": CANON_GRADE["NA"], "notes": "", "flag": "ok"}

    s = re.sub(r"^[\W_]+(?=[A-Za-z])", "", s, count=1)
    stripped, distinction_notes = _extract_distinction_notes(s)
    base = re.sub(r"\s+", " ", stripped.strip())
    base_is_emptyish = re.sub(r"[^A-Za-z]", "", base) == ""

    if re.search(r"AEGROTAT", base, re.IGNORECASE):
        return {"canonical": CANON_GRADE["AEGROTAT"], "notes": "; ".join(distinction_notes), "flag": "ok"}

    norm = base.upper()
    subs = [
        (r"\bTHRID\b", "THIRD"), (r"\bSECON\b", "SECOND"), (r"\bCL\s+ASS\b", "CLASS"),
        (r"\bLIPPER\b", "UPPER"), (r"\bLOWOR\b", "LOWER"), (r"\bUPPPER\b", "UPPER"),
        (r"\bOWER\b", "LOWER"), (r"([A-Z])(UPPER|LOWER|DIVISION)\b", r"\1 \2"),
        (r"\b1ST\b", "FIRST"), (r"\b2ND\b", "SECOND"), (r"\b3RD\b", "THIRD"),
        (r"HON\.?OU?RS?\b", "HONOURS"), (r"HONOURSS", "HONOURS"),
        (r"DIVIS[OI0]NS?", "DIVISION"), (r"DM?SION", "DIVISION"), (r"D[EI]VISION", "DIVISION"),
        (r"CLAS{1,3}\b", "CLASS"), (r"\bLASS\b", "CLASS"),
        (r"SEC[O0]U?ND\s*CLASS", "SECOND CLASS"), (r"SEC[O0]U?ND", "SECOND"),
        (r"THIRDCLASS", "THIRD CLASS"), (r"SECONDCLASS", "SECOND CLASS"),
        (r"THR?I?RD", "THIRD"), (r"UPPE?RE?R?\b", "UPPER"), (r"L[O0]WE?RE?R?\b", "LOWER"),
        (r"DIST[I1]C?TION", "DISTINCTION"), (r"[.:,;'\"()\[\]{}]", " "), (r"\s+", " "),
    ]
    for pat, repl in subs:
        norm = re.sub(pat, repl, norm)
    norm = norm.strip()

    has_first = bool(re.search(r"\bFIRST\b", norm))
    has_second = bool(re.search(r"\bSECOND\b", norm))
    has_third = bool(re.search(r"\bTHIRD\b", norm))
    has_upper = bool(re.search(r"\bUPPER\b", norm))
    has_lower = bool(re.search(r"\bLOWER\b", norm))
    has_pass = bool(re.search(r"\bPASS(ED)?\b", norm))
    has_distinction_word = bool(re.search(r"\bDISTINCTION\b", norm))
    has_merit = bool(re.search(r"\bMERIT\b", norm))
    has_credit = bool(re.search(r"\bCREDIT\b", norm))
    has_honours = bool(re.search(r"\bHONOURS\b", norm))
    has_class = bool(re.search(r"\bCLASS\b", norm))
    has_degree = bool(re.search(r"\bDEGREE\b", norm) or re.search(r"MB\s*[;:]?\s*BS", norm))
    has_any_class_keyword = has_first or has_second or has_third or has_pass or has_merit or has_credit or has_degree
    leftover_is_just_noise = all(len(tok) <= 2 for tok in norm.split(" ") if tok)

    canonical = None
    if has_second and (has_class or has_honours):
        canonical = CANON_GRADE["SECOND_UPPER"] if has_upper else (CANON_GRADE["SECOND_LOWER"] if has_lower else "Second Class Honours (Division unspecified)")
    elif has_first and (has_class or has_honours):
        canonical = CANON_GRADE["FIRST"]
    elif has_third and (has_class or has_honours or norm == "THIRD"):
        canonical = CANON_GRADE["THIRD"]
    elif has_credit and has_upper:
        canonical = CANON_GRADE["UPPER_CREDIT"]
    elif has_credit and has_lower:
        canonical = CANON_GRADE["LOWER_CREDIT"]
    elif has_credit:
        canonical = CANON_GRADE["CREDIT"]
    elif has_merit:
        canonical = CANON_GRADE["MERIT"]
    elif has_pass:
        canonical = CANON_GRADE["PASS"]
    elif has_degree:
        canonical = CANON_GRADE["NA"]
    elif not has_any_class_keyword and (has_upper or has_lower):
        canonical = CANON_GRADE["SECOND_UPPER"] if has_upper else CANON_GRADE["SECOND_LOWER"]
    elif has_distinction_word:
        canonical = CANON_GRADE["DISTINCTION"]
    elif distinction_notes and not has_any_class_keyword and leftover_is_just_noise:
        canonical = CANON_GRADE["DISTINCTION"]
    elif base_is_emptyish and distinction_notes:
        canonical = CANON_GRADE["DISTINCTION"]
    elif has_honours and not has_any_class_keyword and re.sub(r"HONOURS", "", norm).strip() == "":
        canonical = CANON_GRADE["HONOURS"]

    if not canonical:
        return {"canonical": "", "notes": "; ".join(distinction_notes), "flag": "NEEDS_REVIEW", "normalized_attempt": norm}
    return {"canonical": canonical, "notes": "; ".join(distinction_notes), "flag": "ok"}


def _fix_ocr_digits(s: str) -> str:
    def repl(m):
        token = m.group(0)
        return token.replace("O", "0") if re.search(r"\d", token) else token
    return re.sub(r"\b(\d*[0O]\d*[0O]?\d*)\b", repl, s)


def normalize_year(raw_value: str) -> dict:
    s = (raw_value or "").strip()
    if s == "":
        return {"canonical": "", "note": "", "flag": "blank"}
    if re.match(r"^N\s*/?\s*A\.?$", s, re.IGNORECASE):
        return {"canonical": "N/A", "note": "", "flag": "ok"}

    s = _fix_ocr_digits(s)
    note = ""
    sem_match = re.search(r"\b(rain|harmattan|first|second)\s+semester\b", s, re.IGNORECASE)
    if sem_match:
        note = " ".join(w.capitalize() for w in re.sub(r"\s+", " ", sem_match.group(0)).split(" "))

    if re.match(r"^\d{5,6}$", s):
        return {"canonical": "", "note": note, "flag": "CORRUPTED_DATE_SERIAL"}

    pair_match = re.search(r"(19|20)\d{2}\s*[/\-]\s*(19|20)\d{2}", s)
    if pair_match:
        nums = re.findall(r"(?:19|20)\d{2}", pair_match.group(0))
        y1, y2 = int(nums[0]), int(nums[1])
        if y2 < y1:
            return {"canonical": f"{y1}/{y2}", "note": note, "flag": "NEEDS_REVIEW_REVERSED"}
        gap = y2 - y1
        flag = "ok" if gap <= 2 else "NEEDS_REVIEW_WIDE_GAP"
        return {"canonical": f"{y1}/{y2}", "note": note, "flag": flag}

    short_pair = re.search(r"\b(\d{2})\s*/\s*(\d{2})\b", s)
    if short_pair:
        def full_year(yy):
            n = int(yy)
            return 2000 + n if n < 50 else 1900 + n
        y1, y2 = full_year(short_pair.group(1)), full_year(short_pair.group(2))
        flag = "ok" if y2 == y1 + 1 else "NEEDS_REVIEW_WIDE_GAP"
        return {"canonical": f"{y1}/{y2}", "note": note, "flag": flag}

    single_match = re.search(r"(?:19|20)\d{2}", s)
    if single_match:
        return {"canonical": single_match.group(0), "note": note, "flag": "ok-single-year"}

    return {"canonical": "", "note": note, "flag": "NO_YEAR_IN_SOURCE"}
