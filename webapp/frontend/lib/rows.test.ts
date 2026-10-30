// Tests for which rows the review screen shows. Run with:  npm test
import assert from "node:assert/strict";
import { test } from "node:test";
import { filterRows, rowCounts, type Row } from "./rows";

let n = 0;
const row = (o: Partial<Row> & { id?: number } = {}) => ({
  id: ++n, confidence: "low", reviewed: false, removed: false, name: "ADEYEMI Olufemi", page_id: 1, ...o,
});

const rows = [
  row({ id: 1, confidence: "high", name: "OKAFOR Chidi Emeka" }),
  row({ id: 2, confidence: "medium", name: "BELLO Aminat Zainab" }),
  row({ id: 3, confidence: "low", name: "Samuel", page_id: 2 }),
  row({ id: 4, confidence: "low", name: "ADEYEMI Olufemi", reviewed: true }),
  row({ id: 5, confidence: "low", name: "Tunde", removed: true, page_id: 2 }),
  row({ id: 6, confidence: "medium", name: "Okonkwo Ngozi", removed: true }),
];
const ids = (list: { id: number }[]) => list.map((r) => r.id);

test("counts leave removed rows out of every group, and count them on their own", () => {
  assert.deepEqual(rowCounts(rows), { high: 1, medium: 1, low: 2, needsReview: 2, all: 4, removed: 2 });
});

test("removing a flagged row takes it off the work list", () => {
  const before = rowCounts([row({ id: 1 })]).needsReview;
  const after = rowCounts([row({ id: 1, removed: true })]).needsReview;
  assert.deepEqual([before, after], [1, 0]);
});

test("removed rows are hidden in every view except Removed", () => {
  for (const f of ["flagged", "low", "medium", "high", "all"] as const) {
    assert.ok(!filterRows(rows, f, "", null).some((r) => r.removed), `${f} must not show a removed row`);
  }
  assert.deepEqual(ids(filterRows(rows, "removed", "", null)), [5, 6]);
});

test("each group shows its own rows", () => {
  assert.deepEqual(ids(filterRows(rows, "flagged", "", null)), [2, 3]);
  assert.deepEqual(ids(filterRows(rows, "low", "", null)), [3, 4]);
  assert.deepEqual(ids(filterRows(rows, "medium", "", null)), [2]);
  assert.deepEqual(ids(filterRows(rows, "high", "", null)), [1]);
  assert.deepEqual(ids(filterRows(rows, "all", "", null)), [1, 2, 3, 4]);
});

test("searching by name works in every view, ignoring case and stray spaces", () => {
  assert.deepEqual(ids(filterRows(rows, "all", "  okafor ", null)), [1]);
  assert.deepEqual(ids(filterRows(rows, "removed", "TUNDE", null)), [5]);
  assert.deepEqual(ids(filterRows(rows, "flagged", "samuel", null)), [3]);
  assert.deepEqual(ids(filterRows(rows, "all", "nobody", null)), []);
});

test("showing one page shows every kept row on it, whatever the group, and not removed ones", () => {
  assert.deepEqual(ids(filterRows(rows, "all", "", 2)), [3]);
  assert.deepEqual(ids(filterRows(rows, "high", "", 2)), [3], "the page view ignores the group");
  assert.deepEqual(ids(filterRows(rows, "all", "", 1)), [1, 2, 4]);
});

test("the Removed view can be narrowed to a page too", () => {
  assert.deepEqual(ids(filterRows(rows, "removed", "", 2)), [5]);
  assert.deepEqual(ids(filterRows(rows, "removed", "", 1)), [6]);
});

test("putting a row back returns it to the groups", () => {
  const back = rows.map((r) => (r.id === 5 ? { ...r, removed: false } : r));
  assert.deepEqual(rowCounts(back), { high: 1, medium: 1, low: 3, needsReview: 3, all: 5, removed: 1 });
  assert.ok(filterRows(back, "flagged", "", null).some((r) => r.id === 5));
});

test("nothing in the list at all", () => {
  assert.deepEqual(rowCounts([]), { high: 0, medium: 0, low: 0, needsReview: 0, all: 0, removed: 0 });
  assert.deepEqual(filterRows([], "all", "x", 3), []);
});
