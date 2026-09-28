"""End-to-end orchestration: PDF in, canonical-schema CSV out.

    python -m pdf_extractor.cli path/to/booklet.pdf -o output.csv
    python -m pdf_extractor.cli path/to/booklet.pdf -o output.csv --pages 0-5

Header state (faculty/department/course/degree/grade/year) carries forward
across pages within one run, matching how these booklets actually read -
a department heading on page 20 still applies to names on page 21 if no
new heading interrupts it.
"""

import argparse
import csv
import re
import sys

from . import render, orientation, ocr, columns, parser, pages, dewarp, spread
from .classify import classify_line

CANON_FIELDS = parser.CANON_FIELDS

# A recurring, fixed-position line still isn't a banner if classify_line()
# recognizes it as one of these - real header content, not letterhead/crest
# text - see _detect_banners.
_RECOGNIZED_HEADER_FIELDS = {
    "faculty", "department", "department_or_course", "degree", "grade",
    "year", "programme", "affiliated_institution",
}


def _banner_key(text: str) -> str:
    """Normalizes a line for running-header/footer comparison: OCR noise
    (stray punctuation, digit misreads) varies page to page, but a genuine
    letterhead's letters stay stable, so compare on those alone."""
    return re.sub(r"[^A-Z]", "", text.upper())


def _detect_banners(page_data: list[tuple], min_frac: float = 0.3,
                     max_y_spread_frac: float = 0.08) -> set[str]:
    """Booklets repeat a letterhead/crest banner on most pages - identical
    text that doesn't fit cleanly into either 'header field' or 'name row'
    (e.g. "OBAFEMI AWOLOWO UNIVERSITY, ILE-IFE"). `page_data` is a list of
    (page_size, lines) pairs, one per page.

    Frequency alone isn't enough to call something a banner: a legitimate
    content header can also recur across many pages (e.g. a department
    name re-printed atop each of its own continuation pages), and
    frequency-only detection would wrongly strip that as noise. Two more
    signals narrow it down to genuine letterhead:

    1. Positional consistency - a real letterhead sits in a fixed spot on
       the page template (top or bottom margin) every time it's printed,
       while a repeating content header tends to drift up or down
       depending on how much other content precedes it on a given page.
    2. The institutional vocabulary mined from the training corpus (see
       classify.py's vocab_score(), used internally by classify_line) - a
       line classify_line() recognizes as a real header field is kept
       even if it's frequent and fixed-position, since that's a stronger,
       content-aware signal than position alone. A genuine letterhead
       essentially never matches one of those patterns itself (the
       OBAFEMI AWOLOWO example above classifies as a bare name row, not a
       header field - which is exactly why frequency and position are
       still needed to catch it in the first place).

    A single exact key isn't always enough either: OCR sometimes glues a
    stray misread character onto a banner line, and which character (if
    any) varies page to page - e.g. FUTA's slogan line came back as
    "ATHEBESTUNIVERSITY...", "ILTHEBESTUNIVERSITY...", and clean
    "THEBESTUNIVERSITY..." across different pages of the same document.
    None of those exact keys alone reaches the frequency threshold even
    though the same banner prints on nearly every page. So candidates are
    first clustered by substring containment (one key is another plus a
    short prefix/suffix of noise) before the frequency count is taken,
    and the shortest - least noisy - member stands in for the cluster."""
    if len(page_data) < 3:
        return set()

    occurrences: dict[str, list[tuple[float, str]]] = {}
    for size, lines in page_data:
        page_height = size[1] or 1
        seen_this_page = set()
        for line in lines:
            key = _banner_key(line.text)
            if len(key) >= 6 and key not in seen_this_page:
                occurrences.setdefault(key, []).append((line.top / page_height, line.text))
                seen_this_page.add(key)

    # Cluster same-phrase-modulo-noise keys together. Only applied when the
    # shorter (core) key is long enough to be a genuine phrase rather than
    # a short word that might coincidentally be a substring of an unrelated
    # longer line.
    _NOISE_CLUSTER_MIN_LEN = 15
    _NOISE_MAX_EXTRA_CHARS = 8
    _FOOTER_TOP_FRAC = 0.7
    keys_by_len = sorted(occurrences, key=len, reverse=True)
    used = set()
    clusters: list[list[str]] = []
    for key in keys_by_len:
        if key in used:
            continue
        cluster = [key]
        used.add(key)
        for other in keys_by_len:
            if other in used:
                continue
            if (len(other) >= _NOISE_CLUSTER_MIN_LEN and other in key
                    and len(key) - len(other) <= _NOISE_MAX_EXTRA_CHARS):
                cluster.append(other)
                used.add(other)
        clusters.append(cluster)

    threshold = max(2, int(len(page_data) * min_frac))
    banners = set()
    for cluster in clusters:
        occs = [occ for key in cluster for occ in occurrences[key]]
        if len(occs) < threshold:
            continue

        tops = [t for t, _ in occs]
        if max(tops) - min(tops) > max_y_spread_frac:
            continue

        sample_text = occs[0][1]
        classified = classify_line(sample_text)
        avg_top = sum(tops) / len(tops)
        # A real header field describes the content that follows it, so it
        # sits near the top of a page or section - never buried in the
        # bottom margin alongside a footer/page number. classify_line can
        # still mistake a letterhead slogan for one (FUTA's "THE BEST
        # UNIVERSITY OF TECHNOLOGY IN NIGERIA" reads as a plausible
        # department/course name to it), so the carve-out is only trusted
        # away from the footer band.
        if (avg_top < _FOOTER_TOP_FRAC and classified["is_header"]
                and classified["field"] in _RECOGNIZED_HEADER_FIELDS):
            continue

        banners.add(min(cluster, key=len))
    return banners


