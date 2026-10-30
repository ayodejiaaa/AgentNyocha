"use client";

import { useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import Topbar from "@/lib/Topbar";
import {
  exportUrl, fileUrl, getJob, getPageImageStatus, getToken, pageImagesUrl, patchRecord, setPageNumber,
  type Job, type PageImageStatus, type PageProblem, type ReviewRecord,
} from "@/lib/api";
import { filterRows, rowCounts, rowTags, searchRows, type Filter } from "@/lib/rows";

const EDIT_FIELDS: { key: keyof ReviewRecord; label: string }[] = [
  { key: "faculty", label: "Faculty" },
  { key: "department", label: "Department" },
  { key: "course", label: "Course" },
  { key: "degree", label: "Degree" },
  { key: "grade", label: "Grade" },
  { key: "distinction_notes", label: "Distinction notes" },
  { key: "year", label: "Year" },
  { key: "year_note", label: "Year note" },
  { key: "programme", label: "Programme" },
  { key: "affiliated_institution", label: "Affiliated institution" },
  { key: "country", label: "Country" },
];

function Row({ rec, onSaved, onOpenImage }: {
  rec: ReviewRecord;
  onSaved: (id: number, patch: Partial<ReviewRecord>) => void;
  onOpenImage: (src: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const pageIssue = rec.flag_reason === "page_number_uncertain";
  // When the page number is the thing being asked about, its image is shown straight
  // away; otherwise it's one click away, for checking any row's number.
  const [showNumber, setShowNumber] = useState(pageIssue);
  // "saving" while the request is in flight, then "saved" for a couple of
  // seconds so there's an actual confirmation to see - a status that only
  // ever flickered to "saving..." and vanished (the first version of this)
  // gave no lasting sign the edit actually took, which is exactly the kind
  // of thing worth catching before a DES is the one wondering about it.
  const [status, setStatus] = useState<{ field: string; state: "saving" | "saved" } | null>(null);

  async function save(field: keyof ReviewRecord, value: string | boolean) {
    if (rec[field] === value) return;
    setStatus({ field: String(field), state: "saving" });
    try {
      await patchRecord(rec.id, { [field]: value } as Partial<ReviewRecord>);
      onSaved(rec.id, { [field]: value } as Partial<ReviewRecord>);
      setStatus({ field: String(field), state: "saved" });
      setTimeout(() => setStatus((s) => (s?.field === String(field) ? null : s)), 2000);
    } catch {
      setStatus(null);
    }
  }

  const numberBlock = rec.pageno_image ? (
    showNumber ? (
      <div className="snap-number">
        <div className="snap-label">Where the page number is printed &mdash; bottom and top edge of this page</div>
        <img loading="lazy" src={fileUrl(rec.pageno_image)} onClick={() => onOpenImage(fileUrl(rec.pageno_image!))} />
      </div>
    ) : (
      <button type="button" className="link-btn" onClick={() => setShowNumber(true)}>
        Show the page number
      </button>
    )
  ) : pageIssue ? (
    <div className="none">
      No page-number image for this row &mdash; this booklet was processed before page images were kept.
      Upload it again to get them.
    </div>
  ) : null;

  if (rec.removed) {
    return (
      <div className="review-row removed-row">
        <div className="idx">#{rec.row_index}</div>
        <div className="fields">
          <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
            <span style={{ textDecoration: "line-through", color: "#888", fontWeight: 600 }}>{rec.name}</span>
            <span className="reason">Removed: not in the export</span>
            <button type="button" className="btn secondary" onClick={() => save("removed", false)}>Put it back</button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="review-row">
      <div className="idx">#{rec.row_index}</div>
      <div className="fields">
        <span className={`badge ${rec.confidence}`}>{rec.confidence}</span>
        {rec.flag_reason && <span className="reason">{rec.flag_reason}</span>}
        <div style={{ display: "flex", gap: 8, marginTop: 4 }}>
          <input
            defaultValue={rec.name}
            onBlur={(e) => save("name", e.target.value)}
            style={{ fontWeight: 600, fontSize: 14, flex: 1 }}
          />
          <div style={{ flex: "0 0 90px" }}>
            <input
              defaultValue={rec.page}
              onBlur={(e) => save("page", e.target.value)}
              placeholder="Page"
              title="Printed page number - Akowe's report assembly matches a client's pages by this exact number"
              style={pageIssue ? { borderColor: "#b02a2a", background: "#fff5f5", fontWeight: 600 } : undefined}
            />
          </div>
        </div>
        {pageIssue && (
          <div style={{ fontSize: 11, color: "#b02a2a", marginTop: 2 }}>
            Couldn&rsquo;t confirm the printed page number for this row - check it against the page-number image
            on the right and correct it above. This is what Akowe Verify uses to pull the right page into a report.
          </div>
        )}
        <div className="save-status">
          {status?.state === "saving" && <span>Saving {status.field}...</span>}
          {status?.state === "saved" && <span className="ok">&#10003; Saved</span>}
          {!status && <span className="hint">Changes save automatically when you click away from a field.</span>}
        </div>

        <label className="reviewed-toggle">
          <input type="checkbox" defaultChecked={rec.reviewed} onChange={(e) => save("reviewed", e.target.checked)} />
          <span>
            <b>Mark as reviewed</b>
            <br />
            <span className="reviewed-hint">
              Tick this once you&rsquo;ve checked this row against the snapshot - whether or not you changed
              anything. It just tracks your progress through the queue; it doesn&rsquo;t affect the export.
            </span>
          </span>
        </label>

        <button type="button" className="link" style={{ fontSize: 11, color: "#888", background: "none", border: "none", cursor: "pointer", padding: 0, marginTop: 8 }}
                onClick={() => setExpanded((v) => !v)}>
          {expanded ? "Hide other fields" : "Edit other fields (faculty, degree, grade, ...)"}
        </button>
        <button type="button" className="link-btn remove-btn"
                title="Takes this row out of the export. For a leftover piece of a name that is already on another row. You can put it back."
                onClick={() => save("removed", true)}>
          Remove this row
        </button>
        {expanded && (
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0 10px" }}>
            {EDIT_FIELDS.map(({ key, label }) => (
              <div key={String(key)}>
                <label>{label}</label>
                <input defaultValue={String(rec[key] ?? "")} onBlur={(e) => save(key, e.target.value)} />
              </div>
            ))}
          </div>
        )}
      </div>
      <div className="snap">
        {pageIssue && numberBlock}
        {rec.review_image ? (
          <img loading="lazy" src={fileUrl(rec.review_image)} onClick={() => onOpenImage(fileUrl(rec.review_image!))} />
        ) : (
          <div className="none">no snapshot</div>
        )}
        {!pageIssue && numberBlock}
      </div>
    </div>
  );
}

function ProblemPage({ problem, onOpenImage, onSet, onShowRows }: {
  problem: PageProblem;
  onOpenImage: (src: string) => void;
  onSet: (pageId: number, page: string) => void;
  onShowRows: () => void;
}) {
  // The booklet's sequence may point to the number; it is only put in the box,
  // and counts for nothing until the reviewer has looked at the image and pressed Set.
  const [value, setValue] = useState(problem.suggested || "");
  const [state, setState] = useState<{ kind: "idle" | "saving" | "error"; message?: string }>({ kind: "idle" });

  async function apply() {
    setState({ kind: "saving" });
    try {
      const res = await setPageNumber(problem.page_id, value);
      setState({ kind: "idle" });
      onSet(problem.page_id, res.page);
    } catch (e) {
      setState({ kind: "error", message: e instanceof Error ? e.message : "Could not set the page number" });
    }
  }

  return (
    <div className="problem-page">
      <div className="problem-text">
        <div style={{ fontWeight: 600 }}>{problem.where}</div>
        <div style={{ fontSize: 12, color: "#b02a2a", margin: "2px 0 8px" }}>{problem.detail}</div>
        {problem.suggested && (
          <div style={{ fontSize: 12, color: "#666", marginBottom: 6 }}>
            Suggested: <b>{problem.suggested}</b> - filled in below, not saved until you press Set.
          </div>
        )}
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && value.trim()) apply(); }}
            placeholder={problem.label ? `now ${problem.label}` : "Page no."}
            inputMode="numeric"
            style={{ width: 90, padding: "5px 7px", border: "1px solid #ccc", borderRadius: 4, fontSize: 13 }}
            aria-label={`Page number for ${problem.where}`}
          />
          <button type="button" className="btn" disabled={!value.trim() || state.kind === "saving"} onClick={apply}>
            {state.kind === "saving" ? "Saving..." : `Set for all ${problem.records} rows`}
          </button>
          <button type="button" className="link-btn" style={{ marginBottom: 0 }} onClick={onShowRows}>Show these rows</button>
        </div>
        {state.kind === "error" && <div style={{ fontSize: 12, color: "#b02a2a", marginTop: 4 }}>{state.message}</div>}
      </div>
      <div className="problem-image">
        {problem.number_image ? (
          <img loading="lazy" src={fileUrl(problem.number_image)} onClick={() => onOpenImage(fileUrl(problem.number_image!))} />
        ) : (
          <div className="none">no image</div>
        )}
      </div>
    </div>
  );
}

export default function ReviewPage() {
  const params = useParams();
  const jobId = Number(params.id);
  const [job, setJob] = useState<Job | null>(null);
  const [pageStatus, setPageStatus] = useState<PageImageStatus | null>(null);
  const [showProblems, setShowProblems] = useState(false);
  const [pageFilter, setPageFilter] = useState<number | null>(null); // show only the rows on this page
  const [records, setRecords] = useState<ReviewRecord[]>([]);
  const [filter, setFilter] = useState<Filter>("flagged");
  const [lastRemoved, setLastRemoved] = useState<number | null>(null);
  const [find, setFind] = useState("");                       // the whole-job search
  const [focusId, setFocusId] = useState<number | null>(null);  // one row opened from the search
  const [search, setSearch] = useState("");
  const [lightbox, setLightbox] = useState<string | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!getToken()) {
      window.location.href = "/login";
      return;
    }
    getJob(jobId)
      .then(({ job, records }) => { setJob(job); setRecords(records); })
      .catch((e) => setError(e.message || "Failed to load"));
    refreshPageStatus();
  }, [jobId]);

  function refreshPageStatus() {
    getPageImageStatus(jobId).then(setPageStatus).catch(() => setPageStatus(null));
  }

  function onSaved(id: number, patch: Partial<ReviewRecord>) {
    setRecords((prev) => prev.map((r) => (r.id === id ? { ...r, ...patch } : r)));
    if ("page" in patch) refreshPageStatus(); // a single row's page value can create or clear a problem
    if ("removed" in patch) {
      refreshPageStatus();                     // taking a row out (or back) can clear or create a page-number problem
      setLastRemoved(patch.removed ? id : null);
    }
  }

  // Undo for the row just removed. The notice goes away by itself after a while.
  function undoRemove(id: number) {
    patchRecord(id, { removed: false }).then(() => onSaved(id, { removed: false })).catch(() => {});
  }
  useEffect(() => {
    if (lastRemoved === null) return;
    const t = setTimeout(() => setLastRemoved(null), 15000);
    return () => clearTimeout(t);
  }, [lastRemoved]);

  // A page-level number was set on the server for every row of the page - mirror it here.
  function onPageSet(pageId: number, page: string) {
    setRecords((prev) => prev.map((r) => (r.page_id === pageId ? { ...r, page } : r)));
    refreshPageStatus();
  }

  const counts = useMemo(() => rowCounts(records), [records]);
  const found = useMemo(() => searchRows(records, find), [records, find]);
  const visible = useMemo(
    () => (focusId !== null ? records.filter((r) => r.id === focusId) : filterRows(records, filter, search, pageFilter)),
    [records, filter, search, pageFilter, focusId]);

  // Show one row from the search results, where it can be read against its snapshot and edited.
  function openRow(id: number) {
    setFocusId(id);
    setPageFilter(null);
    setTimeout(() => document.getElementById("rows-start")?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
  }

  if (error) {
    return (
      <div><Topbar /><div className="container"><div className="card error">{error}</div></div></div>
    );
  }
  if (!job) {
    return <div><Topbar /><div className="container">Loading...</div></div>;
  }

  const filters: { key: Filter; label: string; count: number }[] = [
    { key: "flagged", label: "Needs review", count: counts.needsReview },
    { key: "low", label: "Low", count: counts.low },
    { key: "medium", label: "Medium", count: counts.medium },
    { key: "high", label: "High", count: counts.high },
    { key: "all", label: "All", count: counts.all },
    { key: "reviewed", label: "Reviewed", count: counts.reviewed },
    { key: "edited", label: "Edited", count: counts.edited },
    ...(counts.removed > 0 ? [{ key: "removed" as Filter, label: "Removed", count: counts.removed }] : []),
  ];

  return (
    <div>
      <Topbar crumb={`${job.university} ${job.year}`} />
      <div className="container">
        <div className="card" style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <div>
            <div style={{ fontWeight: 700 }}>{job.university} {job.year}</div>
            <div style={{ fontSize: 12, color: "#888" }}>{job.filenames.join(", ")}</div>
          </div>
          <div style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", justifyContent: "flex-end" }}>
            {pageStatus?.available && (pageStatus.ready ? (
              <a className="btn secondary" href={pageImagesUrl(jobId)}
                 title="One PNG per page, named by its printed page number, with a manifest.csv">
                Download page images ({pageStatus.pages})
              </a>
            ) : (
              <>
                <span className="btn secondary disabled" aria-disabled="true"
                      title="Every page needs one confirmed page number first">Download page images</span>
                <button type="button" className="link-btn" style={{ marginBottom: 0, color: "#b02a2a" }}
                        onClick={() => setShowProblems((v) => !v)}>
                  {pageStatus.problems.length === 0
                    ? "No pages to download"
                    : `${pageStatus.problems.length} page${pageStatus.problems.length === 1 ? "" : "s"} need a page number first - ${showProblems ? "hide" : "show which"}`}
                </button>
              </>
            ))}
            <a className="btn secondary" href={exportUrl(jobId)}>
              Export clean copy
            </a>
          </div>
        </div>

        {pageStatus?.available && !pageStatus.ready && showProblems && pageStatus.problems.length > 0 && (
          <div className="card">
            <div style={{ fontWeight: 700, marginBottom: 4 }}>Pages that need a page number</div>
            <div style={{ fontSize: 12, color: "#666", marginBottom: 10 }}>
              The page images are named by page number, so each page needs exactly one. Check the number against the
              image (the bottom and top edge of the page), type it, and set it for the whole page - you don&rsquo;t
              need to edit the rows. The download unlocks when every page here is done.
            </div>
            {pageStatus.problems.map((p) => (
              <ProblemPage key={p.page_id} problem={p} onOpenImage={setLightbox} onSet={onPageSet}
                           onShowRows={() => { setPageFilter(p.page_id); setFilter("all"); setFocusId(null); window.scrollTo({ top: 0 }); }} />
            ))}
          </div>
        )}

        {pageFilter !== null && (
          <div className="card" style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span style={{ fontSize: 13 }}>Showing only the {visible.length} rows on one page.</span>
            <button type="button" className="btn secondary" onClick={() => setPageFilter(null)}>Show all rows</button>
          </div>
        )}

        <div className="card find-card">
          <label className="find-label" htmlFor="find-anywhere">Find a name anywhere in this job</label>
          <input id="find-anywhere" className="find-input" value={find} onChange={(e) => setFind(e.target.value)}
                 placeholder="e.g. Samuel. Searches every row: high confidence, reviewed, edited and removed." autoComplete="off" />
          {find.trim() && (
            <div className="find-results">
              <div className="find-count">
                {found.total === 0 ? "No row in this job has that name."
                  : `${found.total} row${found.total === 1 ? "" : "s"} match${found.total === 1 ? "es" : ""}`
                    + (found.total > found.matches.length ? ` - showing the first ${found.matches.length}; type more of the name to narrow it` : "")}
              </div>
              {found.matches.map((r) => (
                <div className="find-row" key={r.id}>
                  <span className="find-idx">#{r.row_index}</span>
                  <span className="find-name">{r.name}</span>
                  <span className="find-page">{r.page ? `page ${r.page}` : ""}</span>
                  <span className={`badge ${r.confidence}`}>{r.confidence}</span>
                  {rowTags(r).map((t) => <span key={t} className={`tag tag-${t.toLowerCase()}`}>{t}</span>)}
                  <button type="button" className="link-btn find-open" onClick={() => openRow(r.id)}>Open row</button>
                </div>
              ))}
            </div>
          )}
        </div>

        {focusId !== null && (
          <div className="card" style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span style={{ fontSize: 13 }}>Showing one row, opened from the search.</span>
            <button type="button" className="btn secondary" onClick={() => setFocusId(null)}>Show all rows</button>
          </div>
        )}

        <div className="filters" style={{ marginBottom: 12 }}>
          {filters.map((f) => (
            <button key={f.key} className={filter === f.key ? "active" : ""} onClick={() => { setFilter(f.key); setFocusId(null); }}>
              {f.label} ({f.count})
            </button>
          ))}
          <input
            placeholder="Filter by name..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{ float: "right", padding: "6px 10px", border: "1px solid #ccc", borderRadius: 6, width: 220 }}
          />
        </div>

        <div id="rows-start" />
        {lastRemoved !== null && (
          <div className="card removed-notice" role="status">
            Row #{records.find((r) => r.id === lastRemoved)?.row_index} is out of the export.{" "}
            <button type="button" className="link-btn" style={{ marginBottom: 0, fontSize: 13 }} onClick={() => undoRemove(lastRemoved)}>Undo</button>
          </div>
        )}

        {visible.length === 0 ? (
          <div className="card" style={{ textAlign: "center", color: "#888" }}>No rows match.</div>
        ) : (
          visible.map((rec) => (
            <Row key={rec.id} rec={rec} onSaved={onSaved} onOpenImage={setLightbox} />
          ))
        )}
      </div>

      <div id="lightbox" className={lightbox ? "open" : ""} onClick={() => setLightbox(null)}>
        {lightbox && <img src={lightbox} alt="" />}
      </div>
    </div>
  );
}
