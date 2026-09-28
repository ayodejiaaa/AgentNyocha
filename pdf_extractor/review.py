"""Turns extraction output into a review package for Data Entry
Specialists, instead of a plain CSV they have to blind-check line by line:

    python -m pdf_extractor.review extract <university/year folder> -o out.csv
    python -m pdf_extractor.review export <corrected_out.csv> -o clean.csv
    python -m pdf_extractor.review preview <out.csv> -o out.html

`extract` writes a CSV with three extra columns beyond the canonical
schema - confidence, flag_reason, review_image - and, for every row that
isn't high confidence, a cropped PNG of the source PDF page around that
row (in a sibling "<name>_images/" folder), so checking a flagged row
means looking at the picture right next to it instead of hunting through
the source scan. It never touches the canonical columns' values - see
confidence.py for why.

A DES corrects flagged rows directly in this file (comparing against the
image, same as they already compare PDF to CSV today), then `export`
strips it back down to exactly the 13 canonical columns for handoff -
nothing about this workflow needs to look different from what they
already do, beyond that one export step.
"""

import argparse
import csv
import html
import json
import sys
from pathlib import Path

from PIL import Image

from . import batch, cli, dewarp, orientation, render, spread
from .confidence import score_record
from .parser import CANON_FIELDS

REVIEW_FIELDS = CANON_FIELDS + ["confidence", "flag_reason", "review_image"]

# Vertical context around a flagged row, so a reviewer sees it in the
# table it came from (a neighbour row or two, the column header) rather
# than an isolated sliver of text; scaled to the row's own line height
# since adaptive zoom means that varies page to page. Horizontal is the
# full page width - narrowing it risks cropping out a second column's
# name that the row was actually about, or the faculty/degree context
# printed to one side.
_MIN_PAD_Y = 35
_PAD_Y_LINES = 2.5


def _rendered_page(geom: dict) -> Image.Image:
    """Reproduces the exact corrected page image a record's line came
    from - see parser.parse_lines_to_records's docstring for why "_geom"
    carries everything needed to redo this instead of keeping every
    rendered page around."""
    img = render.render_page(geom["pdf_path"], geom["page_index"], zoom=geom["zoom"])
    if geom["dewarped"]:
        img, _ = dewarp.correct_perspective(img)
    img = orientation.apply_rotation(img, geom["rotated_by"])
    if geom.get("spread_halves", 1) > 1:
        img = spread.split_spread(img)[geom["spread_half"]]
    return img


def _page_key(geom: dict) -> tuple:
    return (geom["pdf_path"], geom["page_index"])


def generate_snapshots(flagged: list[tuple[int, dict]], images_dir: Path) -> dict[int, str]:
    """flagged: [(row_index, geom), ...]. Renders each distinct source page
    once (flagged rows commonly cluster several-to-a-page) and crops one
    PNG per row. Returns {row_index: relative_path_to_png}."""
    images_dir.mkdir(parents=True, exist_ok=True)
    by_page: dict[tuple, list[tuple[int, dict]]] = {}
    for idx, geom in flagged:
        by_page.setdefault(_page_key(geom), []).append((idx, geom))

    paths: dict[int, str] = {}
    for (pdf_path, page_index), rows in by_page.items():
        img = _rendered_page(rows[0][1])
        w, h = img.size
        for idx, geom in rows:
            pad_y = max(_MIN_PAD_Y, _PAD_Y_LINES * max(geom["bottom"] - geom["top"], 1.0))
            x0, x1 = 0, w
            y0 = max(0, int(geom["top"] - pad_y))
            y1 = min(h, int(geom["bottom"] + pad_y))
            crop = img.crop((x0, y0, x1, y1))
            name = f"p{page_index}_{idx:06d}.png"
            crop.save(images_dir / name)
            paths[idx] = f"{images_dir.name}/{name}"
    return paths


def build_review_rows(records: list[dict]) -> tuple[list[dict], list[tuple[int, dict]]]:
    """Scores every record and returns (review_rows, flagged) where
    flagged is [(index_into_review_rows, geom), ...] for the
    non-high-confidence ones - the only ones that need a snapshot."""
    rows, flagged = [], []
    for i, r in enumerate(records):
        geom = r.get("_geom")
        confidence, reason = score_record(r["name"], geom)
        row = {k: r.get(k, "") for k in CANON_FIELDS}
        row["confidence"] = confidence
        row["flag_reason"] = reason
        row["review_image"] = ""
        rows.append(row)
        if confidence != "high" and geom:
            flagged.append((i, geom))
    return rows, flagged