def parse_page_range(spec: str | None, total_pages: int) -> list[int]:
    if not spec:
        return list(range(total_pages))
    indices = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            indices.extend(range(int(a), int(b) + 1))
        else:
            indices.append(int(part))
    return [i for i in indices if 0 <= i < total_pages]


# Adaptive resolution. The base render (zoom 2.0) is plenty for normal print,
# but very dense pages - LASU 2012 packs ~250 names on a page, word boxes only
# ~11px tall at zoom 2 - are simply too small for Tesseract to read reliably:
# measured on 5 such pages, recall was 30.8% at zoom 2.0, 49.9% at 3.0 and
# 54.3% at 4.0, while typical pages (14-18px words) barely moved. So when a
# page's own text is small, it's re-rendered at a zoom that brings the words
# to a readable size, instead of paying the extra OCR time on every page.
_SMALL_TEXT_HEIGHT = 13.0      # median alphabetic-word height (px) below which to re-render
_TARGET_WORD_HEIGHT = 18.0     # word height to aim for on the re-render
_MAX_ZOOM = 4.0
_MIN_WORDS_FOR_SIZE = 40       # too few words = unreliable estimate (cover pages etc.)


def _larger_zoom_if_small_text(words, zoom: float) -> float | None:
    """Returns a higher zoom to re-render at if this page's text is small,
    else None."""
    alpha = words[words["text"].astype(str).str.fullmatch(r"[A-Za-z][A-Za-z'\-]{2,}")]
    if len(alpha) < _MIN_WORDS_FOR_SIZE:
        return None
    # The median alone misses pages that mix small print with larger text
    # (LASU 2012: median 14.5px but a 25th percentile of 10px), so the
    # smaller of the median and a scaled lower quartile is used.
    ref_h = min(float(alpha["height"].median()), 1.15 * float(alpha["height"].quantile(0.25)))
    if ref_h >= _SMALL_TEXT_HEIGHT or ref_h <= 0:
        return None
    new_zoom = min(_MAX_ZOOM, zoom * _TARGET_WORD_HEIGHT / ref_h)
    return new_zoom if new_zoom > zoom * 1.15 else None


