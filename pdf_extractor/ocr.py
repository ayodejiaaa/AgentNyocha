"""OCR wrapper. Extracts word-level bounding boxes rather than a flat text
blob, because column detection and reading-order reconstruction need to
know *where* each word sits on the page, not just what it says.
"""

import cv2
import numpy as np
import pandas as pd
import pytesseract
from PIL import Image

from . import _tesseract  # noqa: F401  (locates the Tesseract executable)


def _wordlike(text: str) -> bool:
    """Looks like a (possibly slightly misread) word rather than noise:
    starts with a letter, 3+ letters, and mostly letters - so "ADEDE4JI"
    (a name with one stray digit, seen on a real FUTA scan at confidence
    16) is kept, while short, digit-laden or punctuation-heavy fragments
    are not."""
    t = text.strip()
    if len(t) < 3 or not t[0].isalpha():
        return False
    letters = sum(c.isalpha() or c in "'-" for c in t)
    return letters >= 3 and letters / len(t) >= 0.75


# Tesseract page segmentation mode 11 ("sparse text": find as much text as
# possible in no particular order) instead of its automatic layout analysis.
# These pages are tables and multi-column lists, where the automatic mode
# tries to impose paragraph/block structure and sometimes emits wrong word
# boxes (found on a real FUTA scan: a surname reported as spanning the whole
# name's width, given names with double-height boxes). This pipeline builds
# its own lines and columns from word positions anyway, so it only needs
# reliable individual words - which sparse mode gives. Measured across all
# 8 universities' pages: never meaningfully worse than automatic mode, and
# +5 points on FUTA 2021's dense scan (84.5% -> 89.7%), +8-16 matched rows on
# OAU.
DEFAULT_PSM = 11


def _ocr_once(img: Image.Image, min_confidence: int, psm: int | None) -> pd.DataFrame:
    config = f"--psm {psm}" if psm is not None else ""
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DATAFRAME, config=config)
    data = data[data["text"].notna()]
    data = data[data["text"].astype(str).str.strip() != ""]
    conf = data["conf"].astype(float)
    # A blanket confidence cutoff throws away real names: on dense tables
    # Tesseract sometimes reports an exact 0.0 for words it read perfectly
    # (found on a real FUTA scan: "FASHOLA Emmanuel" both at 0.0 beside
    # "Olamilekan" at 93 - the surname and first given name were dropped,
    # leaving a record with only the last given name). So a low-confidence
    # token is still kept when it looks like a word rather than noise: 3+
    # letters, mostly alphabetic. Noise (halftone speckle, rules, page furniture)
    # is overwhelmingly short, punctuation or digit-laden.
    wordlike = data["text"].astype(str).map(_wordlike)
    data = data[(conf >= min_confidence) | ((conf >= 0) & wordlike)]
    return data.reset_index(drop=True)


# Tesseract's own thresholding can fail outright on some scans: a UI 2004 page
# (perfectly readable, dense two-column name list, scanned with a wide grey
# scanner-bed border) came back as 26 fragments with a median box height of
# 5px (real text here is 14-18px) - noise, not words. The same page
# binarised with a plain global Otsu threshold first read 215 real words.
# 45 of that year's 162 pages failed this way, which made it the weakest
# year in the corpus that wasn't a source-coverage gap. Retrying costs a
# second OCR, so it is only done when the first result looks like garbage.
#
# Word count and box height alone missed a second, subtler case (UI 2004: a
# book-scan with a dark spine shadow): 171 words at a plausible 10px median,
# of which only 29 were known names - the page read as confident-looking
# nonsense. The reliable signal is the share of words that are known names
# or terms: healthy pages measured 0.59-0.94 on OOU, UNILAG, UNILORIN and
# LASU, the failing page 0.17. On healthy pages the binarised read is no
# better (0.94 vs 0.94, 0.89 vs 0.78), so this stays a rescue, not a default.
_SUSPECT_MAX_WORDS = 80
_SUSPECT_MAX_MEDIAN_HEIGHT = 8
_SUSPECT_MAX_KNOWN_DENSITY = 0.4


def _known_word_hits(words: pd.DataFrame) -> int:
    from .classify import vocab_score  # local import: avoids a cycle at module load time
    return sum(vocab_score(" ".join(words["text"].astype(str))))


def _looks_like_garbage(words: pd.DataFrame) -> bool:
    if len(words) < _SUSPECT_MAX_WORDS:
        return True
    if float(words["height"].median()) < _SUSPECT_MAX_MEDIAN_HEIGHT:
        return True
    return _known_word_hits(words) / len(words) < _SUSPECT_MAX_KNOWN_DENSITY


def _binarized(img: Image.Image) -> Image.Image:
    gray = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return Image.fromarray(binary)


def ocr_words(img: Image.Image, min_confidence: int = 30, psm: int | None = DEFAULT_PSM) -> pd.DataFrame:
    """Returns a DataFrame of recognized words with position data:
    columns: text, left, top, width, height, conf, line_num, block_num, par_num
    Rows with low confidence or blank text are dropped. `psm` overrides the
    Tesseract page segmentation mode; None means Tesseract's own automatic
    mode.

    If the result looks like noise (see _looks_like_garbage) the page is
    re-read from a binarised copy, and that reading is used only if it
    contains clearly more known names/terms - so a genuinely sparse or
    blank page keeps its original reading.
    """
    words = _ocr_once(img, min_confidence, psm)
    if _looks_like_garbage(words):
        retry = _ocr_once(_binarized(img), min_confidence, psm)
        base_hits = _known_word_hits(words)
        retry_hits = _known_word_hits(retry)
        if retry_hits >= base_hits * 2 and retry_hits - base_hits >= 5:
            return retry
    return words
