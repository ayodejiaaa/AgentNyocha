// Reads the raw column-mapped extracts (one CSV per source file), applies the
// tested grade/year normalizers live (so it works identically whether the
// value came from messy original data or from a hand-verified correction),
// splits out distinction_notes/year_note, deduplicates exact-match rows
// within each file, and writes:
//   - one canonical CSV per source file, mirroring the original folder layout
//   - one master CSV combining everything, with trailing traceability columns
//
// This also doubles as the reference implementation for the post-processing
// step the eventual PDF-extraction tool should reuse: raw OCR/text output ->
// normalizeGrade()/normalizeYear() -> canonical schema row.

const fs = require('fs');
const path = require('path');
const { normalize: normalizeGrade } = require('./normalize_grades.js');
const { normalize: normalizeYear } = require('./normalize_years.js');

const EXTRACT_DIR = path.join(__dirname, 'raw_extract');
const OUT_DIR = process.env.CANON_OUT_DIR || path.join(__dirname, '..', 'Canonical Training Data');
const REPORT_PATH = process.env.CANON_REPORT_PATH || path.join(__dirname, 'build_report.json');
const CANON_FIELDS = ['name','faculty','department','course','degree','grade','distinction_notes','year','year_note','page','programme','affiliated_institution','country'];

function parseCsv(text) {
  text = text.replace(/^﻿/, '');
  const rows = [];
  let row = [], field = '', inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') { if (text[i + 1] === '"') { field += '"'; i++; } else inQuotes = false; }
      else field += c;
    } else {
      if (c === '"') inQuotes = true;
      else if (c === ',') { row.push(field); field = ''; }
      else if (c === '\r') { /* skip */ }
      else if (c === '\n') { row.push(field); rows.push(row); row = []; field = ''; }
      else field += c;
    }
  }
  if (field.length || row.length) { row.push(field); rows.push(row); }
  return rows;
}

