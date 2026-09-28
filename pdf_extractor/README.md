# pdf_extractor

PDF (convocation booklet) -> canonical-schema CSV, matching the schema
defined in `../schema/canonical_schema.html` and used to build
`../Canonical Training Data/`.

## Setup

```
python -m venv .venv          # from the Extractor project root
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Requires Tesseract OCR installed separately (`winget install UB-Mannheim.TesseractOCR`).
`pdf_extractor/_tesseract.py` points at the default Windows install path if
`tesseract` isn't on PATH.

## Usage

```
.venv\Scripts\python.exe -m pdf_extractor.cli path\to\booklet.pdf -o output.csv
.venv\Scripts\python.exe -m pdf_extractor.cli path\to\booklet.pdf -o output.csv --pages 0-5
```

For a whole university/year folder of split PDFs (Akowe's real source
material - see `batch.py`'s docstring), or a whole `<root>/<university>/
<year>` tree:

```
.venv\Scripts\python.exe -m pdf_extractor.batch "path\to\LASU\2005" -o lasu_2005.csv
.venv\Scripts\python.exe -m pdf_extractor.batch "path\to\root" --walk -o output_dir\
```

For the Data Entry Specialist review workflow - a CSV with confidence
flags and PDF snapshots instead of a plain CSV to blind-check (see
"Review workflow" below):

```
.venv\Scripts\python.exe -m pdf_extractor.review extract "path\to\LASU\2005" -o lasu_2005_review.csv
.venv\Scripts\python.exe -m pdf_extractor.review export lasu_2005_review.csv -o lasu_2005_clean.csv
.venv\Scripts\python.exe -m pdf_extractor.corrections diff lasu_2005_review.csv lasu_2005_corrected.csv -o corrections_log.csv
.venv\Scripts\python.exe -m pdf_extractor.corrections mine corrections_log.csv
```

## Pipeline

| Module | Job |
|---|---|
| `render.py` | PDF page -> PIL image (PyMuPDF, no poppler dependency) |
| `dewarp.py` | Best-effort perspective correction for photographed pages (safety net - only fires on a genuinely keystoned page quad; flatbed scans are a no-op) |
| `orientation.py` | Detect/correct 0/90/180/270 rotation: Tesseract OSD, cross-checked against its 180-degree opposite by counting real names/terms each orientation OCRs (OSD alone is unreliable on ALL-CAPS lists) |
| `spread.py` | Splits a two-page-spread scan into independent pages (no-op on a normal single page) |
| `ocr.py` | Word-level OCR with bounding boxes (Tesseract sparse-text mode; keeps low-confidence tokens that still look like words) |
| `_tesseract.py` | Locates the Tesseract executable; imported by every module that calls it |
| `columns.py` | Column-band detection + section-aware reading-order reconstruction |
| `classify.py` | Header-line vs. name-row classification |
| `parser.py` | Stateful header tracking (faculty->department->course->degree->grade->year) -> canonical records |
| `pages.py` | Content-page detection + printed-page-number detection |
| `normalize.py` | Python port of `schema/normalize_grades.js` / `normalize_years.js` - same canonical values as the training data |
| `cli.py` | Orchestration: two-pass extraction (OCR all pages, detect running letterhead banners by cross-page frequency, then parse with state carried across pages) |
| `validate.py`, `validate_corpus.py` | Scratch scoring tools: extraction output vs. ground truth (per-record matching), one file or the whole corpus |
| `test_units.py` | Fast regression tests for the text/geometry rules below (`python -m pdf_extractor.test_units`) |
| `batch.py` | Batch driver: walks a university/year folder of split PDFs (or a whole `<root>/<university>/<year>` tree), stitching split files into one document with header state carried across the file boundary |
| `confidence.py` | Per-record HIGH/MEDIUM/LOW confidence scoring for review flagging - never corrects text, only judges it (see "Review workflow") |
| `review.py` | Produces the DES-facing review CSV (flags + PDF snapshot images) and the clean-copy export |
| `corrections.py` | Diffs a DES's corrected review CSV against the original, and mines the diffs for recurring OCR-confusion and vocabulary patterns |

## Review workflow

The tool isn't meant to run unsupervised end to end - the bar is "close enough
that a Data Entry Specialist can fill the gaps fast," not 100% automatic. Three
pieces make that practical:

**Confidence flagging** (`confidence.py`). Every record gets HIGH / MEDIUM /
LOW plus a short `flag_reason`, using only signals available at extraction
time - never ground truth, since a real PDF won't have any. In order of how
certain they are: unambiguous structural defects (a gender-column marker like
`(F)` glued onto the name, a matric number stuck to it, two words fused with no
space at all, a lone single word where a name needs at least two) are LOW
regardless of anything else; low mean OCR word confidence is LOW; a record a
pipeline repair heuristic had to intervene to produce (the wrapped-name merge,
mainly), or whose text doesn't match the training vocabulary of real names, is
MEDIUM. The vocabulary check is one-directional on purpose - it flags "this
doesn't look like a name I recognize" for a human to check, and never
"corrects" toward a recognized one, which on a genuinely new PDF would
silently rewrite an unfamiliar real name into a familiar wrong one.

**Snapshots** (`review.py`). Every MEDIUM/LOW record gets a PNG crop of its
source page - full page width, a couple of line-heights of vertical context -
saved next to the CSV in a `<name>_images/` folder and referenced by a
`review_image` column, so checking a flag means looking at the picture next to
it. This needed threading a record's bounding box, source PDF, page index, and
the exact rotation/dewarp/zoom decisions made for its page through to the
final record (`parser.parse_lines_to_records`'s private `_geom` key) so the
page can be re-rendered on demand rather than keeping every page image around
for the whole corpus.

**The DES-facing loop.** `review.py extract` produces the flagged CSV (13
canonical columns, unchanged, plus `confidence`/`flag_reason`/`review_image`).
A DES corrects flagged rows in place against the snapshot - the same PDF-to-
CSV comparison they already do, just narrowed to the rows worth checking - then
`review.py export` strips the review columns back off for a clean handoff.
Nothing new for them to learn beyond that export step.

**Feeding corrections back** (`corrections.py`). Behind that, `corrections.py
diff` compares the original and corrected review CSVs and logs exactly what
changed, and `corrections.py mine` looks across accumulated logs for two kinds
of recurring pattern: single-character OCR substitutions (candidates for
`parser._fix_ocr_confusions`) and real names the tool didn't recognize
(candidates for the name vocabulary `classify.py` uses). Both are left as
candidate lists for a person to fold in deliberately, not auto-applied -
a wrong confusion rule or a bad vocabulary entry actively corrupts future
output, unlike a flag, which only ever costs a human a second look.

## Validated

Cross-checked against the ground-truth training data built earlier this
project:

- **LASU 2005, page 21** (2-column, 180° rotated, single continuous list):
  93/93 names extracted, in the exact order confirmed against the Excel.
- **LASU 2005, page 20** (2-column, 180° rotated, multiple short
  side-by-side sections per department): header state (faculty,
  department, grade, year) correctly tracked across department and grade
  transitions for the large majority of rows.
- **OAU 2019, page 34** (3-column, 180° rotated): name order matches the
  ground-truth Excel exactly.
- Printed page numbers detected correctly and sequentially (20, 21, 22,
  23) by recognizing the bare 1-4 digit OCR line near a page edge - the
  same "stray digit" artifact noticed as noise during the original manual
  PDF exploration turned out to be exactly the signal needed here.
- Per-page rotation is genuinely inconsistent within a single PDF (OAU
  2019 pages 0-5 measured 0/180/0/180/0/180) - confirms the original
  finding that orientation must be detected per page, never assumed
  constant for a document.

### Broader validation against real Data Training files

Ran the extractor against small page ranges from four universities not
used in earlier development (FUTA, OOU, UNILORIN, UNILAG), comparing
output token-for-token against the corresponding ground-truth canonical
CSV (`pdf_extractor/validate.py` - a scratch comparison tool, not part of
the pipeline). This surfaced and fixed two real bugs, plus quantified an
already-known one:

- **Fixed - matric-number-prefixed names.** FUTA's booklet prints each row
  as `<matric number> <name>` (e.g. `1 APH/18/5328 AJAYI Oluwadunsin
  Oluwafemi`) rather than a bare list number. `NAME_PREFIX_JUNK` alone left
  the whole matric number glued onto the name. Added `MATRIC_PREFIX` to
  `parser.py`, applied *after* the existing numeric-prefix strip (order
  matters - a line can carry both a stray list-number digit and a matric
  number, and the matric pattern's anchor can't reach past a leading
  digit). This didn't move the recall numbers below, because the
  validation script's loose token-subset matching doesn't care about
  extra noise tokens attached to a name - but it matters a great deal for
  the actual deliverable: the `name` field is now clean instead of
  containing an unrelated ID string.
- **Fixed - Tesseract OSD 90°/270° confusion.** On a UNILORIN 2016
  page, OSD confidently reported a 90° correction that produced pure
  garbage ("nmynypiAueay NMOVIHI"); the actual correct rotation, 270°,
  reads as clean English names ("NWAKAJI James Clifford"). This pair is
  apparently harder for OSD to tell apart than 0°/180° (both "look
  sideways" with similar stroke structure to its heuristic, unlike
  upright-vs-upside-down text). `orientation.py` now runs a cheap
  confidence-based disambiguation specifically for the 90/270 case: OCR
  both candidates on a shrunk copy and swap to the 180-opposite rotation
  if its mean word confidence clearly beats OSD's pick. Verified the fix
  resolves this exact page correctly; confirmed no change to the LASU/OAU
  samples (both 0°/180° cases, outside the branch this touches).
- **Quantified, then fixed - multi-column garbling.** UNILAG's dense
  postgraduate-diploma page recovered only ~20% of ground-truth names, not
  from missing content but from severe row-merging (one extracted "record"
  containing 5-6 different people's names, and separately, a programme
  header fused into a name field). Root-caused and fixed in `columns.py` -
  see item 6 below. Re-running the same page after the fix: **73.8%**
  recall (343/465 names across 6 pages), with the OAU sample's same page
  range recovering 34% more genuine records than before (715 -> 956, zero
  duplicates introduced) - concrete, measured confirmation on real
  production data, not just the synthetic tests used while building the
  fix.
- Also confirmed real production data actually contains UNILORIN's
  two-page spreads (`spread.py`'s target case) and photographed-looking
  pages that trigger `dewarp.py` - both fired on this file's pages, not
  just the synthetic test cases used when building them.

### LASU and FUTA deep-dive (the two weakest universities)

After the first corpus-wide scoring, LASU (56.6%) and FUTA (36.8%) were far
behind the rest. Tracing real rows back through the pipeline found that
neither problem was what it first looked like, and that most were shared
bugs whose damage happened to concentrate there. Every item below was found
by diffing real output against ground truth, and each has a case in
`test_units.py` or a documented measurement.

**Scoring methodology.** Matching each ground-truth name against a *pooled
bag of all tokens* in a document inflated every score (an unrelated
university "matched" 26-54% by coincidence, since Nigerian name components
are common). `validate_corpus.py` now requires a name's full token set to
appear together in one extracted record (unrelated-university control:
0.3-0.9%). All numbers in this README from that point on use that method.

**LASU - pages OCR'd upside-down (the dominant cause).** LASU 2009/2020
lists are ALL CAPS, which leaves Tesseract's orientation detection (OSD)
almost nothing to judge by - it leans on ascender/descender statistics.
It reported 0 degrees with near-zero confidence (0.9-2.1) for roughly half
the pages that were actually upside down, and each of those OCR'd as
gibberish (`NOISIAIG` for `DIVISION`), so ~60% of a year's records were
never extracted - lost, not garbled. The fix went through three versions,
each teaching something:
1. Compare OSD's pick with its 180-degree opposite by Tesseract's mean word
   confidence. Fixed LASU 2020 (384 -> 1,013 records on 8 pages) but was
   **wrong**: on a LASU 2013 page where OSD was correct, the upside-down
   reading scored *higher* confidence (39 vs 27) and the check overturned
   the right answer. Gibberish can OCR with decent confidence.
2. Compare by **vocabulary hits** (words matching the ~21k names/terms mined
   from the training corpus, `classify.vocab_score`) - it measures whether
   text is language, which is the actual question. Correct orientation:
   ~170-410 hits; wrong: 6-27.
3. The check image must be readable: at half scale the very dense LASU 2012
   pages (~250 names/page) gave no signal in either orientation (15 vs 6,
   0 vs 0); at 0.75 the same pages read 6 vs 179, 27 vs 218. The second OCR
   is skipped when OSD's pick already reads 80+ known words.
Final logic is 11/11 on the known-tricky pages, and did not overturn
correct answers on a 12-page sample across six other universities (the two
overturns, UNILORIN 2019 p5/p6, were verified genuine: 9-10 vs 307-380
full-page hits). The same check also covers 90/270 (UNILORIN 2016).

**FUTA - a chain of smaller problems** (FUTA 2021 benchmark on 8 real pages
with ground truth: 45.5% -> 89.7%):
- Tesseract's automatic layout mode returned bad word boxes on the dense,
  tightly-kerned scan (a surname "spanning" the whole name's width) ->
  sparse-text mode (PSM 11) is now the default. Measured on 9 datasets across
  all 8 universities it is never meaningfully worse; zoom 3.0 gave no
  further gain once the other fixes were in, so zoom stays 2.0.
- Words at OCR confidence 0.0 (a perfectly-read surname `FASHOLA` beside
  `Olamilekan` at 93) were discarded by a blanket confidence cutoff. Tokens
  below the cutoff are now kept when they still look like words (`ocr._wordlike`) -
  including `ADEDE4JI` (one stray digit, confidence 16).
- A 1-pixel bounding-box overlap between a surname and its given name split
  the surname off its row: the no-overlap rule for line grouping now
  tolerates up to 30% overlap of the narrower word (same-column words from
  different rows overlap ~98%, so the rule still separates those).
- Name-text repairs in `parser.clean_name_text`: trailing matric numbers
  (`... 14 PMT/14/9181`), trailing list numbers, a stray OCR'd digit in the
  faculty code (`FWT1/15/1797`), fused words (`OYEKANMILekan`,
  `AlabaGbemisola`), `I`/`l`/`!`/`|` glyph confusion (`AJAY!` -> `AJAYI`,
  `lyanu` -> `Iyanu`, `ADEBIY|Adewale`), and stray digits/dots/bangs inside a
  long surname (`OLABAN4JI`, `ADEDE.JI`).

**FUTA - postgraduate table sections.** FUTA 2021's tail (printed pages
116-139, ~900 ground-truth rows) is a different layout: a table of
`No. | Matric | SURNAME | Given names | Degree description`. Column detection
correctly found surname and given names as separate columns, but each was then
read as its own list, so every person became two records (`OYEGOKE` /
`Olufunso Olawale`) and those pages scored **0%**. `columns._merge_split_name_bands`
now merges two adjacent bands when the left one holds only ALL-CAPS surnames and
the right one only given names, row for row. Row alignment alone can't decide
this - two genuine side-by-side name columns are also row-aligned (they share a
line grid), and merging those would fuse two different people - so the rule
keys on *content*: independent columns each hold complete names, table columns
hold halves of one. Result on 10 such pages: **0% -> 74.5%** (272/365 rows). A
survey of 26 pages across all 8 universities (including LASU's row-aligned
two-column lists) found the rule fires on exactly one page - the intended one.
(FUTA 2021 also contains ~25 Ph.D pages in a per-graduate block format
[`Degree:` / `Title of Thesis:` / `Date of Award:`] that are **not in the
ground truth at all**, so they can neither help nor be scored.)

**Dense pages - adaptive zoom.** After the orientation fix, LASU 2012 still
had a partial shortfall concentrated on its densest pages (~250 names each,
word boxes only ~11px tall at zoom 2.0). Recall on 5 such pages was 30.8% at
zoom 2.0, 49.9% at 3.0, 54.3% at 4.0 - while typical pages (14-18px words)
barely changed with zoom, so a blanket higher zoom would just cost ~40% more
time everywhere. `cli._larger_zoom_if_small_text` re-renders a page at a zoom
that brings its words to ~18px only when the page's own text is small (the
smaller of the median word height and a scaled 25th percentile is under 13px -
the percentile catches pages mixing small print with larger text). Two-page
spreads are skipped, and the already-decided rotation is re-applied rather than
re-running orientation detection.

**Ground-truth errors found.** Some remaining FUTA "misses" are typos in the
canonical data itself, where the tool's spelling is right: `OIawale`,
`OIamide`, `OIoruntobi`, `AIex-Ander` (capital I for `Ol`/`Al`) - the
ground truth appears to have inherited OCR errors. These cap measurable
accuracy below 100% regardless of the tool.

**Source coverage gaps (not tool errors).** FUTA 2024's PDFs total 45 pages
against ground truth spanning 91 printed pages (~83 rows each), so that year
has a hard recall ceiling near 50-55%.

**Robustness.** A single `cv2.error: Unknown C++ exception` on one page's
first processing step aborted an entire year's extraction (hours of work).
`cli.extract_pdf_with_state` now retries a failed page once and otherwise
skips it with a loud warning listing the missing pages, instead of dying.
Also: `orientation.py` silently depended on `ocr.py` having been imported
first to locate the Tesseract executable (tripped over twice); both now
import a shared `_tesseract.py`. And `batch.py` file ordering now
understands `(195 - 202)` and `(from page 182)`, not only `PGS n`.

### Final LASU / FUTA results, and what was left (LASU 2024)

After the fixes above, LASU stood at **84.7%** (140,732 of 166,244 ground-truth
names) and FUTA at **63.5%** (7,984 of 12,579; FUTA 2024 is capped near 50% by the
source-coverage gap). LASU 2024 was the laggard at 75.0%. Digging into it found
two more real defects, both of which apply to every university:

**Dewarp false positives on flatbed scans (75.0% -> 76.5%).** `dewarp.py` fired
on ~19% of pages in a whole-corpus run (1,007 of 5,381) - all flatbed scans. The
"page quad" it found was an *inner rectangle* (a page frame or content border),
with opposite sides within 0.05-2.3% of each other, merely offset from the image
corners and tilted 1-2 degrees. Warping it distorted the geometry enough to split
`UGBALA IFUNAYA` / `SANDRA` into separate records. It now also requires genuine
keystone (opposite sides differing by >4%; a synthetic 15-degree photo measures
~8%). Test: `test_dewarp_only_fires_on_real_keystone`.

**Number-anchored columns and wrapped names (76.5% -> 88.4%).** LASU 2024 prints
three tightly packed columns of `N. SURNAME Given names`, and ~25% of names are
long enough to wrap onto an indented second line. Two failures:
- Gap-based band detection cut *after the next column's numbers* (the
  name-to-next-number gap is as small as the number-to-name gap), so `16.` ended
  up on the tail of column 1's line and the name it belongs to was orphaned.
  `columns._number_column_bands` now anchors columns on the list numbers, which
  form a right-aligned vertical line per column.
- The wrapped tail became a separate one-word "record" and the real record lost
  its last given name (`OYEDOKUN ADURAGBEMI` / `EMMANUEL`).
  `columns._merge_wrapped_names` rejoins them on numbered pages: a lone word below
  a name is always a tail (a record is at least surname + given name); a two-word
  line only when the line above visibly carries its number and fills the column.
  It runs over the final line stream because a grade banner in a neighbouring
  column can flush a section between a name and its tail. It cannot rely on the
  numbers: OCR drops the small single-digit ones (`3.`-`9.`) altogether.
  Tests: `test_number_column_bands`, `test_wrapped_name_merge`.

### Full-corpus result (all 8 universities, 52 year-folders): 86.8%

Scored with `validate_corpus.py` against the 535,288-row canonical truth:
**464,748 names recovered (86.8%)**, up from 72.3% before the fixes in this
section. Outputs are in `Extracted Output/_final/`, the per-file scores in
`Extracted Output/_final/score.txt`.

| University | Recall | | University | Recall |
|---|---|---|---|---|
| OAU | 91.4% | | UI | 84.2% |
| LASU | 89.2% | | UNILAG | 86.1% |
| UNILORIN | 88.8% | | COVENANT | 76.8% |
| OOU | 87.8% | | FUTA | 65.7% |

About 10,400 truth rows have no source PDF page at all, so no extractor could
recover them: COVENANT 2023 (the PDF is the 2022 convocation, 1,380 rows), FUTA
2024 (~3,400 rows; PDFs cover ~half the pages), UI 2015 (~2,300; pages 220-259
missing) and UI 2016 (~3,300; PDFs start at page 207, truth at 152). Excluding
them the corpus is ~88.5%.

Further defects found and fixed on the way to this number:
- **Tesseract's own thresholding failing on some scans** (UI 2004, wide grey
  scanner-bed border): a readable page OCR'd as 26 noise fragments. `ocr.py` now
  re-reads a garbage-looking page from an Otsu-binarised copy and keeps it only
  if it finds clearly more known names/terms. UI 2004 66.6% -> 76.1%, UI 2003
  78.7% -> 87.9%.
- **Gender column** (COVENANT postgraduate lists) read as `Male`/`Female`
  "records" or glued onto names; stripped in `parser.py`
  (COVENANT 2024: 1,766 -> 1,669 rows, recall unchanged).
- **Parallelism and memory:** six concurrent jobs opening a 112 MB and a 46 MB PDF
  made every page fail with an OpenCV allocation error (LASU 2016, COVENANT
  2024 came out empty). They succeed run alone; run big folders with <= 4 workers.

**Recall is not the whole picture.** The scorer measures how much of the truth
was found, not how much junk was added. Extracted counts well above the truth
(UI 2021: 21,810 vs 13,670; COVENANT 2024 still ~60 fragment rows) show
non-name rows getting through. Next step: report precision alongside recall.

### Precision, duplicates and missing pages

**Precision.** `validate_corpus.py` now reports precision beside recall. Each
extracted record is classified against the truth: *exact* (its tokens are a truth
name), *superset* (a truth name plus extra text - a polluted name), *subset* (a
strict part of a truth name - a lost given name), or *none* (matches nobody:
garbled OCR, a header, or someone the truth lacks). `prec` = exact + superset;
`strict` = exact only; a duplicate is more records resolving to a name than the
truth has rows for it. Corpus at the last full scoring: recall 86.8%, precision
86.6%, exact 85.2%; of 534,970 records 455,588 exact, 7,844 polluted, 23,471
truncated, 48,067 matching nobody, 1,592 duplicates.

**Overlapping / repeated source files** (`batch.drop_overlapping_files`,
`drop_cross_file_duplicates`). UI 2021's `PGS 319 - 321` file is a ~4,000-record
copy of `PGS 286 - 352` and `PGS 130 - 200` overlaps `PGS 62 - 150`, so the same
people were extracted twice (4,147 duplicate rows in one year). Exact-record
matching can't catch it - a re-scan lacks the earlier headers, so course/year
differ on half the rows. Instead, files are compared by shared names: over all 88
file pairs in the corpus, parts of one booklet share 0-4% of names, the two real
overlaps 30% and 83%. At >=15% the later file loses the shared names; at >=60% it
is dropped as a copy. Cost: UI 2021 recall -1.7 points (the copy occasionally read
a name the original missed). Records inside one file are never touched, since two
graduates can share a name.

**Orientation, all four rotations.** The vocabulary check compared OSD's pick only
with its 180-degree opposite. UNILORIN 2017 has colourful faculty pages printed
sideways where OSD said 0; the check now tries 90 and 270 too whenever the pick
reads fewer than 80 known words (UNILORIN 2017 recall 82.9 -> 84.4%).

**OCR rescue, by known-name density** (`ocr.ocr_words`). Binarising a page first
rescues scans where Tesseract's own thresholding fails, but only the *share of
words that are known names* detects that reliably: healthy pages measure
0.59-0.94, UI 2004's failing book-scan pages 0.17 (171 confident-looking words, 29
known). It is a rescue, not a default - on healthy pages binarising is no better.

**Non-name lines** (`parser.is_non_name`): `Supervisor:`, `Thesis:`, `nil`, a
professor's name, anything ending in a colon.

**What is still unrecovered** is listed page by page in `Missing Pages.md`
(regenerate with `python -m pdf_extractor.missing_pages`): ~11,100 truth rows have
no source PDF (UI 2016 pp. 152-206, FUTA 2024, UI 2015 pp. 220-259, COVENANT 2023,
UI 2019 pp. 267-271); the rest are individual pages a PDF does cover.

### Final full-corpus result: 88.8% recall, 87.0% precision

The user supplied the page images for the five gaps listed above (`More Missing
Files.zip`) except COVENANT 2023, for which the two PDFs on file are the 2022
convocation misdated (verified: their extraction matches 90.8% of the 2022
truth, 0% of 2023's) - those were set aside in `Data Training/Set aside - wrong
year .../` and the new 2023 page images stand in for them. The gap pages were
assembled into PDFs by contiguous run (`UI 2016 (PGS 152 - 206) (from JPG
scans).pdf`, etc.) and dropped into the normal `Data Training` folders, then the
whole corpus was re-run on the current pipeline (all fixes in this README) and
rescored:

- **Recall: 88.8%** (475,252 / 535,288), up from 86.8%.
- **Precision: 87.0%**; exact name 85.5%.
- UI 2015, UI 2016, UI 2019 and COVENANT 2023 are now fully covered - no
  missing pages left in any of them.
- FUTA 2024 and UI 2004 improved (52.5% -> 85.5%; 76.1% -> 82.0%) but still
  have a handful of pages the PDF covers yet extraction still misses; these
  are tool gaps now, not missing files (see `Missing Pages.md`).

Outputs are in `Extracted Output/_final_v3/`, per-file scores in
`score.txt`, remaining gaps in `missing_pages.txt` (also mirrored to
`Missing Pages.md` at the project root).

## Known limitations (next things to tune, not fundamental blockers)

1. **Header/name-row classification for lines with no explicit keyword
   match (no FACULTY/DEPARTMENT/grade word/degree pattern/year) now uses
   vocabulary matching, not text shape.** `build_vocab.py` mines two word
   lists straight out of the 535k-row canonical training corpus -
   institutional terms (faculty/department/course/degree/grade text) and
   personal-name words - keeping only words that land >=95% on one side
   or the other, and `classify.py`'s `vocab_score()` counts a line's words
   against both lists. This replaced the old comma/case/spacing heuristic,
   which misread an all-caps name with no comma (e.g. "LUMOUS ENIOLA
   IYABODE MUYIBAT") as a header. Remaining known gaps:
   - A line with zero words in either vocabulary (OCR garbage, or a name/
     course not seen anywhere in the training corpus) still falls back to
     the old shape heuristic as a last resort.
   - Re-run `build_vocab.py` if `Canonical Training Data/akowe_canonical_master.csv`
     is regenerated, since `vocab.json` is a point-in-time snapshot of it.
   Mitigation path: confidence scoring + a human review queue (as
   originally scoped) rather than chasing every text-shape edge case.

2. ~~Running-header/letterhead detection is frequency-only, not
   position-aware. `cli.py`'s `_detect_banners` correctly drops a
   letterhead that repeats near-identically across many pages, but can't
   yet tell a *true* fixed banner (same text, same position every page)
   from a legitimate section header that happens to recur often.~~
   **Fixed.** `_detect_banners` now requires two more signals beyond raw
   frequency before calling a recurring line a banner:
   - **Positional consistency** - the line's vertical position (as a
     fraction of page height) must cluster tightly across its
     occurrences. A genuine letterhead sits in a fixed template spot every
     time; a content header that happens to repeat (e.g. a department
     name re-printed atop each of its own continuation pages) tends to
     drift with how much other content precedes it on a given page.
   - **The institutional vocabulary** (same `vocab.json`/`vocab_score()`
     from item 1) - a recurring, even fixed-position line is still kept
     if `classify_line()` recognizes it as a real header field (faculty/
     department/degree/grade/year/programme/affiliated_institution).
     This catches a case position alone can't: a legitimate header can
     also sit at a near-fixed y-position across pages (verified with a
     synthetic "FACULTY OF ENGINEERING" banner-shaped test - position
     consistency alone would have wrongly stripped it; the vocabulary
     guard is what saves it). A genuine letterhead essentially never
     matches one of these field patterns itself - it's exactly the class
     of line the *original* "OBAFEMI AWOLOWO UNIVERSITY, ILE-IFE" example
     that motivated this whole check falls into, which is why frequency
     and position are still the primary signals, not a replacement for
     them.

   Residual gap found while verifying this against OAU 2019 pages 30-40:
   on that page range, `reconstruct_reading_order` (in `columns.py`)
   splits the same letterhead inconsistently across pages - `'OBAFEMI'` +
   `'AWOLOWO UNIVERSITY. ILE-IFE'` as two separate lines on one page,
   `'OBAFEMIAWOLOWO'` fused as a single word on another - so the exact
   text key never recurs often enough to clear the frequency threshold at
   all, regardless of position or vocabulary. This is a line-segmentation
   issue in the column-reconstruction step, not a banner-classification
   one - the same underlying cause as the multi-column interleaving noted
   elsewhere - and would have gone undetected under the old frequency-only
   logic too, so it isn't a regression, just a limitation this pass
   surfaced rather than fixed.

3. ~~No perspective/dewarp correction. `orientation.py` only handles the
   four right-angle rotations. Photographed (non-flatbed) pages, which the
   manual exploration found in at least one university's files, need a
   dewarp step this doesn't have yet.~~ **Fixed - see `dewarp.py`.**
   `correct_perspective()` runs before orientation detection (a
   photograph's keystone/skew distorts the edges Tesseract's OSD uses to
   judge rotation, so straightening first gives it a cleaner input) and
   looks for a large, confidently page-shaped quadrilateral that's also
   meaningfully non-rectangular, then warps it flat via
   `cv2.getPerspectiveTransform`.

   This is deliberately a safety net, not a primary pipeline stage, per
   the standing plan to source flatbed scans wherever possible rather than
   lean on this to compensate for photographs. That shaped the design
   choice to err heavily toward doing nothing: a flatbed scan's content
   fills the frame edge to edge with no page-boundary contour to find, so
   `_find_page_quad` finding nothing (or finding something too
   close to the frame's own rectangle already - see `_needs_correction`)
   is the expected, common outcome, not a failure case. Verified against
   both a synthetic skewed-photo case (correctly detected and flattened)
   and the LASU/OAU flatbed samples (correctly left untouched, with
   identical record counts to before this change) - mis-firing on a good
   flatbed scan by mistaking an internal contour (a table border, a dense
   text block) for the page edge would be worse than never correcting
   anything at all.

4. ~~Two-page-spread scans (one image containing two physical book pages,
   seen in UNILORIN's booklets) aren't split into separate pages before
   column detection.~~ **Fixed - see `spread.py`.** `split_spread()` runs
   right after orientation correction: an already-upright image wider than
   `1.15x` its height is treated as a candidate spread, and gets cut at
   whichever vertical line near the horizontal center has the least ink
   (the blank gutter/margin between two physical pages, rather than an
   assumed dead-center 50/50 cut). Each resulting half is OCR'd and read
   independently from there on, with a page-key suffix (`5a`/`5b`) so
   output stays traceable to which half a record came from.

   This must run *after* orientation correction, not before: a spread is
   landscape-shaped in any of its 4 possible raw scan rotations - the same
   silhouette a single page rotated 90 degrees has before correction - so
   checking aspect ratio any earlier would misfire on an ordinary sideways
   single page and wrongly cut it in half. Verified with synthetic
   portrait-single-page (correctly left untouched) and landscape-spread
   (correctly split into two proportioned halves) images, and against the
   LASU/OAU flatbed samples (unchanged record counts, no spurious splits -
   neither sample happens to contain an actual spread, so this confirms
   the no-op path on real data rather than the split path itself).

5. ~~`programme` (mode of study) and `affiliated_institution` (affiliate
   college) fields have a slot in `HeaderState` but no detection pattern
   in `classify.py` yet - not wired up.~~ **Fixed.** `PROGRAMME_PATTERN`
   (Full Time/Part Time/Sandwich/Distance Learning/Supplementary/Addendum)
   and `AFFILIATED_INSTITUTION_PATTERN` (Affiliate*/Seminary/Theological/
   Training Centre-School/College of Education-Theology) are wired into
   `classify_line`, and `HeaderState.apply_header` now has a `programme`
   case (it was previously missing entirely, so even a correctly
   classified programme header would have been silently dropped).
   `affiliated_institution` is checked before `FACULTY_PATTERN` so
   "COLLEGE OF EDUCATION/THEOLOGY" routes to the affiliate field instead
   of being read as a faculty of the main institution. Known gap: an
   affiliate name with no distinguishing keyword at all (e.g. "PAN
   AFRICAN UNIVERSITY", "IMAM MALIK COLLEGE IBADAN") still isn't caught -
   same class of residual gap as the degree/header vocab fallback above.

6. ~~Batch orchestration is one-PDF-at-a-time via the CLI; there's no
   driver yet for walking a whole university/year folder of split PDF
   files (as most of Akowe's real source files are) and merging them with
   state carried across file boundaries.~~ **Fixed - see `batch.py`.**
   `process_folder()` orders a folder's split files by the `PGS <start> -
   <end>` marker in their filenames (a file with no marker sorts first -
   the main/undifferentiated list in every folder sampled) and threads
   `HeaderState` across the file boundary via the new
   `cli.extract_pdf_with_state()`. `--walk` discovers every
   `<university>/<year>/` folder under a root and processes each - except
   a leaf folder that isn't named a bare year (e.g. COVENANT's `Degrees`
   folder, which holds four *independent* per-year documents rather than
   split fragments of one), where each file is extracted on its own
   instead, with no state carried between them.

   Every batch-produced CSV adds a `source_file` column (matching the
   convention already used in `Canonical Training Data/
   akowe_canonical_master.csv`) beyond the 13 canonical fields. This isn't
   just courtesy metadata: `page`-number detection is per-file, so when it
   fails on a given page the fallback label ("idx1", "idx2", ...) is only
   unique *within* that file - two different physical PDFs in the same
   folder can each produce a record labeled page "idx1", and `source_file`
   is what makes those distinguishable again once records from multiple
   files land in one CSV.

   Ordering split files by filename was a deliberate choice over
   OCR-detecting each file's actual starting page number, which was tried
   first: it measured *less* reliable in testing (a stray digit nine pages
   into one file was read as the page number, implying the file started
   around page -5 when it actually starts near page 196), while also
   costing an extra OCR pass per file. The filename convention matched the
   true order on every real folder sampled.

7. ~~Multi-column garbling on dense layouts (see the broader-validation
   section above) - a single extracted record turning into 5-6 different
   people's names fused together.~~ **Fixed - see `columns.py`.** Three
   compounding bugs, each found and root-caused against real data:
   - **Row-grouping snowball merge.** `_group_words_into_lines` (replacing
     the old `words_to_lines`/`_rough_lines_ignoring_columns` duplicate
     logic) now requires horizontal non-overlap, not just vertical
     proximity, to put two words on the same line - real printed words
     never overlap each other, which a dense table's tight leading can
     make vertical-overlap-only grouping get wrong (row height ~18px,
     row-to-row step only ~13px on a real UNILAG page - absorbing one
     overlap grows the merged box, making the next row overlap too, a
     chain that glommed 5+ real rows into one blob). Compared against
     the line's most-recently-added member (not a fixed first-word
     anchor) - an anchor is fragile against a single unrelated outlier
     word starting a line first (a detached OCR punctuation fragment did
     exactly this on a real LASU page, permanently skewing that line's
     reference point and wrongly splitting a genuine name in two).
   - **Column-band detection assumed a page-wide empty gutter.** That
     assumption (verified on LASU/OAU) breaks when column-content width
     varies row to row enough that the union of all rows' extents bridges
     what is, per row, a real gap (found on a real UNILAG page: 3 name-
     columns, literally zero uncovered pixel-columns anywhere on the page
     when checking actual word spans, yet every row still respects clean
     boundaries). `find_column_bands` now always runs and merges a second
     signal - `_recurring_internal_gap_boundaries`, which looks at each
     row's *own* internal gaps rather than the global union - instead of
     only falling back to it when the first method found exactly one
     band. That function also now collects every gap in a row that clears
     the threshold, not just the largest, so a 3-column row's two real
     gutters can both be recognized instead of silently keeping only one.
   - **Header/body classification assumed one column per row-band.**
     Fusing a row-band's words across columns before classifying could
     either swallow real names into a false "header" (a programme label
     in one column at the same height as ordinary names in another) or
     bury a genuine column-local header inside what then reads as
     nonsense once fused with unrelated neighbours - both found on the
     same UNILAG page, since each of its 3 parallel name-lists carries its
     own programme header at a different height. `reconstruct_reading_
     order` now splits a row-band into per-column slices first
     (`_split_row_into_column_slices`) and classifies each independently -
     but only where the slices are separated by a gutter-sized gap, not
     wherever a column-band boundary happens to fall, since that boundary
     can otherwise cut a single continuous banner phrase (like "FACULTY OF
     X") into fragments that don't classify as a header on their own.

   Verified via `pdf_extractor/validate.py` against real ground truth:
   UNILAG recall 10.6% -> 73.8%; OAU's same page range recovered 34% more
   genuine records (715 -> 956) with zero duplicates. Confirmed no
   regression on LASU (92 records, matching every prior run this session)
   and OAU (name order still correct). One iteration surfaced a real
   regression before landing here - generalizing to "every qualifying gap
   per row" initially let a stray list-number's trailing punctuation
   (a detached micro-word recurring at a consistent x-position) masquerade
   as a third column; fixed by excluding punctuation-only tokens from all
   gap/boundary detection (`_is_low_signal_word`), not just from the final
   text.
