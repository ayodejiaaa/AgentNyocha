"""Lists which pages of the ground truth the extraction does not reach.

For each ground-truth page (the printed page number in the canonical CSV's
`page` column) it computes what share of that page's names appear in the
extracted output; a page whose names are almost all absent is either not
in the source PDFs at all or failed to extract. Consecutive such pages are
reported as ranges, next to the page ranges the PDF file names claim to
cover, so a source gap (no PDF for those pages) can be told from a tool
failure (a PDF covers them but nothing came out).

    python -m pdf_extractor.missing_pages <extracted_dir> <ground_truth_dir> <pdf_root>
"""

import re
import sys
from pathlib import Path

from .validate import _tokens
from .validate_corpus import _OUTPUT_NAME_RE, find_ground_truth_csvs, load_rows

_MISSING_BELOW = 0.10   # a page with under this share of its names found counts as missing


def _ranges(nums: list[int]) -> list[tuple[int, int]]:
    out = []
    for n in sorted(nums):
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


def _fmt(ranges) -> str:
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in ranges) or "none"


_FILE_RANGE = re.compile(r"(?:pgs?\.?|pages?)\s*(\d+)\s*(?:-|to)\s*(\d+)|\(\s*(\d+)\s*-\s*(\d+)\s*\)", re.IGNORECASE)


def claimed_ranges(pdf_dir: Path) -> tuple[list[tuple[int, int]], list[str]]:
    """Page ranges the PDF file names claim to cover, plus files whose names
    state no range (their coverage can't be read from the name)."""
    ranges, unlabelled = [], []
    for pdf in sorted(pdf_dir.glob("*.pdf")):
        found = False
        for m in _FILE_RANGE.finditer(pdf.name):
            a, b = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            ranges.append((int(a), int(b)))
            found = True
        if not found:
            unlabelled.append(pdf.name)
    return ranges, unlabelled


def analyse(extracted_csv: Path, truth_paths: list[Path]) -> dict | None:
    extracted = [frozenset(_tokens(r["name"])) for r in load_rows(extracted_csv)]
    index: dict[str, list[int]] = {}
    for i, t in enumerate(extracted):
        for tok in t:
            index.setdefault(tok, []).append(i)

    def found(name_tokens: frozenset) -> bool:
        pivot = min(name_tokens, key=lambda tok: len(index.get(tok, ())))
        return any(name_tokens <= extracted[i] for i in index.get(pivot, ()))

    per_page: dict[int, list[bool]] = {}
    unnumbered = 0
    for tp in truth_paths:
        for r in load_rows(tp):
            toks = frozenset(_tokens(r["name"]))
            if not toks:
                continue
            m = re.match(r"\s*(\d+)", r.get("page", "") or "")
            if not m:
                unnumbered += 1
                continue
            per_page.setdefault(int(m.group(1)), []).append(found(toks))
    if not per_page:
        return None
    missing = [p for p, v in per_page.items() if sum(v) / len(v) < _MISSING_BELOW]
    return {
        "first": min(per_page), "last": max(per_page), "pages": len(per_page),
        "missing": missing,
        "missing_rows": sum(len(per_page[p]) for p in missing),
        "unnumbered_rows": unnumbered,
    }


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    extracted_dir, truth_root, pdf_root = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    print(f"{'folder':<16} {'truth pages':<12} {'missing':>7} {'rows':>6}  missing page ranges  |  PDF file names claim")
    for csv_path in sorted(extracted_dir.glob("*.csv")):
        m = _OUTPUT_NAME_RE.match(csv_path.name)
        if not m:
            continue
        uni, year = m.group(1), m.group(2)
        truth_paths = find_ground_truth_csvs(uni, year, truth_root, pdf_root)
        if not truth_paths:
            continue
        res = analyse(csv_path, truth_paths)
        if res is None or not res["missing"]:
            continue
        claimed, unlabelled = claimed_ranges(pdf_root / uni / year)
        note = _fmt(sorted(set(claimed)))
        if unlabelled:
            note += f"  (+{len(unlabelled)} file(s) with no range in the name)"
        print(f"{uni + ' ' + year:<16} {res['first']}-{res['last']:<7} {len(res['missing']):>7} {res['missing_rows']:>6}  "
              f"{_fmt(_ranges(res['missing']))}  |  {note}")


if __name__ == "__main__":
    main()
