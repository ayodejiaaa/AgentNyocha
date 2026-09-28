"""One round of the A/B/C page-number sampling: extract a real document per
university, resolve page numbers with the current pipeline, and check the
result against ground truth by matching names. Designed to survive a
per-document surprise (a new design pages.py chokes on, a bad PDF) without
losing the rest of the round - the instruction is "note it and keep going,"
not "stop and fix."

    python -m pdf_extractor.scratch_page_sample A
"""
import sys
import traceback
from collections import Counter, defaultdict
from pathlib import Path

from pdf_extractor import cli
from pdf_extractor.validate import _tokens
from pdf_extractor.validate_corpus import find_ground_truth_csvs, load_rows

R = Path("Data Training/PDF Files of the data on Akowe Verify/PDF Files of the data on Akowe Verify")
TRUTH = Path("Canonical Training Data")

# (university, year, filename, start_page, n_pages) - filename is exact, not
# a glob, so re-running is reproducible; start_page offsets into a document
# reused across rounds (marked) so it isn't literally the same pages twice.
ROUNDS = {
    "A": [
        ("LASU", "2005", "LASU 2005 (PGS 20 - 127)_20240408_0001.pdf", 0, 20),
        ("UI", "2004", "UI 2004 (PGS 105 -199)_20240119_0001.pdf", 0, 20),
        ("UNILORIN", "2016", "UNILORIN 2016_20240124_0001.pdf", 0, 20),
        ("OOU", "2020", "OOU 2020 (PGS 90 - 189)_20240513_0001.pdf", 0, 20),
        ("UNILAG", "2019", "UNILAG 2019 (Postgraduate)_20240930_0001.pdf", 0, 20),
        ("FUTA", "2024", "FUTA 2024_20250219_0001.pdf", 0, 20),
        ("COVENANT", "2021", "COVENANT UNIVERSITY 2021_20240126_0001.pdf", 0, 20),
        ("OAU", "2023", "OAU 2023 (from JPG scans).pdf", 0, 20),
    ],
    "B": [
        ("LASU", "2012", "LASU 2012 (PGS 24 - 123)_20240402_0001.pdf", 0, 20),
        ("UI", "2014", "UI 2014 (PGS 298 - 367)_20240117_0001.pdf", 0, 20),
        ("UNILORIN", "2018", "UNILORIN 2018 (from JPG scans).pdf", 0, 20),
        ("OOU", "2024", "OOU 2024 (Pgs 80 - 179)_20241115_0001.pdf", 0, 20),
        ("UNILAG", "2023", "UNILAG 2023_20240207_0001.pdf", 0, 20),
        ("FUTA", "2024", "FUTA 2024_20250219_0001.pdf", 40, 20),  # repeat: different pages than round A
        ("COVENANT", "2022", "COVENANT UNIVERSITY 2022 (from JPG scans).pdf", 0, 20),
        ("OAU", "2024", "OAU 2024 (from JPG scans).pdf", 0, 20),
    ],
    "C": [
        ("LASU", "2019", "LASU 2019 (PGS 45 - 148)_20240725_0001.pdf", 0, 20),
        ("UI", "2021", "UI 2021 (PGS 202 - 285)_20231226_0001.pdf", 0, 20),
        ("UNILORIN", "2021", "UNILORIN 2021 _20240708_0001.pdf", 0, 20),
        ("OOU", "2020", "OOU 2020 (PGS 90 - 189)_20240513_0001.pdf", 40, 20),  # repeat: different pages than round A
        ("UNILAG", "2025", "UNILAG 2025 (from JPG scans).pdf", 0, 20),
        ("FUTA", "2024", "FUTA 2024_20250219_0001.pdf", 80, 20),  # repeat: different pages than A and B
        ("COVENANT", "2024", "COVENANT UNIVERSITY 2024_20241028_0001.pdf", 0, 20),
        ("OAU", "2023", "OAU 2023 (from JPG scans).pdf", 40, 20),  # repeat: different pages than round A
    ],
}


def truth_page_lookup(university, year):
    lookup = {}
    for tp in find_ground_truth_csvs(university, year, TRUTH, R):
        for row in load_rows(tp):
            toks = frozenset(_tokens(row["name"]))
            if toks and row.get("page", "").strip():
                lookup.setdefault(toks, []).append(row["page"].strip())
    idx = {}
    for toks in lookup:
        for tok in toks:
            idx.setdefault(tok, []).append(toks)
    return lookup, idx


def find_truth_page(name, lookup, idx):
    toks = frozenset(_tokens(name))
    if not toks:
        return None
    cands = set()
    for tok in toks:
        cands.update(idx.get(tok, ()))
    matches = [c for c in cands if c <= toks or toks <= c]
    if len(matches) != 1:
        return None
    pages = lookup[matches[0]]
    return pages[0] if len(set(pages)) == 1 else None


def run_round(label: str):
    cases = ROUNDS[label]
    overall = Counter()
    by_uni = defaultdict(Counter)
    notes = []

    for uni, year, filename, start, n_pages in cases:
        pdf = R / uni / year / filename
        print(f"--- Sample {label}: {uni} {year} ({filename}, pages {start}-{start+n_pages}) ---", file=sys.stderr)
        if not pdf.exists():
            notes.append(f"{uni} {year}: file not found - {filename}")
            print(f"  SKIPPED: file not found", file=sys.stderr)
            continue
        try:
            lookup, idx = truth_page_lookup(uni, year)
            if not lookup:
                notes.append(f"{uni} {year}: no ground truth found for this year")
                print(f"  SKIPPED: no ground truth", file=sys.stderr)
                continue
            recs, _ = cli.extract_pdf_with_state(
                str(pdf), list(range(start, start + n_pages)), zoom=2.0, verbose=True
            )
        except Exception as e:  # noqa: BLE001 - note it and keep going, per instructions
            notes.append(f"{uni} {year}: EXTRACTION FAILED - {type(e).__name__}: {e}")
            print(f"  FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            continue

        checked = 0
        status_counts = Counter()
        for r in recs:
            status = r["_geom"].get("page_number_status", "?")
            status_counts[status] += 1
            truth_page = find_truth_page(r["name"], lookup, idx)
            if truth_page is None:
                continue
            checked += 1
            match = str(r["page"]) == str(truth_page)
            overall[(status, match)] += 1
            by_uni[uni][(status, match)] += 1
        print(f"  {len(recs)} records, {checked} matched to a single truth person; "
              f"status counts: {dict(status_counts)}", file=sys.stderr)

    print()
    print(f"===== SAMPLE {label} =====")
    print(f"{'status':<14} {'n':>6} {'matches truth':>14} {'accuracy':>10}")
    for status in ("detected", "interpolated", "uncertain"):
        n_true, n_false = overall[(status, True)], overall[(status, False)]
        n = n_true + n_false
        if n:
            print(f"{status:<14} {n:>6} {n_true:>14} {100*n_true/n:>9.1f}%")
    print()
    print("by university:")
    for uni, counts in by_uni.items():
        line = [f"  {uni:<10}"]
        for status in ("detected", "interpolated", "uncertain"):
            n_true, n_false = counts[(status, True)], counts[(status, False)]
            n = n_true + n_false
            if n:
                line.append(f"{status}={n}({100*n_true/n:.0f}%)")
        print(" ".join(line))
    if notes:
        print()
        print("NOTES / CHALLENGES:")
        for n in notes:
            print(f"  - {n}")


if __name__ == "__main__":
    run_round(sys.argv[1])
