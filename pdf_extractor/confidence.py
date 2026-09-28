"""Per-record confidence scoring for human review, not correction.

This never rewrites extracted text - it only labels each record HIGH /
MEDIUM / LOW with a short reason, for a Data Entry Specialist to check
quickly against the source page (see review.py, which turns a LOW/MEDIUM
row into an actual PDF snapshot next to it).

Deliberately uses no ground truth: on a brand-new PDF there is none to
check against, so every signal here has to be something the tool can
judge about its own output at extraction time - Tesseract's own word
confidence, whether the text looks like a real name (matched against the
name vocabulary mined from the training corpus - used only to flag
"doesn't look familiar", never to rewrite toward a familiar-looking
answer, which would silently corrupt a genuinely new name), and whether a
pipeline repair heuristic had to intervene to produce this row at all.
"""

import re

from .classify import vocab_score

# Found tracing real "matches nobody" records (see validate_corpus.py /
# the corpus-wide none-record survey): a gender-column marker OCR's onto
# the name ("... Nike (F}") or a matric number sticks to it
# ("CVE/5/2324 OLABANUI ..."). Either is close to certainly wrong,
# independent of OCR confidence.
#
# A THIRD pattern from that same survey - two words fused into one long
# unbroken caps run with no space at all ("ADESHINAADEYEMI" for "ADESHINA
# ADEYEMI") - looked like a clean rule (a single token >=10 letters) until
# calibration against ground truth on live pages showed it flags mostly
# CORRECT records: Yoruba/Igbo names are heavily compound (OLUWASEGUN =
# OLUWA + SEGUN, ABDULWASIU = ABDUL + WASIU, ...), so "splits into two
# vocabulary words" is true of most long real names, not a fusion signal.
# On a 15-page LASU 2024 sample it flagged 300 tokens, 197 of them
# "decomposable" - and the rule's own precision, checked against truth,
# was 67% CORRECT despite being flagged LOW. Dropped; an unrecognized
# long token still gets caught by the plain vocabulary check below, just
# at MEDIUM rather than an unearned LOW.
_GENDER_MARK = re.compile(r"[\(\{]\s*[FfMm]\s*[\)\}]")
_MATRIC_LEFTOVER = re.compile(r"[A-Z]{2,6}\d{0,2}\s*/\s*[A-Z0-9]{1,4}\s*/\s*\d")

# Below this mean word confidence (0-100) a record is suspect on OCR
# grounds alone; below the lower bound it's suspect even if it happens to
# match a known name (Tesseract's confidence is noisy - see ocr.py - so a
# genuinely low read still gets a look, just a lighter one).
_LOW_CONF = 45.0
_MEDIUM_CONF = 70.0


def score_record(name: str, geom: dict | None) -> tuple[str, str]:
    """Returns (confidence, flag_reason): confidence is 'high'/'medium'/
    'low'; flag_reason is '' for high confidence, else a short code a
    review sheet can group/sort by. `geom` is the record's private
    "_geom" dict (parser.parse_lines_to_records) - conf/repaired plus the
    page-location fields review.py separately uses for the snapshot."""
    if _GENDER_MARK.search(name):
        return "low", "gender_marker_in_name"
    if _MATRIC_LEFTOVER.search(name):
        return "low", "matric_number_in_name"
    words = name.split()
    letters = sum(c.isalpha() for c in name)
    if name and letters / len(name) < 0.6:
        return "low", "low_letter_ratio"
    if len(words) < 2:
        return "low", "single_word_name"

    geom = geom or {}
    # The page number is its own field, not part of the name, but it's
    # exactly as load-bearing: Akowe's report assembly matches a client's
    # requested pages by this number against a same-numbered PNG, so a
    # record with an otherwise-perfect name but an unresolved page number
    # still needs a human to check it - never surfaced as "high" just
    # because the name itself reads cleanly. See pages.resolve_page_numbers
    # for "uncertain" vs "interpolated" (the latter is trusted: it's
    # arithmetic from two confirmed anchors, not a guess).
    if geom.get("page_number_status") == "uncertain":
        return "medium", "page_number_uncertain"

    conf = geom.get("conf", -1)
    repaired = bool(geom.get("repaired", False))
    _, name_hits = vocab_score(name)
    # A ratio, not "any hit": a 2-word name where only one word is a known
    # fragment ("OMOIJUIADEJOSHUA Akhigbe" - a real "Akhigbe" glued to
    # garbage) would pass an any-hit check outright, masking the garbage
    # half. Nigerian given names run 2-4 tokens with most individually
    # mined into the vocabulary already (see the note above), so recognizing
    # most-but-not-all of them is still a fine bar for "familiar enough".
    recognized = name_hits / len(words) >= 0.6

    if conf >= 0 and conf < _LOW_CONF:
        return "low", "low_ocr_confidence"
    if conf >= 0 and conf < _MEDIUM_CONF and not recognized:
        return "medium", "low_ocr_confidence"
    if not recognized and repaired:
        return "medium", "unrecognized_name_and_auto_repaired"
    if not recognized:
        return "medium", "name_not_in_vocabulary"
    if repaired:
        return "medium", "auto_repaired_line"
    return "high", ""
