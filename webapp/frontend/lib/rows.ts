// Which rows the review screen shows, how many are in each group, and how to find a name anywhere
// in a job. Kept apart from the page so the rules can be tested.
//
// A removed row (a leftover fragment of a name that wrapped onto another line, a stray line) is
// hidden everywhere except the "Removed" view, where it can be put back. It never counts toward
// "Needs review", the confidence groups, "Reviewed", "Edited" or "All": the work list is the rows
// that are still in. The whole-job search is the one place that does look at removed rows.

export type Filter = "flagged" | "low" | "medium" | "high" | "all" | "reviewed" | "edited" | "removed";

export type Row = {
  confidence: string;
  reviewed: boolean;
  removed: boolean;
  name: string;
  page_id: number | null;
  updated_by: number | null;                       // who last changed the row; null if nobody has
  original_fields: Record<string, string> | null;  // what extraction first produced
};

// Every field a person can change, except the page number: setting a page's number changes every
// row on that page at once, and would make the whole page look "edited".
const COMPARED_FIELDS = ["name", "faculty", "department", "course", "degree", "grade", "distinction_notes", "year",
  "year_note", "programme", "affiliated_institution", "country"];

/** Changed by a person from what extraction produced (a page number alone does not count). */
export function isEdited(row: Row): boolean {
  const original = row.original_fields;
  if (!original) return false;
  const now = row as unknown as Record<string, unknown>;
  return COMPARED_FIELDS.some((f) => String(original[f] ?? "").trim() !== String(now[f] ?? "").trim());
}

/** Ticked "reviewed" by a person. Rows the tool is confident about are created as reviewed, so this
 *  needs a person's mark on it: otherwise every high-confidence row would count. */
export function isPersonReviewed(row: Row): boolean {
  return row.reviewed && row.updated_by !== null;
}

/** Every word typed must appear in the name, in any order, ignoring capitals: "olufemi samuel"
 *  finds "ADEYEMI Samuel Olufemi". An empty query matches everything. */
export function nameMatches(name: string, query: string): boolean {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const lower = name.toLowerCase();
  return words.every((w) => lower.includes(w));
}

export function rowCounts(rows: Row[]) {
  const kept = rows.filter((r) => !r.removed);
  return {
    high: kept.filter((r) => r.confidence === "high").length,
    medium: kept.filter((r) => r.confidence === "medium").length,
    low: kept.filter((r) => r.confidence === "low").length,
    // "Needs review" is the actionable worklist: a row ticked "reviewed" is done and leaves it.
    needsReview: kept.filter((r) => r.confidence !== "high" && !r.reviewed).length,
    reviewed: kept.filter(isPersonReviewed).length,
    edited: kept.filter(isEdited).length,
    all: kept.length,
    removed: rows.length - kept.length,
  };
}

/** `pageFilter` is "show only the rows on this page", set from the page-number panel. */
export function filterRows<T extends Row>(rows: T[], filter: Filter, search: string, pageFilter: number | null): T[] {
  return rows.filter((r) => {
    if (!nameMatches(r.name, search)) return false;
    if (filter === "removed") return r.removed && (pageFilter === null || r.page_id === pageFilter);
    if (r.removed) return false;
    if (pageFilter !== null) return r.page_id === pageFilter;   // every row of that page, whatever the group
    if (filter === "flagged") return r.confidence !== "high" && !r.reviewed;
    if (filter === "reviewed") return isPersonReviewed(r);
    if (filter === "edited") return isEdited(r);
    return filter === "all" || r.confidence === filter;
  });
}

/** What to say about a row in the search results, beside its confidence. */
export function rowTags(row: Row): string[] {
  const tags: string[] = [];
  if (isPersonReviewed(row)) tags.push("Reviewed");
  if (isEdited(row)) tags.push("Edited");
  if (row.removed) tags.push("Removed");
  return tags;
}

/** The whole-job search: every row whose name matches, kept rows first (in the booklet's order),
 *  then removed ones. `total` is how many matched; `matches` is the first `limit` of them. */
export function searchRows<T extends Row>(rows: T[], query: string, limit = 30): { matches: T[]; total: number } {
  if (!query.trim()) return { matches: [], total: 0 };
  const found = rows.filter((r) => nameMatches(r.name, query));
  const ordered = [...found.filter((r) => !r.removed), ...found.filter((r) => r.removed)];
  return { matches: ordered.slice(0, limit), total: ordered.length };
}
