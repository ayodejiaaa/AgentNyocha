"""Column detection and reading-order reconstruction.

This is the core problem identified during manual exploration of Akowe's
booklets: naive top-to-bottom OCR reading order scrambles 2/3-column pages,
because it interleaves lines across columns. The fix, confirmed by hand
against real pages, is:

  1. Correct page orientation first (see orientation.py) - a "column
     confusion" bug is very often actually an orientation bug in disguise.
  2. Group words into physical text lines using vertical proximity AND
     horizontal non-overlap (see _group_words_into_lines) - two words that
     overlap horizontally can never be on the same printed line, which is
     a far more reliable signal than vertical bounding-box overlap alone
     on a tightly-leaded table (found on a real UNILAG page: row height
     ~18px, row-to-row step only ~13px - vertical-overlap-only grouping
     snowballs multiple real rows into one garbled blob there).
  3. Split the page into vertical column bands using two complementary
     signals: a page-wide empty gutter in word-center positions (reliable
     when every row's column content is a similar width - verified on
     LASU/OAU), and recurring internal gaps within individual rows
     (reliable when column width varies row to row enough that the union
     of all rows bridges what is, per row, a real gap - found on a real
     UNILAG page with 3 name-columns and no page-wide empty strip at all).
  4. Within each column, group words into lines (step 2), order lines
     top-to-bottom, and words within a line left-to-right.
  5. Concatenate columns left-to-right - this matched the verified Excel
     row order on every real page checked (LASU, OAU, UI, FUTA, UNILAG).
"""

import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

_LOW_SIGNAL_WORD_RE = re.compile(r"^[\W_]+$")

# Two words in the same row may overlap horizontally by up to this fraction
# of the narrower word's width (touching boxes); more than that means they
# belong to different rows. Same-column words from adjacent rows overlap
# ~100% (measured ~98% on the UNILAG case that motivated the rule), so any
# value well under that separates the two cases.
_OVERLAP_TOLERANCE = 0.3


def _is_low_signal_word(text) -> bool:
    """True for a word with no real alphanumeric content - a stray period,
    dash, or other punctuation fragment. Common OCR noise: a list number's
    trailing punctuation ("72.") landing as its own detached micro-"word"
    a few pixels wide, sometimes even baseline-shifted onto a different
    line than the number/name it belongs to. Gap/column-boundary detection
    must ignore these - a recurring narrow punctuation-only token at some
    x-position is not evidence of a real column there, just noise that
    happens to repeat because every row has its own stray period."""
    s = str(text)
    return not s or bool(_LOW_SIGNAL_WORD_RE.match(s))


@dataclass
class Line:
    text: str
    top: float
    bottom: float
    left: float
    column_index: int
    is_header: bool = False
    right: float = 0.0
    # Mean Tesseract word confidence (0-100) behind this line's text, or -1
    # if unknown (e.g. a line built without per-word confidence data). Used
    # downstream by confidence.py to flag low-trust records for review -
    # never to alter the extracted text itself.
    conf: float = -1.0
    # True if this line only exists in its current form because a repair
    # heuristic acted on it (the wrapped-name merge, most directly) rather
    # than being a single untouched OCR read - a genuine but weaker signal
    # for review flagging than conf alone.
    repaired: bool = False