def write_review_csv(records: list[dict], out_path: str, verbose: bool = True):
    rows, flagged = build_review_rows(records)
    out = Path(out_path)
    if flagged:
        images_dir = out.parent / f"{out.stem}_images"
        paths = generate_snapshots(flagged, images_dir)
        for idx, path in paths.items():
            rows[idx]["review_image"] = path

    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    if verbose:
        n = len(rows)
        by_conf = {c: sum(1 for r in rows if r["confidence"] == c) for c in ("high", "medium", "low")}
        print(f"Wrote {n} record(s) to {out}: {by_conf['high']} high, {by_conf['medium']} medium "
              f"({100*by_conf['medium']/max(n,1):.0f}%), {by_conf['low']} low ({100*by_conf['low']/max(n,1):.0f}%). "
              f"{len(flagged)} snapshot(s) in {out.stem}_images/", file=sys.stderr)


def export_clean_copy(review_csv_path: str, out_path: str, verbose: bool = True):
    """Strips a (corrected) review CSV back to exactly the canonical
    columns - the deliverable for the CTO's team. Keeps "source_file" if
    the input has it (batch.py's multi-file output carries it); drops
    confidence/flag_reason/review_image regardless of whether they're
    still present, already removed, or the DES renamed/reordered nothing
    since DictReader reads by header name, not position."""
    with open(review_csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = [c for c in (CANON_FIELDS + ["source_file"]) if reader.fieldnames and c in reader.fieldnames]
        rows = list(reader)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    if verbose:
        print(f"Wrote {len(rows)} record(s), {len(fields)} column(s), to {out_path}", file=sys.stderr)


_PREVIEW_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 0; background: #f4f5f7; color: #1c1e21; }
  header { position: sticky; top: 0; z-index: 5; background: #fff; border-bottom: 1px solid #ddd;
           padding: 12px 20px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  header h1 { font-size: 16px; margin: 0 18px 0 0; }
  .filters button { border: 1px solid #ccc; background: #fff; border-radius: 16px; padding: 6px 14px;
                     font-size: 13px; cursor: pointer; margin-right: 6px; }
  .filters button.active { background: #1c1e21; color: #fff; border-color: #1c1e21; }
  .filters .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  .search { margin-left: auto; }
  .search input { padding: 6px 10px; border: 1px solid #ccc; border-radius: 6px; font-size: 13px; width: 220px; }
  main { padding: 16px 20px 60px; }
  .row { background: #fff; border: 1px solid #e3e4e7; border-radius: 8px; margin-bottom: 10px;
         padding: 12px 14px; display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-start; }
  .row[hidden] { display: none; }
  .row .idx { color: #999; font-size: 11px; width: 42px; flex: none; padding-top: 3px; }
  .row .fields { flex: 3 1 320px; min-width: 220px; }
  .row .name { font-weight: 600; font-size: 14px; margin-bottom: 3px; }
  .row .ctx { color: #555; font-size: 12px; line-height: 1.5; }
  .badge { display: inline-block; font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 10px;
           text-transform: uppercase; letter-spacing: .03em; margin-bottom: 4px; }
  .badge.high { background: #e6f4ea; color: #1e7e34; }
  .badge.medium { background: #fff4e0; color: #a15c00; }
  .badge.low { background: #fde8e8; color: #b02a2a; }
  .reason { font-size: 11px; color: #888; margin-left: 6px; }
  .snap { flex: 2 1 320px; max-width: 420px; min-width: 240px; }
  .snap img { width: 100%; border: 1px solid #ddd; border-radius: 6px; cursor: zoom-in; display: block; }
  .snap .none { color: #bbb; font-size: 12px; padding: 30px 0; text-align: center; border: 1px dashed #ddd; border-radius: 6px; }
  #lightbox { position: fixed; inset: 0; background: rgba(0,0,0,.85); display: none;
              align-items: center; justify-content: center; z-index: 20; cursor: zoom-out; padding: 30px; }
  #lightbox.open { display: flex; }
  #lightbox img { max-width: 95%; max-height: 95%; border-radius: 4px; }
  .empty { text-align: center; color: #888; padding: 60px 0; }
</style>
</head>
<body>
<header>
  <h1>{title}</h1>
  <div class="filters" id="filters"></div>
  <div class="search"><input id="search" type="text" placeholder="Filter by name..."></div>
</header>
<main id="main"></main>
<div id="lightbox"><img id="lightbox-img" src=""></div>
<script>
const ROWS = {data_json};
const COUNTS = {counts_json};
const main = document.getElementById('main');
const filtersEl = document.getElementById('filters');
let activeFilter = 'flagged';

const FILTERS = [
  ['flagged', 'Needs review', (COUNTS.medium||0) + (COUNTS.low||0)],
  ['low', 'Low', COUNTS.low||0],
  ['medium', 'Medium', COUNTS.medium||0],
  ['high', 'High', COUNTS.high||0],
  ['all', 'All', ROWS.length],
];
for (const [key, label, count] of FILTERS) {
  const b = document.createElement('button');
  b.textContent = label + ' (' + count + ')';
  b.dataset.key = key;
  b.className = key === activeFilter ? 'active' : '';
  b.onclick = () => { activeFilter = key; render(); };
  filtersEl.appendChild(b);
}

function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

function rowMatches(r) {
  if (activeFilter === 'flagged') return r.confidence !== 'high';
  if (activeFilter === 'all') return true;
  return r.confidence === activeFilter;
}

function render() {
  for (const btn of filtersEl.children) btn.className = btn.dataset.key === activeFilter ? 'active' : '';
  const q = document.getElementById('search').value.trim().toLowerCase();
  main.innerHTML = '';
  let shown = 0;
  for (const r of ROWS) {
    if (!rowMatches(r)) continue;
    if (q && !r.name.toLowerCase().includes(q)) continue;
    shown++;
    const div = document.createElement('div');
    div.className = 'row';
    const ctx = [r.faculty, r.department, r.course, r.degree, r.grade, r.year].filter(Boolean).join(' \\u00b7 ');
    div.innerHTML =
      '<div class="idx">#' + r.i + '<br>p.' + esc(r.page) + '</div>' +
      '<div class="fields">' +
        '<span class="badge ' + r.confidence + '">' + r.confidence + '</span>' +
        (r.flag_reason ? '<span class="reason">' + esc(r.flag_reason) + '</span>' : '') +
        '<div class="name">' + esc(r.name) + '</div>' +
        '<div class="ctx">' + esc(ctx) + '</div>' +
      '</div>' +
      '<div class="snap">' + (r.review_image
        ? '<img loading="lazy" src="' + esc(r.review_image) + '" onclick="openLightbox(\\'' + esc(r.review_image) + '\\')">'
        : '<div class="none">no snapshot</div>') + '</div>';
    main.appendChild(div);
  }
  if (!shown) main.innerHTML = '<div class="empty">No rows match.</div>';
}

function openLightbox(src) {
  document.getElementById('lightbox-img').src = src;
  document.getElementById('lightbox').classList.add('open');
}
document.getElementById('lightbox').onclick = () => document.getElementById('lightbox').classList.remove('open');
document.getElementById('search').oninput = render;
render();
</script>
</body>
</html>
"""


def write_html_preview(review_csv_path: str, out_html_path: str, verbose: bool = True):
    """A local, self-contained review page for one review CSV - what a
    Data Entry Specialist actually works from, instead of a CSV and a
    folder of images they'd have to juggle by hand. No network calls, no
    external assets: these are real people's academic records, so this
    stays a plain file on disk, opened directly in a browser (file://),
    never uploaded anywhere. Defaults to showing only flagged
    (medium/low) rows - the point of the whole exercise - with a filter
    to see all of them."""
    with open(review_csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    out = Path(out_html_path)
    data = []
    counts = {"high": 0, "medium": 0, "low": 0}
    for i, r in enumerate(rows):
        conf = r.get("confidence", "high") or "high"
        counts[conf] = counts.get(conf, 0) + 1
        img = r.get("review_image", "")
        data.append({
            "i": i, "name": r.get("name", ""), "faculty": r.get("faculty", ""),
            "department": r.get("department", ""), "course": r.get("course", ""),
            "degree": r.get("degree", ""), "grade": r.get("grade", ""), "year": r.get("year", ""),
            "page": r.get("page", ""), "confidence": conf, "flag_reason": r.get("flag_reason", ""),
            "review_image": img,
        })

    page = _PREVIEW_TEMPLATE.replace("{title}", html.escape(out.stem))
    page = page.replace("{data_json}", json.dumps(data))
    page = page.replace("{counts_json}", json.dumps(counts))
    out.write_text(page, encoding="utf-8")
    if verbose:
        print(f"Wrote preview of {len(rows)} record(s) to {out} "
              f"({counts['high']} high, {counts['medium']} medium, {counts['low']} low)", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    ex = sub.add_parser("extract", help="Extract a university/year folder into a flagged review CSV + snapshots")
    ex.add_argument("path")
    ex.add_argument("-o", "--out", required=True)
    ex.add_argument("--zoom", type=float, default=2.0)
    ex.add_argument("--quiet", action="store_true")

    exp = sub.add_parser("export", help="Strip a corrected review CSV down to the canonical columns")
    exp.add_argument("review_csv")
    exp.add_argument("-o", "--out", required=True)

    pv = sub.add_parser("preview", help="Render a review CSV as a local, filterable HTML page")
    pv.add_argument("review_csv")
    pv.add_argument("-o", "--out", required=True)

    args = ap.parse_args()
    if args.cmd == "extract":
        verbose = not args.quiet
        records = batch.process_folder(args.path, zoom=args.zoom, verbose=verbose)
        write_review_csv(records, args.out, verbose=verbose)
    elif args.cmd == "preview":
        write_html_preview(args.review_csv, args.out)
    elif args.cmd == "export":
        export_clean_copy(args.review_csv, args.out)


if __name__ == "__main__":
    main()
