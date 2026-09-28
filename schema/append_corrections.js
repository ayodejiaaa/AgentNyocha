// Helper: append blanket or per-name corrections to corrections_log.csv
const fs = require('fs');
const path = require('path');
const groupsPath = path.join(__dirname, 'flagged_groups.json');
const logPath = path.join(__dirname, 'corrections_log.csv');
const groups = JSON.parse(fs.readFileSync(groupsPath, 'utf8'));

function csvEscape(v) {
  v = String(v ?? '');
  if (/[",\n]/.test(v)) return '"' + v.replace(/"/g, '""') + '"';
  return v;
}

function appendBlanket(fileKeyFragment, page, newYear, note) {
  const key = `${fileKeyFragment} | p.${page}`;
  const g = groups.find(g => g.key === key);
  if (!g) { console.log('NOT FOUND:', key); return; }
  const lines = [];
  for (const r of g.rows) {
    if (r.field === 'year') {
      lines.push([r.raw.includes('File')?'':'', ].join(''));
    }
  }
  const out = g.rows.filter(r=>r.field==='year').map(r =>
    [fileKeyFragment, r.row, r.name, 'year', r.raw, newYear, note].map(csvEscape).join(',')
  );
  fs.appendFileSync(logPath, out.join('\n') + '\n');
  console.log(`Appended ${out.length} year corrections for ${key} -> ${newYear}`);
}

module.exports = { appendBlanket, groups };

// CLI usage: node append_corrections.js "UI\\university-of-ibadan-2014.xlsx" 315 2014 "note text"
if (require.main === module) {
  const [,, fileFrag, page, newYear, ...noteParts] = process.argv;
  appendBlanket(fileFrag, page, newYear, noteParts.join(' '));
}