def _group_words_into_lines(words: pd.DataFrame) -> list[list]:
    """Groups a word set into physical text lines, returning a list of
    index-lists in top-to-bottom order.

    Two words are allowed in the same line only if they are (a) vertically
    close to the line's most-recently-added word and (b) horizontally
    non-overlapping with every word already in that line - real printed
    words never overlap each other, so horizontal overlap is a decisive
    signal that two words belong to different physical rows, regardless of
    how close their bounding boxes sit vertically. That distinction is
    what a purely vertical-overlap check (the previous approach) gets
    wrong on a dense, tightly-leaded table: a row's own descenders/
    ascenders can make its bounding box overlap the next row's by several
    pixels, and absorbing that first overlap grows the merged box, making
    the row after that even more likely to overlap too - a snowball that
    can glom many real rows into one line.

    Comparing against the line's most recent member - not a single fixed
    anchor from whichever word happened to start it - matters in practice:
    a stray, unrelated word (a detached OCR punctuation fragment landing
    slightly above a real row) starting a line first would otherwise
    permanently skew that line's reference point, wrongly rejecting the
    row's own genuine later words once their combined offset from that
    irrelevant anchor exceeds the window (found on a real LASU page: a
    stray "." a few pixels off is what a name's own words were being
    measured against, splitting one row into two). Comparing against the
    most recent member instead - equivalent to single-linkage clustering,
    since words are processed in ascending top order, so the most
    recently added member of any line is always the one closest to the
    current candidate - keeps the relevant reference current without
    reopening the snowball risk: two genuinely different rows still can't
    merge, because their own words occupy overlapping x-ranges (same kind
    of content at the same horizontal position column to column), which
    the non-overlap check continues to block regardless of y-proximity.
    """
    if len(words) == 0:
        return []
    w = words.sort_values("top")
    typical_height = w["height"].median()
    if not typical_height or typical_height <= 0:
        typical_height = w["height"].mean() or 1.0
    y_window = typical_height * 0.9

    def collides(left, right, s0, s1) -> bool:
        # Adjacent words in one row routinely touch by a pixel or two
        # (tight kerning, imprecise boxes) - found on a real FUTA scan,
        # where a surname ending at x=468 and its given name starting at
        # x=467 were split into different rows by a strict no-overlap
        # rule, losing the surname from the record. Only a SUBSTANTIAL
        # overlap means "two different rows' words in the same spot".
        overlap = min(right, s1) - max(left, s0)
        if overlap <= 0:
            return False
        narrower = max(1.0, min(right - left, s1 - s0))
        return overlap > _OVERLAP_TOLERANCE * narrower

    open_lines: list[dict] = []
    for idx, row in w.iterrows():
        left, right = row["left"], row["left"] + row["width"]
        candidates = sorted(
            (ln for ln in open_lines if row["top"] - ln["last_top"] <= y_window),
            key=lambda ln: row["top"] - ln["last_top"],
        )
        placed = False
        for ln in candidates:
            if not any(collides(left, right, s0, s1) for s0, s1 in ln["spans"]):
                ln["idxs"].append(idx)
                ln["spans"].append((left, right))
                ln["last_top"] = row["top"]
                placed = True
                break
        if not placed:
            open_lines.append({"last_top": row["top"], "idxs": [idx], "spans": [(left, right)]})

    open_lines.sort(key=lambda ln: w.loc[ln["idxs"], "top"].min())
    return [ln["idxs"] for ln in open_lines]


def _rough_lines_ignoring_columns(words: pd.DataFrame) -> list[pd.DataFrame]:
    """Groups words into lines (see _group_words_into_lines), across the
    whole page width (no column awareness) - used to tell wide header
    lines apart from narrow, single-column body lines before real column
    detection runs, and to feed per-row gap analysis."""
    if len(words) == 0:
        return []
    return [words.loc[idxs] for idxs in _group_words_into_lines(words)]