def _process_page(pdf_path: str, page_index: int, zoom: float, verbose: bool) -> list[tuple]:
    """Render/orient/split/OCR/reading-order for ONE PDF page. Returns its
    page_data entries: one (page_key, corrected_size, lines, rotated_by,
    page_meta) tuple normally, or two for a two-page spread.

    page_meta records exactly how to reproduce this page's image later
    (render.render_page(pdf_path, page_index, zoom=...) -> dewarp if
    dewarped -> apply_rotation -> pick spread half) without keeping the
    rendered image itself around - review.py uses it to regenerate a
    snapshot crop for one flagged record, long after extraction."""
    img = render.render_page(pdf_path, page_index, zoom=zoom)
    # Perspective correction runs before orientation detection: a
    # photographed page's keystone/skew distorts the very edges OSD
    # uses to judge rotation, so straightening it first gives
    # orientation detection a cleaner input. It's a no-op on the
    # normal flatbed-scan case (see dewarp.py's module docstring).
    img, was_dewarped = dewarp.correct_perspective(img)
    corrected, rotated_by = orientation.correct_orientation(img)

    # Two-page-spread splitting runs AFTER orientation correction, not
    # before - see spread.py's module docstring for why checking
    # aspect ratio pre-orientation would misfire on an ordinary
    # sideways single page. One PDF page can expand into two logical
    # pages here (a genuine spread) or stay one (the common case).
    halves = spread.split_spread(corrected)
    entries = []
    for half_index, half_img in enumerate(halves):
        page_key = str(page_index) if len(halves) == 1 else f"{page_index}{chr(ord('a') + half_index)}"
        words = ocr.ocr_words(half_img)
        zoom_note = ""
        zoom_used = zoom
        if len(halves) == 1:  # two-page spreads keep the base render
            hi_zoom = _larger_zoom_if_small_text(words, zoom)
            if hi_zoom is not None:
                hi_img = render.render_page(pdf_path, page_index, zoom=hi_zoom)
                if was_dewarped:
                    hi_img, _ = dewarp.correct_perspective(hi_img)
                half_img = orientation.apply_rotation(hi_img, rotated_by)
                words = ocr.ocr_words(half_img)
                zoom_note = f", re-OCR'd at zoom {hi_zoom:.1f} (small text)"
                zoom_used = hi_zoom
        lines = columns.reconstruct_reading_order(words, half_img.size[0])
        # Only the cheap check runs here - a bare digit line near an edge
        # (pages._plain_page_number). The upscaled-band fallback is deferred
        # to _resolve_boxed_fallbacks, run once for the whole document AFTER
        # banner detection, because telling a genuine page number apart from
        # one with a recurring slogan tagged onto it (FUTA: "THE BEST
        # UNIVERSITY OF TECHNOLOGY IN NIGERIA --- [84]") needs to already
        # know what text repeats across this document's pages - which by
        # definition isn't knowable from any single page in isolation. See
        # pages._looks_isolated's banner_keys param.
        raw_page_number = pages._plain_page_number(lines, half_img.size[1])
        page_meta = {
            "pdf_path": pdf_path, "page_index": page_index, "rotated_by": rotated_by,
            "dewarped": was_dewarped, "zoom": zoom_used,
            "spread_half": half_index if len(halves) > 1 else None, "spread_halves": len(halves),
            "image_size": half_img.size, "raw_page_number": raw_page_number,
            "boxed_page_number_tried": False, "boxed_page_number_succeeded": False,
        }
        entries.append((page_key, half_img.size, lines, rotated_by, page_meta))
        if verbose:
            spread_note = " (spread half)" if len(halves) > 1 else ""
            dewarp_note = ", dewarped" if was_dewarped else ""
            print(f"[page {page_key}] OCR'd, rotated {rotated_by}deg{dewarp_note}{spread_note}{zoom_note}, "
                  f"{len(lines)} raw lines", file=sys.stderr)
    return entries


# Stops paying for the upscaled-band OCR fallback (pages.py's
# _boxed_page_number) on a document where it's already failed this many
# times running with zero successes - some page-number designs (LASU's
# boxed number on a busy background) defeat it every single time, and
# continuing to try on every remaining page is pure waste: found while
# cross-checking page numbers against ground truth at scale, where one
# such document dominated a whole run's time for no payoff.
_BOXED_CIRCUIT_BREAKER = 8


