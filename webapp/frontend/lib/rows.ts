// Which rows the review screen shows, and how many are in each group. Kept apart from the page so
// the rules can be tested.
//
// A removed row (a leftover fragment of a name that wrapped onto another line, a stray line) is
// hidden everywhere except the "Removed" view, where it can be put back. It never counts toward
// "Needs review", the confidence groups or "All": the work list is the rows that are still in.

export type Filter = "flagged" | "low" | "medium" | "high" | "all" | "removed";

export type Row = {
  confidence: string;
  reviewed: boolean;
  removed: boolean;
  name: string;
  page_id: number | null;
};

export function rowCounts(rows: Row[]) {
  const kept = rows.filter((r) => !r.removed);
  return {
    high: kept.filter((r) => r.confidence === "high").length,
    medium: kept.filter((r) => r.confidence === "medium").length,
    low: kept.filter((r) => r.confidence === "low").length,
    // "Needs review" is the actionable worklist: a row ticked "reviewed" is done and leaves it.
    needsReview: kept.filter((r) => r.confidence !== "high" && !r.reviewed).length,
    all: kept.length,
    removed: rows.length - kept.length,
  };
}

/** `pageFilter` is "show only the rows on this page", set from the page-number panel. */
export function filterRows<T extends Row>(rows: T[], filter: Filter, search: string, pageFilter: number | null): T[] {
  const q = search.trim().toLowerCase();
  const nameMatches = (r: Row) => !q || r.name.toLowerCase().includes(q);
  return rows.filter((r) => {
    if (filter === "removed") return r.removed && nameMatches(r) && (pageFilter === null || r.page_id === pageFilter);
    if (r.removed) return false;
    if (pageFilter !== null) return r.page_id === pageFilter && nameMatches(r);   // every row of that page, whatever the group
    if (filter === "flagged") return r.confidence !== "high" && !r.reviewed && nameMatches(r);
    if (filter !== "all" && r.confidence !== filter) return false;
    return nameMatches(r);
  });
}
