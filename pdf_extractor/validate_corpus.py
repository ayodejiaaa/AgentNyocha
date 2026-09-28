"""Corpus-wide accuracy report: compares every batch-extracted CSV against
the corresponding ground-truth canonical CSV(s) built earlier this project,
using the same page-level name-recall check as validate.py.

This is the actual measure of whether the tool is doing its job: the
training data was built specifically so extraction output could be checked
against it, not just eyeballed. Not part of the production pipeline - a
scratch tool for this comparison.

Usage:
    python -m pdf_extractor.validate_corpus "Extracted Output" "Canonical Training Data"
"""

import csv
import re
import sys
from pathlib import Path

from .validate import _tokens

# Maps a PDF-side university folder name (as batch.py names its output
# files) to the search key(s) that identify it in the ground-truth
# filenames/folder names - the two sides were named independently and
# don't match directly (e.g. "UI" vs "university-of-ibadan", and OOU/
# UNILAG's ground truth additionally splits into Undergraduate/
# Postgraduate subfolders that don't exist on the PDF side at all).
UNIVERSITY_SEARCH_KEYS = {
    "COVENANT": ["covenant"],
    "FUTA": ["futa"],
    "LASU": ["lasu"],
    "OAU": ["oau"],
    "OOU": ["oou"],
    "UI": ["university-of-ibadan"],
    "UNILAG": ["unilag"],
    "UNILORIN": ["unilorin"],
}

_OUTPUT_NAME_RE = re.compile(r"^([A-Za-z]+)_(\d{4})\.csv$")


def detect_postgraduate_only(university: str, year: str, pdf_root: Path | None) -> bool:
    """True if every PDF in this university/year's source folder is
    explicitly labeled Postgraduate - found to matter on real data: UNILAG
    2019 and 2021's folders hold ONLY a Postgraduate booklet, with no
    Undergraduate PDF present at all (not lost by the tool - simply never
    in Data Training to begin with), yet naively combining both ground-
    truth splits for scoring checked ~9,000-17,000 undergraduate names
    against a document that was never provided, understating those years'
    real accuracy by 30+ points. Deliberately narrow: only the clearly
    single-type "postgraduate-only" case is detected and filtered out
    below, since that's the case actually observed - a mixed folder (like
    UNILAG 2022, which holds both) or an unlabeled one is left alone."""
    if pdf_root is None:
        return False
    folder = pdf_root / university / year
    if not folder.is_dir():
        return False
    pdfs = [p for p in folder.iterdir() if p.suffix.lower() == ".pdf"]
    if not pdfs:
        return False
    return all(re.search(r"post[\s-]?graduate", p.stem, re.IGNORECASE) for p in pdfs)


def find_ground_truth_csvs(university: str, year: str, truth_root: Path,
                            pdf_root: Path | None = None) -> list[Path]:
    """Finds every ground-truth CSV that could plausibly hold rows for this
    university/year - deliberately over-inclusive by default (e.g. both
    the Undergraduate and Postgraduate split for OOU/UNILAG) rather than
    guessing which sub-list a given PDF folder's pages belong to, since
    matching happens by page number against the combined pool afterward,
    not by which file a row came from. The one exception is a folder
    confirmed postgraduate-only (see detect_postgraduate_only): there, an
    explicitly Undergraduate-labeled ground-truth file is dropped, since
    it's checking against a document that isn't in this folder at all."""
    keys = UNIVERSITY_SEARCH_KEYS.get(university.upper())
    if not keys:
        return []
    pg_only = detect_postgraduate_only(university, year, pdf_root)
    matches = []
    for p in truth_root.rglob("*.csv"):
        if p.name == "akowe_canonical_master.csv":
            continue
        path_lower = str(p).lower()
        if not (any(k in path_lower for k in keys) and year in p.name):
            continue
        if pg_only and "undergraduate" in path_lower:
            continue
        matches.append(p)
    return matches


