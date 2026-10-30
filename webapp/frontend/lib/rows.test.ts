// Tests for which rows the review screen shows and for the whole-job search. Run with:  npm test
import assert from "node:assert/strict";
import { test } from "node:test";
import { filterRows, isEdited, isPersonReviewed, nameMatches, rowCounts, rowTags, searchRows, type Row } from "./rows";

const blank = { faculty: "", department: "", course: "", degree: "", grade: "", distinction_notes: "", year: "", year_note: "",
  programme: "", affiliated_institution: "", country: "", page: "12" };

// a row as the screen gets it: current fields, plus what extraction first produced
function row(o: Partial<Row> & { id: number; originalName?: string; page?: string; faculty?: string }) {
  const { originalName, ...rest } = o;
  const name = o.name ?? "ADEYEMI Olufemi";
  return {
    confidence: "low", reviewed: false, removed: false, page_id: 1, updated_by: null,
    ...blank, ...rest, name,
    original_fields: { ...blank, name: originalName ?? name, page: o.page ?? "12" } as Record<string, string>,
  };
}

const rows = [
  row({ id: 1, confidence: "high", name: "OKAFOR Chidi Emeka", reviewed: true }),                       // auto-reviewed by the tool
  row({ id: 2, confidence: "medium", name: "BELLO Aminat Zainab" }),
  row({ id: 3, confidence: "low", name: "Samuel", page_id: 2 }),                                          // a fragment
  row({ id: 4, confidence: "low", name: "ADEYEMI Olufemi Samuel", originalName: "ADEYEMI Olufemi", reviewed: true, updated_by: 7 }),
  row({ id: 5, confidence: "low", name: "Tunde", removed: true, page_id: 2, updated_by: 7 }),
  row({ id: 6, confidence: "medium", name: "Okonkwo Ngozi", removed: true }),
  row({ id: 7, confidence: "high", name: "ADEBAYO Samuel Tobi", reviewed: true }),
];
const ids = (list: { id: number }[]) => list.map((r) => r.id);

// ---- edited / reviewed ------------------------------------------------------------------------

test("a row is edited when a field differs from what extraction produced", () => {
  assert.equal(isEdited(rows[3]), true);
  assert.equal(isEdited(rows[0]), false);
  assert.equal(isEdited(row({ id: 9, faculty: "Science" })), true, "any field, not only the name");
});

test("changes that are not edits are not counted", () => {
  assert.equal(isEdited(row({ id: 9, name: "ADEYEMI Olufemi  ", originalName: "ADEYEMI Olufemi" })), false, "stray spaces");
  const pageOnly = { ...row({ id: 9 }), page: "13" };
  assert.equal(isEdited(pageOnly), false, "setting a page's number changes every row on it, so it is not an edit");
  assert.equal(isEdited({ ...row({ id: 9 }), original_fields: null }), false, "a job from before originals were kept");
});

test("reviewed means a person ticked it, not that the tool was sure", () => {
  assert.equal(isPersonReviewed(rows[0]), false, "a high-confidence row starts as reviewed, with nobody's mark on it");
  assert.equal(isPersonReviewed(rows[3]), true);
  assert.equal(isPersonReviewed(row({ id: 9, reviewed: false, updated_by: 7 })), false, "a person who un-ticked it");
});

// ---- counts and views -------------------------------------------------------------------------

test("counts leave removed rows out of every group, and count them on their own", () => {
  assert.deepEqual(rowCounts(rows), { high: 2, medium: 1, low: 2, needsReview: 2, reviewed: 1, edited: 1, all: 5, removed: 2 });
});

test("a removed row stops counting as reviewed or edited too", () => {
  const gone = row({ id: 1, reviewed: true, updated_by: 7, removed: true, name: "ADEYEMI Olufemi Samuel", originalName: "ADEYEMI Olufemi" });
  assert.deepEqual([isPersonReviewed(gone), isEdited(gone)], [true, true], "it was both before it was removed");
  const c = rowCounts([gone]);
  assert.deepEqual([c.reviewed, c.edited, c.all, c.removed], [0, 0, 0, 1]);
  assert.deepEqual(filterRows([gone], "reviewed", "", null), []);
  assert.deepEqual(filterRows([gone], "edited", "", null), []);
});

test("removing a flagged row takes it off the work list", () => {
  assert.deepEqual([rowCounts([row({ id: 1 })]).needsReview, rowCounts([row({ id: 1, removed: true })]).needsReview], [1, 0]);
});

test("removed rows are hidden in every view except Removed", () => {
  for (const f of ["flagged", "low", "medium", "high", "all", "reviewed", "edited"] as const) {
    assert.ok(!filterRows(rows, f, "", null).some((r) => r.removed), `${f} must not show a removed row`);
  }
  assert.deepEqual(ids(filterRows(rows, "removed", "", null)), [5, 6]);
});

