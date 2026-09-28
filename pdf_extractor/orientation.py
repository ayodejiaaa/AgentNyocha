"""Per-page orientation detection and correction.

This is pipeline stage zero, and the single highest-leverage fix identified
during the manual PDF exploration earlier in this project: a meaningful
fraction of Akowe's scanned pages are rotated 0/90/180/270 degrees, and page
rotation is inconsistent even within one PDF (odd/even pages scanned
differently). Any column-detection or reading-order logic downstream is
worthless if it runs on a page that's actually upside down.

Starts from Tesseract's built-in orientation-and-script-detection (OSD),
then double-checks its answer against the 180-degree-opposite candidate by
counting how many of the words each orientation OCRs are real names or
institutional terms from the training vocabulary - see
_resolve_opposite_ambiguity for why OSD alone isn't trustworthy on these
documents, and why Tesseract's own confidence isn't a good tiebreaker.
"""

import pytesseract
from PIL import Image

from . import _tesseract  # noqa: F401  (locates the Tesseract executable)
from .classify import vocab_score

# The opposite orientation has to beat OSD's pick decisively, on BOTH a
# ratio and an absolute count, before we swap. Measured on real pages at the
# scale used below: correctly oriented text reads ~170-410 vocabulary hits,
# a wrong orientation reads 6-27, and a correct OSD pick still sees ~15-30
# coincidental hits in its upside-down twin (short gibberish strings
# occasionally spell a short name). Requiring 2x AND +5 leaves that whole
# range on the "keep" side, so a correct answer is never overturned by noise.
_MIN_HIT_RATIO = 2.0
_MIN_HIT_GAIN = 5
# If OSD's pick already reads this many known words, it is plainly correct
# and the second OCR (of the opposite orientation) is skipped - most pages,
# so the extra cost is paid only where OSD is actually in doubt.
_TRUSTED_HITS = 80
# The check only needs to compare relative word quality, not transcribe
# accurately, so it runs on a shrunk copy of the page's central band
# (skipping headers/footers/crests, the least representative part of a
# page). The scale is a real constraint, not just a speed knob: at 0.5 the
# very dense LASU 2012 pages (~250 names each) were unreadable in EITHER
# orientation (15 vs 6 hits, 0 vs 0), so the check had no signal and left
# ~40 upside-down pages unfixed; at 0.75 the same pages read 6 vs 179,
# 27 vs 218, 13 vs 172.
_CHECK_SCALE = 0.75
_CHECK_BAND = (0.25, 0.75)


def detect_rotation(img: Image.Image) -> int:
    """Returns the clockwise rotation (0/90/180/270) needed to make the page
    upright, as reported by Tesseract OSD. Returns 0 if OSD can't get a
    confident reading (e.g. a near-blank or non-text page)."""
    try:
        osd = pytesseract.image_to_osd(img, output_type=pytesseract.Output.DICT)
        return int(osd.get("rotate", 0))
    except pytesseract.TesseractError:
        return 0


def _vocab_hits(img: Image.Image) -> int:
    """How many words OCR'd from the page's central band are known names or
    institutional terms (classify.vocab_score - the vocabulary mined from
    the 535k-row training corpus). Correctly oriented text is full of them;
    upside-down or sideways text OCRs as gibberish that almost never is."""
    w, h = img.size
    band = img.crop((0, int(h * _CHECK_BAND[0]), w, int(h * _CHECK_BAND[1])))
    small = band.resize((max(1, int(band.width * _CHECK_SCALE)),
                          max(1, int(band.height * _CHECK_SCALE))))
    try:
        text = pytesseract.image_to_string(small, config="--psm 11")
    except pytesseract.TesseractError:
        return 0
    return sum(vocab_score(text))


def apply_rotation(img: Image.Image, clockwise_degrees: int) -> Image.Image:
    """Rotates by a clockwise correction as returned by correct_orientation
    (so an already-decided rotation can be re-applied to a re-rendered copy
    of the same page without re-running detection)."""
    return _rotated(img, clockwise_degrees)


def _rotated(img: Image.Image, clockwise_degrees: int) -> Image.Image:
    if clockwise_degrees % 360 == 0:
        return img
    # PIL's rotate() is counter-clockwise; Tesseract's "rotate" value is the
    # clockwise correction needed, so negate it.
    return img.rotate(-clockwise_degrees, expand=True)


def _resolve_opposite_ambiguity(img: Image.Image, rotate_by: int) -> tuple[Image.Image, int]:
    """OSD is not reliable at picking between a rotation and its 180-degree
    opposite on these documents, so its pick is checked against that
    opposite by how much of what each OCRs is real names/terms. Real
    failures found validating against Akowe's own data:

    - 90 vs 270 (UNILORIN 2016): OSD confidently reported 90 for a page that
      reads as clean English names only at 270 ("NWAKAJI James Clifford")
      and as scrambled garbage at 90 ("nmynypiAueay NMOVIHI").
    - 0 vs 180 (LASU 2009/2020): these lists are ALL CAPS, which leaves OSD
      almost nothing to judge by - it leans on ascender/descender
      statistics, and capital letters have neither. It answered "0" with
      near-zero orientation confidence (0.9-2.1) for roughly half of a
      file's pages that were in fact upside down; every one of those OCR'd
      as gibberish, so ~60% of that year's records were never extracted.

    An earlier version compared Tesseract's own mean word confidence
    instead, and that turned out to be wrong in a way that made things
    worse: on a LASU 2013 page where OSD was CORRECT (180), the upside-down
    reading scored higher confidence (39 vs 27) and the check overturned
    the right answer. Gibberish can OCR with decent confidence; it
    cannot OCR into names. Counting vocabulary hits measures the thing that
    actually matters - whether the text is language.

    The check covers all four rotations, since OSD can be wrong in either
    direction, and it defaults to trusting OSD unless another rotation is
    decisively better."""
    picked = _rotated(img, rotate_by)
    picked_hits = _vocab_hits(picked)
    if picked_hits >= _TRUSTED_HITS:
        return picked, rotate_by

    # OSD's pick reads few known words, so it may be wrong - by 180 degrees
    # (checked first, the common case) or by a quarter turn (UNILORIN 2017:
    # a colourful faculty page printed sideways, OSD said 0, and 3 such pages
    # of that year yielded almost nothing). Try each other rotation and keep
    # the best, on the same decisive-margin rule.
    best, best_degrees, best_hits = picked, rotate_by, picked_hits
    for offset in (180, 90, 270):
        degrees = (rotate_by + offset) % 360
        candidate = _rotated(img, degrees)
        hits = _vocab_hits(candidate)
        if hits > best_hits:
            best, best_degrees, best_hits = candidate, degrees, hits
        if offset == 180 and hits >= _TRUSTED_HITS:
            break   # the opposite reads plainly - no need to try the quarter turns
    if best_degrees != rotate_by and best_hits >= picked_hits * _MIN_HIT_RATIO             and best_hits - picked_hits >= _MIN_HIT_GAIN:
        return best, best_degrees
    return picked, rotate_by


def correct_orientation(img: Image.Image) -> tuple[Image.Image, int]:
    """Returns (corrected_image, degrees_rotated)."""
    return _resolve_opposite_ambiguity(img, detect_rotation(img))
