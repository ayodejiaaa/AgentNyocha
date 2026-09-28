// Normalizes the 379 raw grade-value variants found across Akowe's 57 training
// spreadsheets into a small set of canonical categories (British spelling),
// separating out "Distinction in <subject>" footnote annotations into their
// own field rather than discarding them.

const fs = require('fs');
const path = require('path');

function parseCsv(text) {
  const rows = [];
  let row = [], field = '', inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; }
        else { inQuotes = false; }
      } else field += c;
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

const CANON = {
  FIRST: 'First Class Honours',
  SECOND_UPPER: 'Second Class Honours (Upper Division)',
  SECOND_LOWER: 'Second Class Honours (Lower Division)',
  THIRD: 'Third Class Honours',
  PASS: 'Pass',
  DISTINCTION: 'Distinction',
  MERIT: 'Merit',
  UPPER_CREDIT: 'Upper Credit',
  LOWER_CREDIT: 'Lower Credit',
  CREDIT: 'Credit',
  AEGROTAT: 'Aegrotat',
  HONOURS: 'Honours',
  NA: 'N/A',
};

function extractDistinctionNotes(s) {
  // Pulls out every "Distinction in <subject>" / "DISTINCTION IN <SUBJECT>" clause,
  // wherever it occurs (comma-separated, footnote-marker-prefixed, etc.)
  const notes = [];
  const re = /distinction\s+in\s+([^,;]+)/gi;
  let m;
  while ((m = re.exec(s)) !== null) {
    notes.push(m[1].trim().replace(/\s+/g, ' '));
  }
  const stripped = s.replace(re, '').replace(/[,;]/g, ' ');
  return { stripped, notes };
}

