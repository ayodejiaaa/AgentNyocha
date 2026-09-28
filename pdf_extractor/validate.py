"""One-off validation helper: compares extracted CSV output against the
ground-truth canonical CSV for the same pages, reporting name-recall per
page. Not part of the production pipeline - a scratch tool for spot-
checking the extractor against real Akowe data.

Usage:
    python -m pdf_extractor.validate extracted.csv ground_truth.csv
"""

import csv
import re
import sys

_WORD_RE = re.compile(r"[A-Z]{2,}")


def _tokens(text: str) -> set:
    return set(_WORD_RE.findall((text or "").upper()))


def load_rows(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def compare(extracted_path: str, truth_path: str):
    extracted = load_rows(extracted_path)
    truth = load_rows(truth_path)

    pages_extracted = sorted(set(r["page"] for r in extracted if r["page"]))
    print(f"Extracted {len(extracted)} record(s) across pages: {pages_extracted}")

    # Bag of all name-field tokens actually produced on each extracted page -
    # not a strict row-for-row comparison, since column-interleaving noise
    # (a known, separate limitation) can merge/split names across records.
    # This asks a narrower, still-meaningful question: did the OCR/reading-
    # order pipeline preserve this person's name text on the page at all.
    tokens_by_page: dict[str, set] = {}
    for r in extracted:
        tokens_by_page.setdefault(r["page"], set()).update(_tokens(r["name"]))

    total_truth = 0
    total_found = 0
    for page in pages_extracted:
        truth_rows = [r for r in truth if r["page"] == page]
        if not truth_rows:
            print(f"  page {page}: no ground-truth rows for this page label (skipped)")
            continue
        page_tokens = tokens_by_page.get(page, set())
        found = 0
        misses = []
        for r in truth_rows:
            name_tokens = _tokens(r["name"])
            if not name_tokens:
                continue
            if name_tokens <= page_tokens:
                found += 1
            else:
                misses.append(r["name"])
        total = len(truth_rows)
        total_truth += total
        total_found += found
        pct = 100 * found / total if total else 0
        print(f"  page {page}: {found}/{total} ({pct:.0f}%) ground-truth names found in extracted text")
        if misses:
            print(f"    missed: {misses[:8]}{' ...' if len(misses) > 8 else ''}")

    if total_truth:
        print(f"\nOverall: {total_found}/{total_truth} ({100*total_found/total_truth:.1f}%) "
              f"ground-truth names recovered across compared pages")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python -m pdf_extractor.validate extracted.csv ground_truth.csv")
        sys.exit(1)
    compare(sys.argv[1], sys.argv[2])
