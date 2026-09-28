"""Page-level helpers: deciding whether a page is worth extracting at all
(booklets mix in covers, programme-glossary pages, photos - see the
"non-list pages" finding from the manual exploration phase), and pulling
the printed page number, which is the load-bearing field for Akowe's
client-facing report (it's literally what page 3 of the report points at).
"""

import re
from collections import Counter

import cv2
import numpy as np
import pytesseract

from . import _tesseract  # noqa: F401  (locates the Tesseract executable)
from .classify import classify_line
from .parser import clean_name_text


def is_content_page(lines, min_name_rows: int = 3) -> bool:
    """A page is worth parsing if it has enough name-row-shaped lines to be
    a graduand list, rather than a cover, photo, or glossary page."""
    name_rows = 0
    for line in lines:
        if line.is_header:
            continue
        text = clean_name_text(line.text)
        if len(text) >= 3 and re.search(r"[A-Za-z]{2,}", text):
            name_rows += 1
    return name_rows >= min_name_rows


# These booklets never run to a thousand pages, so a page number is 1-3 digits
# and anything longer is never one. That alone rules out every printed year
# ("2020 ORDER OF PROCEEDINGS", "...Release of Eagles 2022") wherever it turns
# up - in a bare-digit line, a boxed badge, or a digit fused onto a slogan -
# without needing to recognise it as a year at all.
_MAX_PAGE_DIGITS = 3
_PLAIN_PAGE_NUM_RE = re.compile(r"\(?(\d{1,%d})\)?" % _MAX_PAGE_DIGITS)


def _plain_page_number(lines, page_height: float, edge_frac: float = 0.15) -> int | None:
    """Looks for a bare 1-3 digit number near the top or bottom edge of the
    page - these are the printed page numbers, and they show up in our line
    stream as short numeric-only lines (we saw this repeatedly as "noise"
    during manual page inspection - e.g. a lone "21" or "20" - which turns
    out to be exactly the signal we want here, not noise to discard). A
    stray leading/trailing paren is tolerated - some booklets print the
    number as "(109)", and OCR often keeps one paren but drops the other
    (UI 2004 came back as "109)" and "793)") - a strict bare-digit match
    would silently discard an otherwise clean, isolated reading."""
    candidates = []  # (0=bottom/1=top, top, value)
    for line in lines:
        m = _PLAIN_PAGE_NUM_RE.fullmatch(line.text.strip())
        if not m:
            continue
        near_top = line.top < page_height * edge_frac
        near_bottom = line.bottom > page_height * (1 - edge_frac)
        if near_top or near_bottom:
            candidates.append((0 if near_bottom else 1, line.top, int(m.group(1))))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][2]


# A page number set inside a small decorative box (found investigating why
# LASU alone left ~78% of records without one) defeats normal OCR entirely -
# not a confidence problem, an outright non-detection: even a tight, heavily
# upscaled crop of just that box, with a digit-only whitelist, reads nothing.
# What actually works is much blunter: skip trying to find the box at all,
# just upscale the whole edge band several times over and OCR it as sparse
# text - this read UI, UNILORIN, OOU and UNILAG's page numbers cleanly with
# no shape-finding. It still misses LASU's specific busy-diagonal design, but
# see resolve_page_numbers() - that gap gets closed by the fact that printed
# numbers increase by exactly one per PHYSICAL page, so any pages this can't
# read are recoverable from whichever nearby pages it can.
_BOXED_SCALE = 4
_BOXED_MIN_CONF = 60.0


# How far (in multiples of the candidate digit's own box size) real content
# can sit before it no longer counts as "beside" the candidate. Found
# tracing four confirmed wrong readings (COVENANT 2021, FUTA 2024, LASU
# 2019, UNILORIN 2021 - all four independently, in three separate sampling
# rounds): a list-item number ("12. OLAYIWOLA...", "2 ONITIRI...") sits
# tight against its row's content, typically well under this gap; a genuine
# page number, wherever a document puts it, has nothing printed immediately
# beside it.
_ISOLATION_GAP_FACTOR = 4.0


def _looks_like_real_content(text: str) -> bool:
    """Broader than ocr._wordlike on purpose: that asks "does this look like
    a name", which a matric code ("AEC/17/1624") fails despite being real,
    populated row content - and printed exactly between a FUTA list number
    and the name it belongs to, which is precisely what let a first version
    of this check miss it (the matric code didn't count as "content", so the
    nearer of the two real neighbours was invisible to it, and the name past
    it was just far enough away to clear the gap). Here the question is only
    "is this row busy or empty", so anything with 2+ real characters counts,
    stray punctuation aside."""
    letters_or_digits = sum(c.isalnum() for c in text)
    return letters_or_digits >= 2