def _resolve_boxed_page_numbers(page_data: list[tuple], banner_keys: set[str], verbose: bool):
    """Second pass, run once per document after banner detection: for every
    physical page the cheap check (pages._plain_page_number) couldn't read,
    re-renders it and tries the upscaled-band fallback, now armed with
    `banner_keys` - the whole reason this waits until here rather than
    running inline in _process_page. Telling a genuine page number apart
    from one with a repeating slogan tagged onto it (FUTA: "THE BEST
    UNIVERSITY OF TECHNOLOGY IN NIGERIA --- [84]") needs to already know
    what text repeats across THIS document's pages, which by definition
    isn't knowable from any single page in isolation - exactly the same
    reasoning _detect_banners itself is built on.

    Mutates each page's page_meta in place rather than rebuilding page_data,
    since callers already hold references into it."""
    def _render(pm: dict):
        img = render.render_page(pm["pdf_path"], pm["page_index"], zoom=pm["zoom"])
        if pm["dewarped"]:
            img, _ = dewarp.correct_perspective(img)
        img = orientation.apply_rotation(img, pm["rotated_by"])
        if pm.get("spread_halves", 1) > 1:
            img = spread.split_spread(img)[pm["spread_half"]]
        return img, ocr.ocr_words(img)

    consecutive_failures = 0
    give_up = False
    for _, size, lines, _, pm in page_data:
        if pm["raw_page_number"] is not None:
            continue

        # A page number OCR merged onto the same line as a recurring banner
        # is locatable for free from `lines` (the normal OCR pass), but the
        # digit it read there isn't trusted - see pages._reread_banner_fused
        # _digit for why a page still needs rendering to verify it properly.
        img = words = None
        candidate = pages._banner_fused_candidate_line(lines, banner_keys, size[1])
        if candidate is not None:
            candidate_line, side = candidate
            img, words = _render(pm)
            value = pages._reread_banner_fused_digit(img, words, candidate_line, side)
            if value is not None:
                pm["raw_page_number"] = value
                pm["boxed_page_number_tried"] = True
                pm["boxed_page_number_succeeded"] = True
                consecutive_failures = 0
                continue

        if give_up:
            continue
        if img is None:
            img, words = _render(pm)
        value = pages._boxed_page_number(img, words, banner_keys)
        pm["raw_page_number"] = value
        pm["boxed_page_number_tried"] = True
        pm["boxed_page_number_succeeded"] = value is not None
        consecutive_failures = 0 if value is not None else consecutive_failures + 1
        if consecutive_failures >= _BOXED_CIRCUIT_BREAKER:
            give_up = True
            if verbose:
                print(f"Page-number box fallback found nothing in {_BOXED_CIRCUIT_BREAKER} straight tries - "
                      f"skipping it for the rest of this document (still recoverable by interpolation)",
                      file=sys.stderr)


def _discard_repeated_readings(page_data: list[tuple], verbose: bool):
    """Blanks page-number readings that repeat across the document's pages
    (title/banner/footer print, not page numbers) - see
    pages.drop_repeated_readings. Runs once before the fallbacks, so a page
    freed of a bogus reading gets them tried, and once after, since a
    fallback can just as easily land on the same fixed number."""
    raw = [pm["raw_page_number"] for _, _, _, _, pm in page_data]
    kept = pages.drop_repeated_readings(raw)
    dropped = sum(1 for a, b in zip(raw, kept) if a is not None and b is None)
    if dropped and verbose:
        print(f"Ignored {dropped} page-number reading(s) that repeat across pages "
              f"(fixed print such as a title or footer, not page numbers)", file=sys.stderr)
    for (_, _, _, _, pm), value in zip(page_data, kept):
        pm["raw_page_number"] = value