function csvEscape(v) {
  v = String(v ?? '');
  return '"' + v.replace(/"/g, '""') + '"';
}

function cleanText(v) {
  return String(v ?? '').replace(/\s+/g, ' ').trim();
}

if (!fs.existsSync(OUT_DIR)) fs.mkdirSync(OUT_DIR, { recursive: true });

const files = fs.readdirSync(EXTRACT_DIR).filter(f => f.endsWith('.csv') && f !== '_manifest.csv');

// Hand-resolved values the normalizers cannot produce on their own, read from
// manual_overrides.csv (git-ignored: it names individual people). One row each:
// File, Page, Name, Field (grade | distinction_notes), NewValue, Note.
// A "grade" override replaces the grade; a "distinction_notes" override is added to
// any notes already extracted from the grade cell (joined with "; ").
// Every override must match a row of the extract or the build stops: a fix that
// silently stops applying is how three distinction notes once went missing.
const OVERRIDES_PATH = path.join(__dirname, 'manual_overrides.csv');
const OVERRIDES = new Map(); // "relPath|page|name|field" -> { value, used }
if (fs.existsSync(OVERRIDES_PATH)) {
  const orows = parseCsv(fs.readFileSync(OVERRIDES_PATH, 'utf8'));
  const oh = Object.fromEntries(orows[0].map((h, i) => [h, i]));
  for (const r of orows.slice(1)) {
    if (r.length < orows[0].length) continue;
    OVERRIDES.set(`${r[oh.File]}|${cleanText(r[oh.Page])}|${cleanText(r[oh.Name])}|${r[oh.Field]}`,
                  { value: r[oh.NewValue], used: 0 });
  }
}
function overrideFor(relPath, page, name, field) {
  const o = OVERRIDES.get(`${relPath}|${page}|${name}|${field}`);
  if (o) o.used++;
  return o ? o.value : undefined;
}
const CONFIRMED_WIDE_GAP_YEARS = new Set(['2006/2017']);

const masterRows = [];
const perFileReport = [];
let totalIn = 0, totalOut = 0, totalDupRemoved = 0;
let unresolvedGrade = [];
let unresolvedYear = [];

for (const fname of files) {
  const relPath = fname.replace(/\.csv$/, '').replace(/__/g, '\\');
  const university = relPath.split('\\')[0];
  const text = fs.readFileSync(path.join(EXTRACT_DIR, fname), 'utf8');
  const rows = parseCsv(text);
  const header = rows[0];
  const idx = Object.fromEntries(header.map((h, i) => [h, i]));
  const dataRows = rows.slice(1).filter(r => r.length >= header.length);

  const seen = new Set();
  const outRows = [];
  let dupCount = 0;

  for (const r of dataRows) {
    const name = cleanText(r[idx.name]);
    if (!name) continue; // skip any fully blank trailing rows
    const faculty = cleanText(r[idx.faculty]);
    const department = cleanText(r[idx.department]);
    const course = cleanText(r[idx.course]);
    const degree = cleanText(r[idx.degree]);
    const page = cleanText(r[idx.page]);
    const programme = cleanText(r[idx.programme]);
    const affiliated_institution = cleanText(r[idx.affiliated_institution]);
    const country = cleanText(r[idx.country]);

    const gradeRaw = r[idx.grade];
    const gRes = normalizeGrade(gradeRaw);
    let grade = gRes.canonical;
    let distinction_notes = gRes.notes || '';
    const gradeOverride = overrideFor(relPath, page, name, 'grade');
    if (gradeOverride !== undefined) {
      grade = gradeOverride;
    } else if (gRes.flag !== 'ok' && gRes.flag !== 'blank') {
      unresolvedGrade.push({ relPath, name, page, raw: gradeRaw, flag: gRes.flag });
    }
    const noteOverride = overrideFor(relPath, page, name, 'distinction_notes');
    if (noteOverride !== undefined) {
      distinction_notes = distinction_notes ? `${distinction_notes}; ${noteOverride}` : noteOverride;
    }

    const yearRaw = r[idx.year];
    const yRes = normalizeYear(yearRaw);
    let year = yRes.canonical;
    const year_note = yRes.note || '';
    if (yRes.flag !== 'ok' && yRes.flag !== 'ok-single-year' && yRes.flag !== 'blank'
        && !CONFIRMED_WIDE_GAP_YEARS.has(yRes.canonical)) {
      unresolvedYear.push({ relPath, name, page, raw: yearRaw, flag: yRes.flag });
    }

    const canonRow = [name, faculty, department, course, degree, grade, distinction_notes, year, year_note, page, programme, affiliated_institution, country];
    const sig = canonRow.join('');
    if (seen.has(sig)) { dupCount++; continue; }
    seen.add(sig);
    outRows.push(canonRow);
  }

  // Write per-file canonical CSV, mirroring original folder structure
  const outPath = path.join(OUT_DIR, relPath.replace(/\.xlsx$/i, '') + '.csv');
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  const lines = [CANON_FIELDS.join(',')];
  for (const row of outRows) lines.push(row.map(csvEscape).join(','));
  fs.writeFileSync(outPath, lines.join('\n') + '\n', 'utf8');

  for (const row of outRows) masterRows.push([...row, university, relPath]);

  totalIn += dataRows.length;
  totalOut += outRows.length;
  totalDupRemoved += dupCount;
  perFileReport.push({ relPath, university, inRows: dataRows.length, outRows: outRows.length, dupRemoved: dupCount });
}

const unusedOverrides = [...OVERRIDES].filter(([, o]) => o.used === 0);
if (unusedOverrides.length) {
  console.error(`STOPPING: ${unusedOverrides.length} manual override(s) matched no row of the extract.`);
  process.exit(1);
}

// Master combined CSV: canonical fields + traceability columns
const masterLines = [[...CANON_FIELDS, 'source_university', 'source_file'].join(',')];
for (const row of masterRows) masterLines.push(row.map(csvEscape).join(','));
fs.writeFileSync(path.join(OUT_DIR, 'akowe_canonical_master.csv'), masterLines.join('\n') + '\n', 'utf8');

console.log(`Files processed: ${perFileReport.length}`);
console.log(`Total input rows: ${totalIn}`);
console.log(`Total output rows: ${totalOut}`);
console.log(`Duplicate rows removed: ${totalDupRemoved}`);
console.log(`Unresolved grade values remaining: ${unresolvedGrade.length}`);
console.log(`Unresolved year values remaining: ${unresolvedYear.length}`);
console.log(`Manual overrides applied: ${OVERRIDES.size}`);

if (unresolvedGrade.length) {
  console.log('\n=== Unresolved grade rows ===');
  unresolvedGrade.slice(0, 30).forEach(u => console.log(`  ${u.relPath} p.${u.page} ${u.name}: "${u.raw}" (${u.flag})`));
}
if (unresolvedYear.length) {
  console.log('\n=== Unresolved year rows ===');
  unresolvedYear.slice(0, 30).forEach(u => console.log(`  ${u.relPath} p.${u.page} ${u.name}: "${u.raw}" (${u.flag})`));
}

fs.writeFileSync(REPORT_PATH, JSON.stringify({
  totalIn, totalOut, totalDupRemoved, overridesApplied: OVERRIDES.size,
  perFileReport, unresolvedGrade, unresolvedYear
}, null, 1));
console.log('\nWrote build_report.json');
console.log(`Canonical dataset written to: ${OUT_DIR}`);