# Matches cli._banner_key's normalization exactly (letters only, uppercase)
# - not imported from there to avoid a cycle (cli.py imports this module),
# so if one changes the other needs to stay in step.
_LETTERS_ONLY = re.compile(r"[^A-Z]")


def _banner_key(text: str) -> str:
    return _LETTERS_ONLY.sub("", text.upper())


def _row_text(words, cy: float, box_h: float) -> str:
    """Every real-content word roughly on the candidate's row, left to
    right - for comparing against a document's known running banner text."""
    row = []
    for _, w in words.iterrows():
        if not _looks_like_real_content(str(w["text"])):
            continue
        wcy = w["top"] + w["height"] / 2
        if abs(wcy - cy) <= box_h:
            row.append((w["left"], str(w["text"])))
    row.sort()
    return " ".join(t for _, t in row)


def _is_banner_text(row_text: str, banner_keys, min_len: int = 10) -> bool:
    key = _banner_key(row_text)
    if len(key) < min_len:
        return False
    return any(key in bk or bk in key for bk in banner_keys)


# A leading/trailing digit run, optionally parenthesized, split from
# whatever comes before/after it - see _banner_fused_candidate_line.
_LEADING_DIGITS_RE = re.compile(r"^\(?(\d{1,%d})\)?\s+(.+)$" % _MAX_PAGE_DIGITS)
_TRAILING_DIGITS_RE = re.compile(r"^(.+?)\s+\(?(\d{1,%d})\)?$" % _MAX_PAGE_DIGITS)
_DIGIT_RUN_RE = re.compile(r"\d+")


def _banner_fused_candidate_line(lines, banner_keys, page_height: float, edge_frac: float = 0.15):
    """Finds a line that looks like a recognized banner/slogan with a page
    number fused onto its front or back by OCR (FUTA: "THE BEST UNIVERSITY
    OF TECHNOLOGY IN NIGERIA (95)", sometimes with stray noise glued to the
    front instead: "ih THE BEST UNIVERSITY OF TECHNOLOGY IN NIGERIA (28)")
    - a case _plain_page_number's bare-digit check can never match, since
    the line as a whole is far more than just digits. Returns the matching
    Line, or None.

    This only locates the candidate - it does NOT trust the digit already
    sitting in that line's text (see _reread_banner_fused_digit for why:
    reading it as one line among a page's other 100+ lines has been
    observed to misread the digit even when the exact same crop, read on
    its own, gets it right consistently). Returns (line, side), where side
    is "leading" or "trailing" - which end of the row the digit sits on -
    since a slogan can carry its OWN fixed numbers too (COVENANT's "17th
    Convocation", or a bare year in "Release of Eagles 2022"), the reread
    needs to know WHERE to look rather than picking whichever digit run in
    the row looks most plausible."""
    if not banner_keys:
        return None
    for line in lines:
        text = line.text.strip()
        near_top = line.top < page_height * edge_frac
        near_bottom = line.bottom > page_height * (1 - edge_frac)
        if not (near_top or near_bottom):
            continue
        m = _LEADING_DIGITS_RE.match(text)
        if m and _is_banner_text(m.group(2), banner_keys):
            return line, "leading"
        m = _TRAILING_DIGITS_RE.match(text)
        if m and _is_banner_text(m.group(1), banner_keys):
            return line, "trailing"
    return None