def extract_pdf_with_state(pdf_path: str, page_indices: list[int] | None = None, zoom: float = 2.0,
                            verbose: bool = True,
                            initial_state: "parser.HeaderState | None" = None) -> tuple[list[dict], "parser.HeaderState"]:
    """Like extract_pdf, but also accepts/returns HeaderState so a caller can
    carry faculty/department/.../year forward across file boundaries - used
    by batch.py to treat a folder of split PDFs (a single booklet scanned
    and cut into several files) as one continuous document."""
    total = render.page_count(pdf_path)
    indices = page_indices if page_indices is not None else list(range(total))

    # Pass 1: render/orient/split/OCR/reading-order for every page up
    # front, so we can detect running letterhead banners by frequency
    # before parsing - a banner is defined by recurring *across* pages, so
    # it can't be recognized correctly from any single page in isolation.
    # (The same reasoning is why the page-number box fallback is deferred
    # past this point too - see _resolve_boxed_page_numbers.)
    page_data = []  # list of (page_key, corrected_size, lines, rotated_by, page_meta)
    failed_pages = []
    for page_index in indices:
        # One bad page (a transient OpenCV/Tesseract failure, a corrupt
        # image) must not lose a whole folder's run - that's hours of OCR.
        # Found the hard way: a single "Unknown C++ exception from OpenCV"
        # on a page's very first step aborted an entire year's extraction.
        # Retry once, then log the page as failed and carry on.
        for attempt in (1, 2):
            try:
                page_data.extend(_process_page(pdf_path, page_index, zoom, verbose))
                break
            except Exception as e:  # noqa: BLE001 - deliberately broad, see above
                if attempt == 2:
                    failed_pages.append(page_index)
                    print(f"[page {page_index}] FAILED after retry ({type(e).__name__}: {e}) - skipped",
                          file=sys.stderr)

    if failed_pages:
        print(f"WARNING: {len(failed_pages)} page(s) could not be processed and are missing "
              f"from the output: {failed_pages}", file=sys.stderr)

    banner_keys = _detect_banners([(size, lines) for _, size, lines, _, _ in page_data])
    if verbose and banner_keys:
        print(f"Detected {len(banner_keys)} running banner line(s) to exclude", file=sys.stderr)

    _discard_repeated_readings(page_data, verbose)
    _resolve_boxed_page_numbers(page_data, banner_keys, verbose)
    _discard_repeated_readings(page_data, verbose)

    # Resolved once, across the whole document, from every physical page's
    # raw reading (content or not - a divider or photo page still occupies
    # a page number, which matters for the +1-per-page math even though it
    # never produces a record of its own). See pages.resolve_page_numbers.
    resolved = pages.resolve_page_numbers([pm["raw_page_number"] for _, _, _, _, pm in page_data])
    if verbose:
        n_detected = sum(1 for _, s in resolved if s == "detected")
        n_interp = sum(1 for _, s in resolved if s == "interpolated")
        n_uncertain = sum(1 for _, s in resolved if s == "uncertain")
        print(f"Page numbers: {n_detected} read directly, {n_interp} recovered by interpolation, "
              f"{n_uncertain} still uncertain", file=sys.stderr)

    # Pass 2: filter banners, classify content pages, and parse with header
    # state carried forward across pages.
    all_records: list[dict] = []
    state = initial_state or parser.HeaderState()
    skipped_pages = []

    for (page_key, size, lines, rotated_by, page_meta), (page_number, page_status) in zip(page_data, resolved):
        lines = [l for l in lines if _banner_key(l.text) not in banner_keys]

        if not pages.is_content_page(lines):
            skipped_pages.append(page_key)
            if verbose:
                print(f"[page {page_key}] skipped (not a content page)", file=sys.stderr)
            continue

        # An "uncertain" page number (no direct reading, and no consistent
        # anchor pair to interpolate from) still needs SOME label, since
        # every record needs a page - falls back to the old idx-by-physical-
        # position placeholder, but see confidence.py: unlike "detected" or
        # "interpolated", this one gets flagged for a human to check the
        # actual page against the PDF, because Akowe's report assembly
        # matches client-facing pages by this exact number.
        page_label = page_number if page_number is not None else f"idx{page_key}"
        page_meta["page_number_status"] = page_status

        records, state = parser.parse_lines_to_records(lines, page_label, initial_state=state, page_meta=page_meta)
        all_records.extend(records)
        if verbose:
            print(f"[page {page_key}] printed page {page_label}: "
                  f"{len(records)} records, rotated {rotated_by}deg", file=sys.stderr)

    if verbose and skipped_pages:
        print(f"Skipped {len(skipped_pages)} non-content page(s): {skipped_pages}", file=sys.stderr)

    return all_records, state


def extract_pdf(pdf_path: str, page_indices: list[int] | None = None, zoom: float = 2.0,
                 verbose: bool = True) -> list[dict]:
    records, _ = extract_pdf_with_state(pdf_path, page_indices, zoom, verbose)
    return records


def write_csv(records: list[dict], out_path: str):
    # extrasaction="ignore": records also carry a private "_geom" key (see
    # parser.parse_lines_to_records) used by review.py for confidence
    # scoring and PDF snapshots - never meant to appear in the plain CSV.
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CANON_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            writer.writerow(r)


def main():
    ap = argparse.ArgumentParser(description="Extract a canonical-schema CSV from an Akowe convocation-booklet PDF.")
    ap.add_argument("pdf_path")
    ap.add_argument("-o", "--out", default="output.csv")
    ap.add_argument("--pages", default=None, help="e.g. '0-5,10,12'; default: all pages")
    ap.add_argument("--zoom", type=float, default=2.0)
    args = ap.parse_args()

    total = render.page_count(args.pdf_path)
    indices = parse_page_range(args.pages, total)
    print(f"Processing {len(indices)} of {total} page(s)...", file=sys.stderr)

    records = extract_pdf(args.pdf_path, indices, zoom=args.zoom)
    write_csv(records, args.out)
    print(f"Wrote {len(records)} records to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