def load_rows(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _score_precision(record_token_sets: list[frozenset], truth_rows: list[dict]) -> dict:
    """The other half of the picture. Recall says how much of the truth the
    tool found; it says nothing about the junk it ADDED, so an extractor
    that dumps every fragment on the page scores the same as a clean one.
    Each extracted record is classified against the truth names:

      exact    - its tokens are exactly a truth name's (fully correct)
      superset - it contains a truth name plus extra tokens (right person,
                 name polluted: a glued gender column, degree text, ...)
      subset   - it is a strict part of a truth name (right person, name
                 truncated: a lost given name)
      none     - matches nobody: OCR-garbled, a header, a page fragment, or
                 a person the truth simply doesn't have

    `precision` counts exact + superset (the record identifies a real
    truth person); `precision_strict` counts exact only. Records that
    resolve to a truth person already claimed by an earlier record are also
    counted as `duplicates`. Caveat: where the PDFs hold people the truth
    lacks, "none" is not necessarily the tool's fault."""
    truth_sets = [frozenset(_tokens(r["name"])) for r in truth_rows]
    truth_sets = [t for t in truth_sets if t]
    token_freq: dict[str, int] = {}
    for t in truth_sets:
        for tok in t:
            token_freq[tok] = token_freq.get(tok, 0) + 1
    by_pivot: dict[str, list[int]] = {}   # truth ids, keyed by their rarest token
    by_any: dict[str, list[int]] = {}     # truth ids, keyed by every token
    for i, t in enumerate(truth_sets):
        by_pivot.setdefault(min(t, key=lambda tok: token_freq[tok]), []).append(i)
        for tok in t:
            by_any.setdefault(tok, []).append(i)

    counts = {"exact": 0, "superset": 0, "subset": 0, "none": 0}
    # A truth list can itself hold two different people with the same
    # name-token set, so "duplicate" means MORE records resolving to a name
    # than the truth has rows for it, not merely a second record.
    multiplicity: dict[frozenset, int] = {}
    for t in truth_sets:
        multiplicity[t] = multiplicity.get(t, 0) + 1
    used: dict[frozenset, int] = {}
    duplicates = 0
    for r in record_token_sets:
        contained = {i for tok in r for i in by_pivot.get(tok, ()) if truth_sets[i] <= r}
        if contained:
            exact = r in multiplicity and r in {truth_sets[i] for i in contained}
            counts["exact" if exact else "superset"] += 1
            key = r if exact else max((truth_sets[i] for i in contained), key=len)
            used[key] = used.get(key, 0) + 1
            if used[key] > multiplicity[key]:
                duplicates += 1
            continue
        pivot = min(r, key=lambda tok: len(by_any.get(tok, ())))
        if any(r < truth_sets[i] for i in by_any.get(pivot, ())):
            counts["subset"] += 1
        else:
            counts["none"] += 1
    n = len(record_token_sets)
    return {
        "records_exact": counts["exact"], "records_superset": counts["superset"],
        "records_subset": counts["subset"], "records_none": counts["none"],
        "records_duplicate": duplicates, "records_classified": n,
        "precision": (counts["exact"] + counts["superset"]) / n if n else None,
        "precision_strict": counts["exact"] / n if n else None,
    }


def score_file(extracted_path: Path, truth_paths: list[Path]) -> dict:
    """Compares at the whole-document level, not page-by-page: what
    matters is whether the tool pulled the right people and data out of
    the PDF at all, not whether its page label happens to match the
    ground truth's page label for the same row. Page-level matching is
    fragile in exactly the way that would hide real signal here - a page
    with no detectable printed number falls back to an "idx0"-style label
    that never matches ground truth, and a source PDF can carry more than
    one internal page-numbering series (a postgraduate supplement
    restarting its own count, found on a real UNILORIN file), so a
    "page" match or mismatch reflects page-numbering bookkeeping, not
    whether the actual name/content was captured correctly."""
    extracted = load_rows(extracted_path)
    truth_rows = []
    for tp in truth_paths:
        truth_rows.extend(load_rows(tp))

    # Match against INDIVIDUAL extracted records, not a pooled bag of every
    # token in the document. A pooled bag was tried first and measured
    # unreliable: checking a completely unrelated university's ground
    # truth against a document's pooled tokens still "matched" 26-54% of
    # names purely by coincidence, because common Nigerian name components
    # (individual given names, common surnames) are frequent enough that a
    # large token pool contains most of them regardless of whether that
    # specific person appears anywhere in it. Requiring a ground-truth
    # name's full token set to appear TOGETHER in some single extracted
    # record eliminates that false-positive path. An inverted index on
    # each record's rarest token keeps this fast without checking every
    # truth row against every extracted row.
    record_token_sets: list[frozenset] = []
    token_index: dict[str, list[int]] = {}
    for r in extracted:
        t = frozenset(_tokens(r["name"]))
        if not t:
            continue
        idx = len(record_token_sets)
        record_token_sets.append(t)
        for tok in t:
            token_index.setdefault(tok, []).append(idx)

    def matches_some_record(name_tokens: frozenset) -> bool:
        if not name_tokens:
            return False
        pivot = min(name_tokens, key=lambda tok: len(token_index.get(tok, ())))
        return any(name_tokens <= record_token_sets[i] for i in token_index.get(pivot, ()))

    total_truth = 0
    total_found = 0
    for r in truth_rows:
        name_tokens = frozenset(_tokens(r["name"]))
        if not name_tokens:
            continue
        total_truth += 1
        if matches_some_record(name_tokens):
            total_found += 1

    precision = _score_precision(record_token_sets, truth_rows)

    return {
        "extracted_records": len(extracted),
        "truth_records": len(truth_rows),
        "truth_names_checked": total_truth,
        "names_recovered": total_found,
        "recall": (total_found / total_truth) if total_truth else None,
        **precision,
    }


def main():
    if len(sys.argv) not in (3, 4):
        print("Usage: python -m pdf_extractor.validate_corpus <extracted_dir> <ground_truth_dir> [pdf_root_dir]")
        sys.exit(1)

    extracted_dir = Path(sys.argv[1])
    truth_root = Path(sys.argv[2])
    pdf_root = Path(sys.argv[3]) if len(sys.argv) == 4 else None

    results = []
    for csv_path in sorted(extracted_dir.glob("*.csv")):
        m = _OUTPUT_NAME_RE.match(csv_path.name)
        if not m:
            continue
        university, year = m.group(1), m.group(2)
        truth_paths = find_ground_truth_csvs(university, year, truth_root, pdf_root)
        if not truth_paths:
            print(f"{csv_path.name}: no ground-truth match found (skipped)", file=sys.stderr)
            continue
        try:
            stats = score_file(csv_path, truth_paths)
        except Exception as e:
            print(f"{csv_path.name}: ERROR - {e}", file=sys.stderr)
            continue
        stats["university"] = university
        stats["year"] = year
        stats["file"] = csv_path.name
        stats["truth_files"] = [str(p) for p in truth_paths]
        results.append(stats)

    pct = lambda x: f"{x*100:.1f}%" if x is not None else "n/a"
    print(f"{'file':<28} {'extracted':>9} {'truth':>7} {'recall':>7} {'prec':>7} {'strict':>7} "
          f"{'subset':>7} {'none':>7} {'dup':>6}")
    total_checked = total_recovered = 0
    totals = dict.fromkeys(("n", "exact", "superset", "subset", "none", "dup"), 0)
    by_university: dict[str, list] = {}
    for r in results:
        print(f"{r['file']:<28} {r['extracted_records']:>9} {r['truth_records']:>7} {pct(r['recall']):>7} "
              f"{pct(r['precision']):>7} {pct(r['precision_strict']):>7} {r['records_subset']:>7} "
              f"{r['records_none']:>7} {r['records_duplicate']:>6}")
        if r["recall"] is not None:
            total_checked += r["truth_names_checked"]
            total_recovered += r["names_recovered"]
        totals["n"] += r["records_classified"]
        totals["exact"] += r["records_exact"]
        totals["superset"] += r["records_superset"]
        totals["subset"] += r["records_subset"]
        totals["none"] += r["records_none"]
        totals["dup"] += r["records_duplicate"]
        by_university.setdefault(r["university"], []).append(r)

    print()
    print("--- by university ---")
    print(f"{'':<12} {'files':>5} {'recall':>8} {'prec':>8} {'strict':>8}")
    for uni, rs in sorted(by_university.items()):
        checked = sum(r["truth_names_checked"] for r in rs if r["recall"] is not None)
        recovered = sum(r["names_recovered"] for r in rs if r["recall"] is not None)
        n = sum(r["records_classified"] for r in rs)
        good = sum(r["records_exact"] + r["records_superset"] for r in rs)
        exact = sum(r["records_exact"] for r in rs)
        print(f"{uni:<12} {len(rs):>5} {pct(recovered / checked if checked else None):>8} "
              f"{pct(good / n if n else None):>8} {pct(exact / n if n else None):>8}")

    print()
    n = totals["n"]
    print(f"OVERALL recall:    {total_recovered}/{total_checked} ground-truth names recovered "
          f"({pct(total_recovered / total_checked if total_checked else None)})")
    print(f"OVERALL precision: {totals['exact'] + totals['superset']}/{n} extracted records identify a truth person "
          f"({pct((totals['exact'] + totals['superset']) / n if n else None)}); "
          f"exact name {pct(totals['exact'] / n if n else None)}")
    print(f"  of {n} records: exact {totals['exact']}, name polluted (superset) {totals['superset']}, "
          f"name truncated (subset) {totals['subset']}, matches nobody {totals['none']}; "
          f"{totals['dup']} duplicate the same person")


if __name__ == "__main__":
    main()