def _reread_banner_fused_digit(img, words, line, side: str) -> int | None:
    """Re-OCRs a banner-fused line's own row in isolation, tightly cropped
    out of the full page, instead of trusting the digit _process_page's
    whole-page OCR pass already read for it. Confirmed against four real
    FUTA pages: the whole-page pass misread the digit on two of the four
    (a systematic error, not random noise - both misreads landed on a
    DIFFERENT but internally-consistent wrong page-numbering offset, which
    is exactly the shape of error that can fool resolve_page_numbers's
    majority check into trusting the wrong one), while this isolated
    reread got all four right, including the two the whole-page pass got
    wrong. `words` is the page's own word-level OCR (used to find every
    word on this line's row, since Line.right isn't reliably populated);
    `line` only needs to give the row's approximate vertical position.

    `side` (from _banner_fused_candidate_line) picks the FIRST digit run
    for "leading" or the LAST for "trailing", rather than picking whichever
    run in the row looks most plausible (longest, say) - a slogan can carry
    its own fixed numbers that have nothing to do with the page (COVENANT:
    "17th Convocation" sits in the middle, a bare "2022" sits right before
    a trailing page number), and those are exactly the ones a plausibility
    heuristic would grab instead of the real, edge-of-row page number."""
    if words is None or len(words) == 0:
        return None
    cy = (line.top + line.bottom) / 2
    row_h = max(line.bottom - line.top, 1.0)
    row = words[(words["top"] + words["height"] / 2 - cy).abs() < row_h]
    if len(row) == 0:
        return None
    left, top = float(row["left"].min()), float(row["top"].min())
    right = float((row["left"] + row["width"]).max())
    bottom = float((row["top"] + row["height"]).max())
    pad_y = (bottom - top) * 0.3
    crop = img.crop((max(0, left - 5), max(0, top - pad_y), right + 10, bottom + pad_y))
    big = crop.resize((crop.width * 3, crop.height * 3))
    text = pytesseract.image_to_string(big, config="--psm 6").strip()
    matches = list(_DIGIT_RUN_RE.finditer(text))
    if not matches:
        return None
    picked = (matches[0] if side == "leading" else matches[-1]).group()
    # A slogan can itself end (or start) with a year and carry no readable
    # page number at all (COVENANT: "...Release of Eagles 2022"), which
    # looks exactly like a fused trailing digit. Runs are taken whole (not
    # split at 3 digits) so that year is seen as the 4-digit number it is
    # and refused, rather than being truncated into a plausible-looking one.
    if len(picked) > _MAX_PAGE_DIGITS:
        return None
    return int(picked)


def _looks_isolated(words, x: float, y: float, box_w: float, box_h: float, banner_keys=None) -> bool:
    """True if no real content from the page's own OCR sits close beside this
    candidate digit on roughly the same row. `words` is the page's word-level
    OCR data (same coordinate space the candidate's box has already been
    mapped into - see _boxed_page_number) - this is exactly the information
    the plain bare-digit-line check gets for free by only trusting whole
    LINES, and the upscaled-band OCR has none of on its own, since it reads
    a raw image crop with no idea what surrounds a digit it finds.

    `banner_keys` (a document-wide running-header/footer set - see
    cli._detect_banners) is what tells a genuine page number apart from one
    with a repeating slogan tagged onto it (FUTA: "THE BEST UNIVERSITY OF
    TECHNOLOGY IN NIGERIA --- [84]") - a plain proximity check can't
    otherwise tell that apart from a list-item number sitting next to a
    name, since both are "real content nearby". The difference is that a
    name is different on every row and a slogan is the exact same text on
    every page - which is precisely what a banner already is."""
    if words is None or len(words) == 0:
        return True
    gap = _ISOLATION_GAP_FACTOR * max(box_w, box_h, 1.0)
    cy = y + box_h / 2
    row_text_cache = None
    for _, w in words.iterrows():
        if not _looks_like_real_content(str(w["text"])):
            continue  # noise doesn't count as "real content" beside the candidate
        wcy = w["top"] + w["height"] / 2
        if abs(wcy - cy) > box_h:
            continue  # not on roughly the same row
        wleft, wright = w["left"], w["left"] + w["width"]
        if wleft > x + box_w:
            hgap = wleft - (x + box_w)
        elif wright < x:
            hgap = x - wright
        else:
            hgap = 0.0  # horizontally overlapping - definitely not isolated
        if hgap >= gap:
            continue
        if banner_keys:
            if row_text_cache is None:
                row_text_cache = _row_text(words, cy, box_h)
            if _is_banner_text(row_text_cache, banner_keys):
                continue  # a recurring slogan, not per-row content - doesn't disqualify
        return False
    return True


_OVAL_MIN_H, _OVAL_MAX_H = 20, 65
_OVAL_MIN_W, _OVAL_MAX_W = 25, 90
_OVAL_MIN_ASPECT, _OVAL_MAX_ASPECT = 0.85, 2.0
_OVAL_CENTER_FRAC = (0.35, 0.65)