def _recurring_internal_gap_boundaries(words: pd.DataFrame, page_width: int,
                                        min_gap_frac: float = 0.05,
                                        min_support_frac: float = 0.2) -> list[float]:
    """Finds column boundaries from each row's own internal word-gaps,
    rather than a page-wide empty gutter. This is the robust complement to
    the page-wide gutter search in find_column_bands: that search assumes
    there's an x-range no row's content ever touches, which fails when
    column-content width varies row to row enough that the union of all
    rows bridges what is, per row, a real gap (a genuine case found on a
    real UNILAG page: 3 name-columns, no page-wide empty strip between any
    of them, yet every individual row still respects clean boundaries).

    Every internal gap in a row that clears min_gap_frac is a candidate
    (not just the row's single largest one) - a row can genuinely have
    more than one real column boundary crossing it (e.g. a 3-column page's
    row crosses two gutters), and taking only the largest per row would
    silently discard the second. Candidates are then clustered by x-
    position across rows and kept only where enough different rows agree,
    so a boundary has to recur structurally, not just happen once."""
    rough_lines = _rough_lines_ignoring_columns(words)
    candidates = []
    qualifying_lines = 0
    min_gap_width = page_width * min_gap_frac
    for line_words in rough_lines:
        lw = line_words[~line_words["text"].apply(_is_low_signal_word)].sort_values("left")
        if len(lw) < 2:
            continue
        lefts = lw["left"].to_numpy()
        rights = (lw["left"] + lw["width"]).to_numpy()
        gaps = lefts[1:] - rights[:-1]
        if len(gaps) == 0:
            continue
        big_gap_positions = [lefts[i + 1] for i, g in enumerate(gaps) if g >= min_gap_width]
        if big_gap_positions:
            qualifying_lines += 1
            candidates.extend(big_gap_positions)

    if qualifying_lines < 3:
        return []

    # Cluster candidate split positions into bins and keep bins with enough
    # support to be a real recurring structural boundary, not one-off noise.
    bin_width = page_width * 0.03
    bins: dict[int, list[float]] = {}
    for c in candidates:
        b = int(c // bin_width)
        bins.setdefault(b, []).append(c)

    min_support = max(2, int(round(qualifying_lines * min_support_frac)))
    strong_bins = [(b, vals) for b, vals in bins.items() if len(vals) >= min_support]
    boundaries = sorted(float(np.mean(vals)) for _, vals in strong_bins)
    return boundaries


def find_column_bands(words: pd.DataFrame, page_width: int,
                       n_bins: int = 100, min_gap_frac: float = 0.05,
                       min_band_frac: float = 0.12, wide_line_frac: float = 0.55) -> list[tuple[float, float]]:
    """Finds vertical column bands as (left, right) pixel ranges, combining
    two complementary signals (see module docstring): a page-wide empty
    gutter in word-center positions, and recurring internal gaps within
    individual rows. Either signal alone misses real cases (the former
    when column width varies row to row; the latter's old single-
    largest-gap-per-row form when a page has more than 2 columns), so both
    run and their candidate boundaries are merged, with the same narrow-
    band cleanup as before discarding anything that doesn't hold up.

    Two defenses against the most common false-positive here - splitting on
    the small gap between a row's leading list-number ("72.") and its name
    text, rather than on a real column gutter:
      1. min_gap_frac requires the empty run to be a meaningfully wide gap
         (real column gutters run ~10-20%+ of page width on these booklets;
         a number-to-name gap is a couple of percent at most).
      2. Any band that still comes out too narrow (min_band_frac) is merged
         into its nearest neighbour rather than kept as its own "column" -
         a real column holds many words across many lines, not a handful.
    """
    if len(words) == 0:
        return [(0, page_width)]

    # Header lines (faculty/department/course/degree/grade/year banners) can
    # span most of the page width, straddling exactly the gutter a 2/3-column
    # body list would otherwise show clearly. Find the gutter using only
    # words from lines that are narrow enough to sit inside a single column,
    # then apply that gutter to every word (headers included) afterwards.
    rough_lines = _rough_lines_ignoring_columns(words)
    narrow_line_word_idx = []
    for line_words in rough_lines:
        span = (line_words["left"] + line_words["width"]).max() - line_words["left"].min()
        if span < page_width * wide_line_frac:
            narrow_line_word_idx.extend(line_words.index.tolist())
    gutter_source = words.loc[narrow_line_word_idx] if narrow_line_word_idx else words
    # Fall back to all words if narrowing left too little to work with
    if len(gutter_source) < max(10, len(words) * 0.15):
        gutter_source = words
    # Stray punctuation-only OCR tokens (see _is_low_signal_word) shouldn't
    # count as evidence for or against a gutter position either.
    content_source = gutter_source[~gutter_source["text"].apply(_is_low_signal_word)]
    if len(content_source) >= 4:
        gutter_source = content_source

    centers = np.sort((gutter_source["left"] + gutter_source["width"] / 2).to_numpy())
    min_gap_width = page_width * min_gap_frac

    # Rather than bin into a histogram (sensitive to bin-edge placement and
    # to a single dense bin skewing any density-relative threshold), look
    # directly at the gaps between consecutive sorted word-center positions.
    # A true column gutter shows up as one or two gaps far larger than the
    # typical word-to-word spacing within a column; a number-to-name gap or
    # a bit of OCR noise does not.
    global_boundaries: list[float] = []
    if len(centers) >= 4:
        diffs = np.diff(centers)
        typical_spacing = np.median(diffs[diffs > 0]) if np.any(diffs > 0) else 1.0
        gap_threshold = max(min_gap_width, typical_spacing * 8)
        global_boundaries = [(centers[i] + centers[i + 1]) / 2
                              for i, d in enumerate(diffs) if d >= gap_threshold]

    internal_boundaries = _recurring_internal_gap_boundaries(words, page_width, min_gap_frac=min_gap_frac)

    all_boundaries = sorted(set(global_boundaries) | set(internal_boundaries))
    if not all_boundaries:
        return [(0, page_width)]

    # Two methods proposing near-identical split points (common when a
    # page's layout is clean enough for both to agree) shouldn't produce a
    # sliver band between them - collapse boundaries within 3% of the page
    # width of each other into one, the same tolerance used below to prune
    # degenerate bands.
    merge_tol = page_width * 0.03
    merged_boundaries: list[float] = []
    for b in all_boundaries:
        if merged_boundaries and (b - merged_boundaries[-1]) < merge_tol:
            merged_boundaries[-1] = (merged_boundaries[-1] + b) / 2
        else:
            merged_boundaries.append(b)

    edges = [0.0] + merged_boundaries + [float(page_width)]
    bands = [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]
    # Drop fully-empty degenerate bands (can happen at page edges)
    bands = [(l, r) for (l, r) in bands if r - l > page_width * 0.03]
    if not bands:
        return [(0, page_width)]

    # Merge any band narrower than min_band_frac into its adjacent band -
    # a genuine column is wide enough to hold a full name/line, a sliver
    # this narrow is virtually always a number-prefix artifact.
    min_band_width = page_width * min_band_frac
    merged: list[list[float]] = [list(bands[0])]
    for (l, r) in bands[1:]:
        if (r - l) < min_band_width:
            merged[-1][1] = r  # absorb into the previous band
        elif (merged[-1][1] - merged[-1][0]) < min_band_width:
            merged[-1][1] = r  # previous band was too narrow - absorb it into this one
            merged[-1][0] = min(merged[-1][0], l)
        else:
            merged.append([l, r])
    result = [tuple(b) for b in merged]

    return result if result else [(0, page_width)]


def words_to_lines(words: pd.DataFrame) -> list[Line]:
    """Groups words (already restricted to one column) into lines (see
    _group_words_into_lines)."""
    if len(words) == 0:
        return []
    w = words.copy()
    w["bottom"] = w["top"] + w["height"]

    lines = []
    for idxs in _group_words_into_lines(w):
        sub = w.loc[idxs].sort_values("left")
        text = " ".join(str(t) for t in sub["text"])
        conf = -1.0
        if "conf" in sub.columns:
            # Tesseract marks non-text/uncertain boxes with conf < 0; only
            # average the words it actually scored. (It can also report an
            # exact 0.0 for a word it read perfectly - see ocr.py - so this
            # is a real but noisy signal, meant to combine with others.)
            valid = sub["conf"].astype(float)
            valid = valid[valid >= 0]
            if len(valid):
                conf = float(valid.mean())
        lines.append(Line(
            text=text,
            top=float(sub["top"].min()),
            bottom=float(sub["bottom"].max()),
            left=float(sub["left"].min()),
            column_index=-1,
            right=float((sub["left"] + sub["width"]).max()),
            conf=conf,
        ))
    return lines


def _split_words_by_bands(words: pd.DataFrame, bands: list[tuple[float, float]]) -> list[Line]:
    """Splits a set of words into columns per the given bands and returns
    all resulting lines in left-column-first, top-to-bottom order."""
    all_lines: list[Line] = []
    for col_i, (left, right) in enumerate(bands):
        col_words = words[(words["left"] + words["width"] / 2 >= left) &
                           (words["left"] + words["width"] / 2 < right)]
        col_lines = words_to_lines(col_words)
        for line in col_lines:
            line.column_index = col_i
        all_lines.extend(col_lines)
    return all_lines



_LIST_NUMBER_WORD = re.compile(r"^\d{1,3}[.,)]?$")
_LIST_NUMBER_PREFIX = re.compile(r"^\s*\d{1,3}\s*[.,)]?\s+\S")
_MIN_NUMBERS_PER_COLUMN = 8
_NUMBER_EDGE_TOLERANCE = 0.012   # of page width: how closely one column's numbers line up


def _number_column_bands(words: pd.DataFrame, page_width: int) -> list[tuple[float, float]] | None:
    """Column bands anchored on the list-number columns of a numbered
    multi-column page, or None if the page isn't one.

    Real failure (LASU 2024, three tightly packed columns of "N. SURNAME
    Given names"): the gaps between one column's names and the next
    column's numbers are about as narrow as the gap between a number and its
    own name, so gap-based band detection put its cut after the next
    column's NUMBERS - "16." landed at the end of column 1's line, and the
    name that followed it was orphaned in column 2. A numbered column is
    marked far more reliably by the numbers themselves: they form a
    vertical line (right-aligned, so clustered on their right edge), and the
    column starts at that line's leftmost number."""
    nums = words[words["text"].astype(str).str.match(_LIST_NUMBER_WORD)]
    if len(nums) < 2 * _MIN_NUMBERS_PER_COLUMN:
        return None
    tol = page_width * _NUMBER_EDGE_TOLERANCE
    edges = sorted(zip((nums["left"] + nums["width"]).tolist(), nums["left"].tolist()))
    clusters, current = [], [edges[0]]
    for right, left in edges[1:]:
        if right - current[-1][0] <= tol:
            current.append((right, left))
        else:
            clusters.append(current)
            current = [(right, left)]
    clusters.append(current)
    clusters = [c for c in clusters if len(c) >= _MIN_NUMBERS_PER_COLUMN]
    if len(clusters) < 2:
        return None
    starts = sorted(min(left for _, left in c) for c in clusters)
    if any(b - a < page_width * 0.12 for a, b in zip(starts, starts[1:])):
        return None
    cuts = [st - page_width * 0.01 for st in starts[1:]]
    edges_out = [0.0] + cuts + [float(page_width)]
    return list(zip(edges_out[:-1], edges_out[1:]))


_WRAP_MIN_PREV_FILL = 0.72   # for a two-word tail, the line above must nearly fill the column
_WRAP_MAX_GAP = 1.0          # tail must start within this many line-heights of the line above


def _merge_wrapped_names(lines: list[Line], numbered: bool) -> list[Line]:
    """Rejoins a name that wrapped onto a second line inside a narrow
    numbered column ("17. ADEGBAKINRO BLESSING" / "ADEOLA"), which would
    otherwise become two records - a name with its last given name cut off,
    plus a one-word "name". LASU 2024 prints ~25% of its names this way, and
    it was the single largest source of misses on that year.

    Runs over the FINAL line stream, per column, rather than inside one
    section: a grade banner in another column can flush a section between a
    name and its tail (they sit at the same height), so the two halves land
    in different sections even though they are adjacent in the same column.

    Only on numbered pages. It can't lean on the numbers themselves,
    because OCR drops the small single-digit ones ("3." - "9.") entirely, so
    a numberless line is not by itself a tail. The evidence used instead:
      - a ONE-word line is a tail: a record is always at least a surname
        and a given name, so a lone word below a name can only be its
        continuation;
      - a TWO-word line is a tail only when the line above visibly carries
        its list number and runs nearly the full column width (the reason it
        wrapped) - two words can be a complete short name otherwise.
    Either way the tail must sit directly below (within one line height)."""
    if not numbered:
        return lines
    body = [l for l in lines if not l.is_header]
    if len(body) < 2:
        return lines
    col_bounds: dict[int, tuple[float, float]] = {}
    for l in body:
        lo, hi = col_bounds.get(l.column_index, (l.left, l.right))
        col_bounds[l.column_index] = (min(lo, l.left), max(hi, l.right))

    out: list[Line] = []
    last_body: dict[int, Line] = {}
    for line in lines:
        if line.is_header:
            if line.column_index < 0:
                last_body.clear()
            else:
                last_body.pop(line.column_index, None)
            out.append(line)
            continue
        col = line.column_index
        prev = last_body.get(col)
        words = line.text.split()
        prev_h = max(prev.bottom - prev.top, 1.0) if prev is not None else 1.0
        lo, hi = col_bounds[col]
        width = max(hi - lo, 1.0)
        merge = False
        if (prev is not None and not _LIST_NUMBER_PREFIX.match(line.text)
                and words and all(re.fullmatch(r"[A-Za-z][A-Za-z'\-.]*[,.]?", w) for w in words)
                and -0.5 * prev_h <= line.top - prev.bottom <= _WRAP_MAX_GAP * prev_h):
            if len(words) == 1:
                merge = True
            elif len(words) == 2:
                merge = (bool(_LIST_NUMBER_PREFIX.match(prev.text))
                         and (prev.right - lo) / width >= _WRAP_MIN_PREV_FILL)
        if merge:
            prev.text = f"{prev.text} {line.text}"
            prev.bottom = max(prev.bottom, line.bottom)
            prev.right = max(prev.right, line.right)
            if prev.conf >= 0 and line.conf >= 0:
                prev.conf = min(prev.conf, line.conf)
            prev.repaired = True
            continue
        out.append(line)
        last_body[col] = line
    return out


_MATRIC_IN_TEXT = re.compile(r"\b[A-Z][A-Z0-9]{1,5}\s*/\s*\d{2,4}\s*/\s*\d{2,7}\b", re.IGNORECASE)


def _name_shape(text: str) -> str:
    """Classifies a line's letters, ignoring list numbers and matric codes:
    "surname" if every word is ALL CAPS, "given" if none is, else "other"
    (a complete "SURNAME Given Names" line, or anything mixed)."""
    t = _MATRIC_IN_TEXT.sub(" ", text)
    tokens = [x for x in re.findall(r"[A-Za-z][A-Za-z'\-]+", t) if len(x) >= 2]
    if not tokens:
        return "other"
    upper = [x for x in tokens if x.isupper()]
    if len(upper) == len(tokens):
        return "surname"
    if not upper:
        return "given"
    return "other"


def _is_surname_then_given(a_lines: list, b_lines: list, y_tol: float,
                            min_lines: int = 5, min_purity: float = 0.7,
                            min_alignment: float = 0.6) -> bool:
    """True if band A holds surnames only and band B holds the matching given
    names, row for row - i.e. one name split across two adjacent table
    columns rather than two independent columns of names."""
    if len(a_lines) < min_lines or len(b_lines) < min_lines:
        return False
    a_shapes = [_name_shape(l.text) for l in a_lines]
    b_shapes = [_name_shape(l.text) for l in b_lines]
    if a_shapes.count("surname") / len(a_lines) < min_purity:
        return False
    if b_shapes.count("given") / len(b_lines) < min_purity:
        return False
    b_tops = [l.top for l in b_lines]
    surname_lines = [l for l, sh in zip(a_lines, a_shapes) if sh == "surname"]
    aligned = sum(1 for l in surname_lines if any(abs(l.top - t) <= y_tol for t in b_tops))
    return aligned / len(surname_lines) >= min_alignment


def _merge_split_name_bands(words: pd.DataFrame, bands: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merges adjacent column bands that are really ONE name split across two
    table columns. Some booklet sections (postgraduate lists) are tables of
    `No. | Matric | SURNAME | Given names | Degree`, and column detection
    correctly finds surname and given names as separate columns - but then
    each is read as its own list, so every person becomes two records
    ("OYEGOKE" and "Olufunso Olawale") and none of them match. Row alignment
    alone can't decide this: two genuine side-by-side name columns are also
    row-aligned (both follow the same line grid), and merging those would
    fuse two different people into one record. What distinguishes the table
    case is the CONTENT - the left band's lines are all-caps surnames only,
    the right band's are given names only - whereas independent columns each
    hold complete names (see _is_surname_then_given)."""
    if len(bands) < 2 or len(words) == 0:
        return bands
    centers = words["left"] + words["width"] / 2
    y_tol = 0.6 * float(words["height"].median())

    def lines_in(band):
        return words_to_lines(words[(centers >= band[0]) & (centers < band[1])])

    merged: list[list[float]] = [list(bands[0])]
    prev_lines = lines_in(bands[0])
    just_merged = False
    for band in bands[1:]:
        cur_lines = lines_in(band)
        if not just_merged and _is_surname_then_given(prev_lines, cur_lines, y_tol):
            merged[-1][1] = band[1]
            just_merged = True
        else:
            merged.append(list(band))
            just_merged = False
        prev_lines = cur_lines
    return [tuple(b) for b in merged]


def _split_row_into_column_slices(line_words: pd.DataFrame, bands: list[tuple[float, float]],
                                   min_gap_width: float) -> list[tuple[int, pd.DataFrame]]:
    """Splits ONE row-band's words by the page's column bands, but only
    where doing so reflects a genuine column separation - not wherever a
    band boundary happens to fall.

    A row-band that spans several bands is ambiguous: it might be several
    independent columns' content that coincidentally lines up at the same
    height (row-aligned parallel lists - a real case found on a UNILAG
    page, where each of 3 side-by-side name-lists has its own programme
    header at a different height, so a row-band can hold one column's
    header text alongside another column's ordinary name at the same y),
    or it might be ONE continuous phrase (e.g. a wide "FACULTY OF X"
    banner) that a band boundary just happens to cut through, because
    bands are positioned from body-content columns, not from where a
    banner's own words happen to fall. The two cases are told apart by the
    actual gap between the words on either side of the split: a real
    column boundary is gutter-sized; an arbitrary cut through one phrase
    leaves only normal word-to-word spacing. Adjacent per-band slices
    without a gutter-sized gap between them are merged back into one."""
    raw_slices = []
    for col_i, (left, right) in enumerate(bands):
        mask = (line_words["left"] + line_words["width"] / 2 >= left) & \
               (line_words["left"] + line_words["width"] / 2 < right)
        sub = line_words[mask]
        if len(sub) > 0:
            raw_slices.append((col_i, sub))

    if len(raw_slices) <= 1:
        return raw_slices

    merged = [raw_slices[0]]
    for col_i, sub in raw_slices[1:]:
        prev_col_i, prev_sub = merged[-1]
        prev_right = (prev_sub["left"] + prev_sub["width"]).max()
        this_left = sub["left"].min()
        if this_left - prev_right < min_gap_width:
            merged[-1] = (prev_col_i, pd.concat([prev_sub, sub]))
        else:
            merged.append((col_i, sub))
    return merged


def reconstruct_reading_order(words: pd.DataFrame, page_width: int) -> list[Line]:
    """Full pipeline, section-aware: a page can hold several short,
    independent sections stacked vertically (e.g. two departments each with
    their own tiny left/right sub-columns) - reading "all of column 0, then
    all of column 1" across the whole page would incorrectly interleave
    one section's data with another's. So:

      1. Group words into rough, column-agnostic lines.
      2. Classify each rough line as a header (faculty/department/course/
         degree/grade/year banner) or body content - headers are emitted
         as-is, in page order, since they apply across the full width.
      3. Between headers, each run of consecutive body lines is one
         "section": re-split just that section's words into columns (using
         bands detected from the whole page) and emit left-column-first,
         top-to-bottom, before moving on to the next header or section.

    A header-free page (a single list spanning the whole page) is just one
    section end to end, which reduces to the simple column-then-column
    order already verified against ground truth.

    A row-band can also hold MULTIPLE independent columns' content at once
    (see _split_row_into_column_slices) - each of several side-by-side
    name-lists can have its own header at a different height, so a header
    in one column can coincide, at the same y, with an ordinary name in
    another. Step 2 above handles that by classifying each such row-band's
    column-slices independently rather than forcing one page-wide
    decision on their fused text.
    """
    from .classify import is_header_line  # local import: avoids a cycle at module load time

    if len(words) == 0:
        return []

    number_bands = _number_column_bands(words, page_width)
    bands = number_bands or _merge_split_name_bands(words, find_column_bands(words, page_width))
    numbered = number_bands is not None
    min_gap_width = page_width * 0.05
    rough_lines = _rough_lines_ignoring_columns(words)
    rough_lines.sort(key=lambda lw: lw["top"].min())

    all_lines: list[Line] = []
    pending_section_idx: list[int] = []

    def flush_section():
        if not pending_section_idx:
            return
        section_words = words.loc[pending_section_idx]
        all_lines.extend(_split_words_by_bands(section_words, bands))
        pending_section_idx.clear()

    for line_words in rough_lines:
        slices = _split_row_into_column_slices(line_words, bands, min_gap_width) if len(bands) > 1 else []

        if len(slices) <= 1:
            # Only one column genuinely active in this row-band (the
            # common case, even on multi-column pages, since two
            # independently-flowing columns rarely land at the exact same
            # height) - classify the whole thing as one unit, as before.
            text = " ".join(str(t) for t in line_words.sort_values("left")["text"])
            if is_header_line(text):
                flush_section()
                all_lines.append(Line(
                    text=text,
                    top=float(line_words["top"].min()),
                    bottom=float((line_words["top"] + line_words["height"]).max()),
                    left=float(line_words["left"].min()),
                    column_index=-1,
                    is_header=True,
                ))
            else:
                pending_section_idx.extend(line_words.index.tolist())
            continue

        # Row-aligned parallel columns: classify and handle each column's
        # own slice independently, rather than forcing one page-wide
        # header/body decision on text fused from unrelated columns - that
        # either swallows real names into a false "header" (a programme
        # label in one column, real names in another, at the same height)
        # or buries a real column-local header inside what then reads as
        # nonsense body text once fused with its neighbours.
        for col_i, sub in slices:
            slice_text = " ".join(str(t) for t in sub.sort_values("left")["text"])
            if is_header_line(slice_text):
                flush_section()
                all_lines.append(Line(
                    text=slice_text,
                    top=float(sub["top"].min()),
                    bottom=float((sub["top"] + sub["height"]).max()),
                    left=float(sub["left"].min()),
                    column_index=col_i,
                    is_header=True,
                ))
            else:
                pending_section_idx.extend(sub.index.tolist())
    flush_section()

    return _merge_wrapped_names(all_lines, numbered)