function normalize(rawValue) {
  const original = rawValue;
  let s = rawValue.trim();

  if (s === '') return { canonical: '', notes: '', flag: 'blank' };

  // Pure garbage: numeric-only (e.g. leaked Excel error code) or symbol-only strings
  if (/^-?\d{4,}$/.test(s)) return { canonical: '', notes: original, flag: 'GARBAGE_NUMERIC' };
  if (/^[^A-Za-z0-9]+$/.test(s)) return { canonical: '', notes: original, flag: 'GARBAGE_SYMBOLS' };
  if (/^[A-Za-z]$/.test(s)) return { canonical: '', notes: original, flag: 'GARBAGE_SINGLE_CHAR' };

  // N/A family
  if (/^N\s*\/?\s*A\.?$/i.test(s)) return { canonical: CANON.NA, notes: '', flag: 'ok' };

  // Strip footnote marker prefixes like "µ", "@", "#", "$", "%", "&", "*", "'", "?", "£", "©", "Ω", "β", "^", "+", "V", "r " etc.
  // that precede a real word (common OCR/footnote artefact)
  s = s.replace(/^[\W_]+(?=[A-Za-z])/u, '');

  const { stripped, notes: distinctionNotes } = extractDistinctionNotes(s);
  let base = stripped.trim().replace(/\s+/g, ' ');

  // If, after stripping distinction clauses, nothing meaningful is left but we DID
  // find distinction notes, and there's no class-of-honours info, treat the whole
  // thing as a pure "Distinction" grade (postgrad/professional viva distinction).
  const baseIsEmptyish = base.replace(/[^A-Za-z]/g, '').length === 0;

  // Aegrotat
  if (/AEGROTAT/i.test(base)) {
    return { canonical: CANON.AEGROTAT, notes: distinctionNotes.join('; '), flag: 'ok' };
  }

  // Fix common OCR/typo tokens before keyword matching
  let norm = base.toUpperCase();
  norm = norm
    .replace(/\bTHRID\b/g, 'THIRD')
    .replace(/\bSECON\b/g, 'SECOND')
    .replace(/\bCL\s+ASS\b/g, 'CLASS')
    .replace(/\bLIPPER\b/g, 'UPPER')
    .replace(/\bLOWOR\b/g, 'LOWER')
    .replace(/\bUPPPER\b/g, 'UPPER')
    .replace(/\bOWER\b/g, 'LOWER')
    // insert a space where UPPER/LOWER/DIVISION got fused onto the previous word
    .replace(/([A-Z])(UPPER|LOWER|DIVISION)\b/g, '$1 $2')
    .replace(/\b1ST\b/g, 'FIRST')
    .replace(/\b2ND\b/g, 'SECOND')
    .replace(/\b3RD\b/g, 'THIRD')
    .replace(/HON\.?OU?RS?\b/g, 'HONOURS')      // HONOUR/HONORS/HON/HONOURS -> HONOURS
    .replace(/HONOURSS/g, 'HONOURS')
    .replace(/DIVIS[OI0]NS?/g, 'DIVISION')       // DIVISON/DIVSION/DIVISION0/DMSION-ish
    .replace(/DM?SION/g, 'DIVISION')
    .replace(/D[EI]VISION/g, 'DIVISION')
    .replace(/CLAS{1,3}\b/g, 'CLASS')
    .replace(/\bLASS\b/g, 'CLASS')               // OCR-dropped leading C in "CLASS"
    .replace(/SEC[O0]U?ND\s*CLASS/g, 'SECOND CLASS')
    .replace(/SEC[O0]U?ND/g, 'SECOND')
    .replace(/THIRDCLASS/g, 'THIRD CLASS')
    .replace(/SECONDCLASS/g, 'SECOND CLASS')
    .replace(/THR?I?RD/g, 'THIRD')
    .replace(/UPPE?RE?R?\b/g, 'UPPER')
    .replace(/L[O0]WE?RE?R?\b/g, 'LOWER')
    .replace(/DIST[I1]C?TION/g, 'DISTINCTION')
    .replace(/[.:,;'"()\[\]{}]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();

  const hasFirst = /\bFIRST\b/.test(norm);
  const hasSecond = /\bSECOND\b/.test(norm);
  const hasThird = /\bTHIRD\b/.test(norm);
  const hasUpper = /\bUPPER\b/.test(norm);
  const hasLower = /\bLOWER\b/.test(norm);
  const hasPass = /\bPASS(ED)?\b/.test(norm);
  const hasDistinctionWord = /\bDISTINCTION\b/.test(norm);
  const hasMerit = /\bMERIT\b/.test(norm);
  const hasCredit = /\bCREDIT\b/.test(norm);
  const hasHonours = /\bHONOURS\b/.test(norm);
  const hasClass = /\bCLASS\b/.test(norm);
  const hasDegree = /\bDEGREE\b/.test(norm) || /MB\s*[;:]?\s*BS/.test(norm);

  let canonical = null;

  // Orphaned footnote markers (single short tokens like "P", "R", "V", leftover
  // after stripping "Distinction in <subject>" clauses) shouldn't block a pure-
  // distinction classification when no real class-of-honours keyword is present.
  const hasAnyClassKeyword = hasFirst || hasSecond || hasThird || hasPass || hasMerit || hasCredit || hasDegree;
  const leftoverIsJustNoise = norm.split(' ').every(tok => tok.length <= 2);

  if (hasSecond && (hasClass || hasHonours)) {
    if (hasUpper) canonical = CANON.SECOND_UPPER;
    else if (hasLower) canonical = CANON.SECOND_LOWER;
    else canonical = 'Second Class Honours (Division unspecified)';
  } else if (hasFirst && (hasClass || hasHonours)) {
    canonical = CANON.FIRST;
  } else if (hasThird && (hasClass || hasHonours || norm === 'THIRD')) {
    canonical = CANON.THIRD;
  } else if (hasCredit && hasUpper) {
    canonical = CANON.UPPER_CREDIT;
  } else if (hasCredit && hasLower) {
    canonical = CANON.LOWER_CREDIT;
  } else if (hasCredit) {
    canonical = CANON.CREDIT;
  } else if (hasMerit) {
    canonical = CANON.MERIT;
  } else if (hasPass) {
    canonical = CANON.PASS;
  } else if (hasDegree) {
    canonical = CANON.NA; // professional degree, not classified into honours
  } else if (!hasAnyClassKeyword && (hasUpper || hasLower)) {
    // Standalone "UPPER DIVISION" / "LOWER DIVISION" / "LOWER CLASS" with no
    // First/Second/Third word: this division terminology is only ever used for
    // Second Class in this grading system, so it's inferred as Second Class.
    canonical = hasUpper ? CANON.SECOND_UPPER : CANON.SECOND_LOWER;
  } else if (hasDistinctionWord) {
    canonical = CANON.DISTINCTION;
  } else if (distinctionNotes.length > 0 && !hasAnyClassKeyword && leftoverIsJustNoise) {
    canonical = CANON.DISTINCTION;
  } else if (baseIsEmptyish && distinctionNotes.length > 0) {
    canonical = CANON.DISTINCTION;
  } else if (hasHonours && !hasAnyClassKeyword && norm.replace(/HONOURS/g, '').trim() === '') {
    // Bare "HONOURS" with no First/Second/Third/Pass qualifier - confirmed (by
    // manual source check) to be a genuine standalone category on some MB;BS
    // pages, not a truncated/ambiguous value.
    canonical = CANON.HONOURS;
  }

  if (!canonical) {
    return { canonical: '', notes: distinctionNotes.join('; '), flag: 'NEEDS_REVIEW', normalizedAttempt: norm };
  }

  return { canonical, notes: distinctionNotes.join('; '), flag: 'ok' };
}

module.exports = { normalize, CANON };

if (require.main === module) {
  const raw = fs.readFileSync(path.join(__dirname, 'raw_grade_values.csv'), 'utf8');
  const rows = parseCsv(raw).slice(1).filter(r => r.length >= 2 && r[0] !== '');

  const results = rows.map(([value, countStr]) => {
    const count = parseInt(countStr, 10) || 0;
    const r = normalize(value);
    return { raw: value, count, ...r };
  });

  // Summary stats
  let totalRows = 0, okRows = 0, reviewRows = 0, garbageRows = 0;
  const canonicalTotals = {};
  for (const r of results) {
    totalRows += r.count;
    if (r.flag === 'ok') {
      okRows += r.count;
      canonicalTotals[r.canonical] = (canonicalTotals[r.canonical] || 0) + r.count;
    } else if (r.flag === 'NEEDS_REVIEW') {
      reviewRows += r.count;
    } else if (r.flag.startsWith('GARBAGE')) {
      garbageRows += r.count;
    }
  }

  console.log(`Total rows represented: ${totalRows}`);
  console.log(`Mapped OK: ${okRows} (${(100*okRows/totalRows).toFixed(2)}%)`);
  console.log(`Needs review: ${reviewRows} (${(100*reviewRows/totalRows).toFixed(2)}%) across ${results.filter(r=>r.flag==='NEEDS_REVIEW').length} distinct raw values`);
  console.log(`Garbage: ${garbageRows} rows across ${results.filter(r=>r.flag.startsWith('GARBAGE')).length} distinct raw values`);
  console.log('');
  console.log('Canonical grade distribution:');
  Object.entries(canonicalTotals).sort((a,b)=>b[1]-a[1]).forEach(([k,v]) => {
    console.log(`  ${v.toString().padStart(7)}  ${k}`);
  });

  console.log('');
  console.log('=== NEEDS_REVIEW values (raw, count) ===');
  results.filter(r => r.flag === 'NEEDS_REVIEW').sort((a,b)=>b.count-a.count).forEach(r => {
    console.log(`  [${r.count}] "${r.raw}"  (normalized attempt: "${r.normalizedAttempt}")`);
  });

  console.log('');
  console.log('=== GARBAGE values ===');
  results.filter(r => r.flag.startsWith('GARBAGE')).forEach(r => {
    console.log(`  [${r.count}] "${r.raw}"  (${r.flag})`);
  });

  fs.writeFileSync(path.join(__dirname, 'grade_mapping.json'), JSON.stringify(results, null, 2));
  console.log('\nWrote grade_mapping.json');
}