def _oval_badge_page_number(img, band_frac: float = 0.15) -> int | None:
    """A last-resort fallback for a page number set inside a small round or
    oval badge (found on LASU - an elongated oval - and OAU - a circular
    one) rather than sitting as its own OCR-able word. Word-level OCR (what
    _boxed_page_number relies on) never proposes a candidate here at all -
    the badge's own ring border visually merges with the digit strokes and
    defeats Tesseract's text segmentation entirely, not just its
    recognition, confirmed by testing the exact same crop through
    Tesseract directly and getting no word box back whatsoever. Cropping
    tightly to the badge's own bounding box (found as a shape via OpenCV,
    not as text) excludes enough of the ring and the busy decorative
    graphic beneath it that the digits become legible - the same tight
    crop that turned meaningless noise into a clean read during manual
    investigation of both a LASU and an OAU sample. The aspect range spans
    both a stretched oval and a plain circle; restricted to a small
    centered badge shape specifically (a university crest sits to one
    side and is a different size, which is what the position/size bounds
    here are for) since, unlike _boxed_page_number, there's no isolation
    or banner check to fall back on - the shape and position bounds are
    what keeps this from firing on unrelated page furniture."""
    w, h = img.size
    y0 = int(h * (1 - band_frac))
    band = img.crop((0, y0, w, h)).convert("L")
    arr = np.array(band)
    _, bw = cv2.threshold(arr, 150, 255, cv2.THRESH_BINARY_INV)
    contours, _ = cv2.findContours(bw, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []  # (-area, value)
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        if not (_OVAL_MIN_H <= ch <= _OVAL_MAX_H and _OVAL_MIN_W <= cw <= _OVAL_MAX_W):
            continue
        aspect = cw / ch
        if not (_OVAL_MIN_ASPECT <= aspect <= _OVAL_MAX_ASPECT):
            continue
        center_frac = (x + cw / 2) / w
        if not (_OVAL_CENTER_FRAC[0] <= center_frac <= _OVAL_CENTER_FRAC[1]):
            continue
        crop = band.crop((x, y, x + cw, y + ch))
        big = crop.resize((crop.width * 8, crop.height * 8))
        for psm in (6, 7, 8, 10):
            text = pytesseract.image_to_string(
                big, config=f"--psm {psm} -c tessedit_char_whitelist=0123456789()").strip()
            m = _PLAIN_PAGE_NUM_RE.fullmatch(text)
            if m:
                candidates.append((-cv2.contourArea(c), int(m.group(1))))
                break
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]


def _boxed_page_number(img, words=None, banner_keys=None, band_frac: float = 0.15) -> int | None:
    w, h = img.size
    candidates = []  # (is_top, value)
    for is_top, (y0, y1) in ((True, (0, int(h * band_frac))), (False, (int(h * (1 - band_frac)), h))):
        band = img.crop((0, y0, w, y1))
        big = band.resize((band.width * _BOXED_SCALE, band.height * _BOXED_SCALE))
        try:
            data = pytesseract.image_to_data(big, config="--psm 11", output_type=pytesseract.Output.DATAFRAME)
        except pytesseract.TesseractError:
            continue
        for _, r in data[data["text"].notna()].iterrows():
            t = str(r["text"]).strip()
            if not (t.isdigit() and 1 <= len(t) <= _MAX_PAGE_DIGITS and float(r["conf"]) >= _BOXED_MIN_CONF):
                continue
            # Map the candidate's box out of the upscaled band crop and back
            # into the page's own coordinate space, to check against `words`.
            ox, oy = r["left"] / _BOXED_SCALE, r["top"] / _BOXED_SCALE + y0
            ow, oh = r["width"] / _BOXED_SCALE, r["height"] / _BOXED_SCALE
            if not _looks_isolated(words, ox, oy, ow, oh, banner_keys):
                continue
            candidates.append((is_top, int(t)))
    if not candidates:
        return _oval_badge_page_number(img, band_frac)
    return min(candidates, key=lambda c: c[0])[1]  # prefer the bottom edge


def detect_page_number(lines, page_height: float, img=None, words=None, banner_keys=None,
                        edge_frac: float = 0.15) -> int | None:
    """The plain bare-digit-line check first (cheap, and correct wherever it
    finds anything); only falls back to the slower upscaled-band OCR (needs
    `img`) when that finds nothing. `words` (the page's own word-level OCR,
    same coordinate space as `img`) and `banner_keys` (see cli._detect_banners)
    let the fallback reject a candidate that's actually a list-item number,
    while not rejecting a genuine page number just because a recurring
    slogan happens to sit beside it - see _looks_isolated."""
    found = _plain_page_number(lines, page_height, edge_frac)
    if found is not None or img is None:
        return found
    return _boxed_page_number(img, words, banner_keys, edge_frac)


