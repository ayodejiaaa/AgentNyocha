// Normalizes the 811 raw "year" variants into a canonical academic-session
// format (YYYY/YYYY, or plain YYYY when the source gives no range), pulling
// any semester/yearbook qualifier into a separate note rather than discarding it.

const fs = require('fs');
const path = require('path');

function parseCsv(text) {
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

function fixOcrDigits(s) {
  // OCR/typo: letter O used in place of digit 0 inside number-like tokens
  return s.replace(/\b(\d*[0O]\d*[0O]?\d*)\b/g, (m) => /\d/.test(m) ? m.replace(/O/g, '0') : m);
}

function normalize(rawValue) {
  let s = rawValue.trim();
  if (s === '') return { canonical: '', note: '', flag: 'blank' };

  // N/A passthrough - a legitimate "not stated in source" marker, not garbage
  if (/^N\s*\/?\s*A\.?$/i.test(s)) return { canonical: 'N/A', note: '', flag: 'ok' };

  s = fixOcrDigits(s);

  // Pull out a semester qualifier if present (Rain/Harmattan Semester, First/Second Semester)
  let note = '';
  const semMatch = s.match(/\b(rain|harmattan|first|second)\s+semester\b/i);
  if (semMatch) {
    note = semMatch[0].replace(/\s+/g, ' ').trim();
    // Title-case it
    note = note.replace(/\w\S*/g, w => w[0].toUpperCase() + w.slice(1).toLowerCase());
  }

  // Excel date-serial leakage (a year cell that got auto-converted to a date
  // by Excel and whose underlying serial number leaked into this raw text) -
  // e.g. "45457". These carry no recoverable year information; must be
  // corrected by hand against the source page image, not guessed.
  if (/^\d{5,6}$/.test(s)) {
    return { canonical: '', note, flag: 'CORRUPTED_DATE_SERIAL' };
  }

  // Find a YYYY/YYYY (or YYYY / YYYY, YYYY-YYYY) pair anywhere in the string
  const pairMatch = s.match(/(19|20)\d{2}\s*[\/\-]\s*(19|20)\d{2}/);
  if (pairMatch) {
    const nums = pairMatch[0].match(/(19|20)\d{2}/g);
    const y1 = parseInt(nums[0], 10);
    const y2 = parseInt(nums[1], 10);
    if (y2 < y1) return { canonical: `${y1}/${y2}`, note, flag: 'NEEDS_REVIEW_REVERSED' };
    const gap = y2 - y1;
    // gap of 1 (normal session) or 2 (known combined-session convocations,
    // e.g. post-strike/COVID delays) are both genuine; anything wider is
    // almost certainly a mis-OCR'd digit in the second year.
    const flag = (gap <= 2) ? 'ok' : 'NEEDS_REVIEW_WIDE_GAP';
    return { canonical: `${y1}/${y2}`, note, flag };
  }

  // Two-digit shorthand pair, e.g. "18/19 SESSION" -> 2018/2019
  const shortPair = s.match(/\b(\d{2})\s*\/\s*(\d{2})\b/);
  if (shortPair) {
    const toFullYear = (yy) => { const n = parseInt(yy, 10); return n < 50 ? 2000 + n : 1900 + n; };
    const y1 = toFullYear(shortPair[1]);
    const y2 = toFullYear(shortPair[2]);
    if (y2 === y1 + 1) return { canonical: `${y1}/${y2}`, note, flag: 'ok' };
    return { canonical: `${y1}/${y2}`, note, flag: 'NEEDS_REVIEW_WIDE_GAP' };
  }

  // Otherwise find a single bare 4-digit year (e.g. "UNILAG 2024 YEARBOOK")
  const singleMatch = s.match(/(19|20)\d{2}/);
  if (singleMatch) {
    return { canonical: singleMatch[0], note, flag: 'ok-single-year' };
  }

  // No year-like content at all (e.g. grade text that leaked into this
  // column for a handful of rows) - genuinely unrecoverable without going
  // back to the source page.
  return { canonical: '', note, flag: 'NO_YEAR_IN_SOURCE' };
}

module.exports = { normalize };

if (require.main === module) {
  const raw = fs.readFileSync(path.join(__dirname, 'raw_year_values.csv'), 'utf8');
  const rows = parseCsv(raw).slice(1).filter(r => r.length >= 2 && r[0] !== '');

  const results = rows.map(([value, countStr]) => {
    const count = parseInt(countStr, 10) || 0;
    const r = normalize(value);
    return { raw: value, count, ...r };
  });

  let total = 0, ok = 0, okSingle = 0, review = 0, corrupted = 0, noYear = 0;
  const canonicalTotals = {};
  for (const r of results) {
    total += r.count;
    if (r.flag === 'ok') { ok += r.count; canonicalTotals[r.canonical] = (canonicalTotals[r.canonical]||0) + r.count; }
    else if (r.flag === 'ok-single-year') { okSingle += r.count; canonicalTotals[r.canonical] = (canonicalTotals[r.canonical]||0) + r.count; }
    else if (r.flag === 'CORRUPTED_DATE_SERIAL') corrupted += r.count;
    else if (r.flag === 'NO_YEAR_IN_SOURCE') noYear += r.count;
    else review += r.count;
  }

  console.log(`Total rows represented: ${total}`);
  console.log(`Mapped to YYYY/YYYY: ${ok} (${(100*ok/total).toFixed(2)}%)`);
  console.log(`Mapped to single YYYY (no range in source): ${okSingle} (${(100*okSingle/total).toFixed(2)}%)`);
  console.log(`Corrupted Excel date-serial (unrecoverable, needs source lookup): ${corrupted}`);
  console.log(`No year-like content found at all: ${noYear}`);
  console.log(`Needs review (reversed / implausible gap): ${review} across ${results.filter(r=>r.flag.startsWith('NEEDS_REVIEW')).length} distinct raw values`);
  console.log(`Distinct canonical year values produced: ${Object.keys(canonicalTotals).length} (down from ${rows.length} raw variants)`);

  console.log('\n=== NEEDS_REVIEW values ===');
  results.filter(r => r.flag.startsWith('NEEDS_REVIEW')).sort((a,b)=>b.count-a.count).forEach(r => {
    console.log(`  [${r.count}] "${r.raw}"  -> attempted "${r.canonical}" (${r.flag})`);
  });
  console.log('\n=== NO_YEAR_IN_SOURCE values ===');
  results.filter(r => r.flag === 'NO_YEAR_IN_SOURCE').forEach(r => {
    console.log(`  [${r.count}] "${r.raw}"`);
  });

  console.log('\n=== Sample of rows where a semester/yearbook note was captured ===');
  results.filter(r => r.note).slice(0, 15).forEach(r => {
    console.log(`  "${r.raw}" -> canonical="${r.canonical}" note="${r.note}"`);
  });

  fs.writeFileSync(path.join(__dirname, 'year_mapping.json'), JSON.stringify(results, null, 2));
  console.log('\nWrote year_mapping.json');
}