test("each view shows its own rows", () => {
  assert.deepEqual(ids(filterRows(rows, "flagged", "", null)), [2, 3]);
  assert.deepEqual(ids(filterRows(rows, "low", "", null)), [3, 4]);
  assert.deepEqual(ids(filterRows(rows, "medium", "", null)), [2]);
  assert.deepEqual(ids(filterRows(rows, "high", "", null)), [1, 7]);
  assert.deepEqual(ids(filterRows(rows, "all", "", null)), [1, 2, 3, 4, 7]);
  assert.deepEqual(ids(filterRows(rows, "reviewed", "", null)), [4]);
  assert.deepEqual(ids(filterRows(rows, "edited", "", null)), [4]);
});

test("the name box narrows whichever view you are in, ignoring case and extra spaces", () => {
  assert.deepEqual(ids(filterRows(rows, "all", "  okafor ", null)), [1]);
  assert.deepEqual(ids(filterRows(rows, "removed", "TUNDE", null)), [5]);
  assert.deepEqual(ids(filterRows(rows, "flagged", "samuel", null)), [3]);
  assert.deepEqual(ids(filterRows(rows, "edited", "samuel", null)), [4]);
  assert.deepEqual(ids(filterRows(rows, "all", "nobody", null)), []);
});

test("showing one page shows every kept row on it, whatever the group, and not removed ones", () => {
  assert.deepEqual(ids(filterRows(rows, "all", "", 2)), [3]);
  assert.deepEqual(ids(filterRows(rows, "high", "", 2)), [3], "the page view ignores the group");
  assert.deepEqual(ids(filterRows(rows, "all", "", 1)), [1, 2, 4, 7]);
});

test("the Removed view can be narrowed to a page too", () => {
  assert.deepEqual(ids(filterRows(rows, "removed", "", 2)), [5]);
  assert.deepEqual(ids(filterRows(rows, "removed", "", 1)), [6]);
});

test("putting a row back returns it to the groups", () => {
  const back = rows.map((r) => (r.id === 5 ? { ...r, removed: false } : r));
  assert.equal(rowCounts(back).removed, 1);
  assert.ok(filterRows(back, "flagged", "", null).some((r) => r.id === 5));
});

test("nothing in the list at all", () => {
  assert.deepEqual(rowCounts([]), { high: 0, medium: 0, low: 0, needsReview: 0, reviewed: 0, edited: 0, all: 0, removed: 0 });
  assert.deepEqual(filterRows([], "all", "x", 3), []);
});

// ---- finding a name -------------------------------------------------------------------------

test("every word typed must be in the name, in any order", () => {
  assert.equal(nameMatches("ADEYEMI Samuel Olufemi", "olufemi samuel"), true);
  assert.equal(nameMatches("ADEYEMI Olufemi", "olufemi samuel"), false);
  assert.equal(nameMatches("ADEYEMI Olufemi", "  OLU  "), true, "part of a word is enough");
  assert.equal(nameMatches("ADEYEMI Olufemi", ""), true);
  assert.equal(nameMatches("ADEYEMI Olufemi", "   "), true);
});

test("the search finds a fragment's full name wherever it is: high, edited or reviewed rows", () => {
  const { matches, total } = searchRows(rows, "samuel");
  assert.equal(total, 3);
  assert.deepEqual(ids(matches), [3, 4, 7], "the fragment, the edited and reviewed row, and a high-confidence row");
});

test("the search also looks at removed rows, listing them last", () => {
  const { matches } = searchRows(rows, "tunde");
  assert.deepEqual(ids(matches), [5]);
  const all = searchRows([...rows, row({ id: 8, name: "Tunde Bakare" })], "tunde");
  assert.deepEqual(ids(all.matches), [8, 5], "the removed one after the kept one");
});

test("the search is empty until something is typed", () => {
  assert.deepEqual(searchRows(rows, ""), { matches: [], total: 0 });
  assert.deepEqual(searchRows(rows, "   "), { matches: [], total: 0 });
});

test("a long list of matches is cut at the limit but still counted", () => {
  const many = Array.from({ length: 50 }, (_, i) => row({ id: 100 + i, name: `ADEYEMI Samuel ${i}` }));
  const { matches, total } = searchRows(many, "samuel", 30);
  assert.deepEqual([matches.length, total], [30, 50]);
  assert.equal(matches[0].id, 100, "in the booklet's order");
});

test("each result is tagged with what a reviewer needs to know about it", () => {
  assert.deepEqual(rowTags(rows[3]), ["Reviewed", "Edited"]);
  assert.deepEqual(rowTags(rows[4]), ["Removed"]);
  assert.deepEqual(rowTags(rows[0]), [], "a high-confidence row the tool marked reviewed is not tagged");
  assert.deepEqual(rowTags(rows[2]), []);
});