_REPEAT_MIN_PAGES = 3
_REPEAT_MIN_SHARE = 0.2


def drop_repeated_readings(readings: list[int | None]) -> list[int | None]:
    """Blanks every reading whose value turns up on many pages of the same
    document. A page number is different on every page by definition, so a
    number that recurs is fixed print - the booklet's title, a running
    banner, a footer ("2020 ORDER OF PROCEEDINGS", a convocation ordinal
    like "28th / 29th") - that happened to sit near an edge. This is what
    the booklet's own title tells us without parsing it: whatever numbers it
    carries are printed identically on every page.

    "Many" needs both a floor and a share of the pages that produced any
    reading: three pages alone could just be a document whose numbering
    restarts per section, where every value legitimately recurs a few times
    among dozens of distinct ones - a title number recurs on most pages.
    Blanked pages become "no reading", so the fallbacks still get to try
    them."""
    present = [v for v in readings if v is not None]
    if not present:
        return list(readings)
    threshold = max(_REPEAT_MIN_PAGES, _REPEAT_MIN_SHARE * len(present))
    counts = Counter(present)
    repeated = {v for v, n in counts.items() if n >= threshold}
    return [None if v in repeated else v for v in readings]


def resolve_page_numbers(directly_detected: list[int | None]) -> list[tuple[int | None, str]]:
    """Fills in the printed page number for physical pages where direct
    detection (above) found nothing, using the one fact that holds
    regardless of a document's decorative design: the printed number goes up
    by exactly one for every physical page, whether or not that page has any
    names on it (a divider, a photo page, a blank leaf each still take a
    number - Akowe's own booklets routinely skip numbers across such pages
    in the sequence of NAMED rows, which is a gap in content, not a gap in
    the underlying page count).

    `directly_detected` is one entry per physical page of one document, in
    order - the value `detect_page_number` found, or None. Returns the same
    length, each a (final_number_or_None, status) pair:

      "detected"    - direct OCR read it, trusted as-is.
      "interpolated"- no direct reading, but two directly-detected anchors
                      bracket it and their gap exactly matches the number of
                      physical pages between them - the answer is then
                      arithmetic, not a guess (found this holds even across
                      LASU's stretches where every single page defeats direct
                      OCR, since it only takes two anchors anywhere in the
                      whole stretch, not one for every page).
      "uncertain"   - no reading here, and either no confirmed pagination
                      scheme exists for this document at all, or this one
                      page's own reading contradicted it - never guessed
                      at; a human should look at this one page's own
                      number specifically, not the person's whole record.

    A real detected value minus its own physical-page index is a constant
    across the WHOLE document (page 12 reading "149" and page 20 reading
    "157" both mean the booklet's numbering started at 137) - every genuine
    reading anywhere in the document agrees on that one constant, while a
    spurious reading (a stray list number, a caption, near enough to an
    edge to be mistaken for the real thing) essentially never happens to
    match it. That is a much stronger test than only comparing a reading to
    its immediate neighbours: a real failure found tracing this exact case
    (LASU 2024's opening pages) was a run of SEVERAL early pages that each
    read a small, plausible-looking, but wrong number ("1", "8", "4", "7")
    with nothing adjacent to contradict any one of them individually - only
    checking them all against the document's real, larger-evidenced
    numbering scheme (confirmed by many pages agreeing later on) exposes
    that none of them belong to it.
    """
    n = len(directly_detected)
    anchors = [(i, v) for i, v in enumerate(directly_detected) if v is not None]
    out: list[tuple[int | None, str]] = [(None, "uncertain")] * n
    if not anchors:
        return out

    if len(anchors) == 1:
        # Nothing else in the document to corroborate or contradict it -
        # still the best evidence available for its own page, but it can't
        # anchor an interpolation (there's no confirmed per-page rate).
        i, v = anchors[0]
        out[i] = (v, "detected")
        return out

    groups: dict[int, list[tuple[int, int]]] = {}
    for i, v in anchors:
        groups.setdefault(v - i, []).append((i, v))
    trusted = max(groups.values(), key=len)
    if len(trusted) < 2:
        return out  # every reading disagrees with every other - no confirmed scheme, nothing trusted

    for i, v in trusted:
        out[i] = (v, "detected")
    for a_idx in range(len(trusted) - 1):
        i, vi = trusted[a_idx]
        j, vj = trusted[a_idx + 1]
        for k in range(i + 1, j):
            out[k] = (vi + (k - i), "interpolated")  # vj - vi == j - i always holds: same offset by construction
    return out
