"use client";

import { useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import Topbar from "@/lib/Topbar";
import { exportUrl, fileUrl, getJob, getToken, patchRecord, type Job, type ReviewRecord } from "@/lib/api";

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

type Filter = "flagged" | "low" | "medium" | "high" | "all";

function Row({ rec, onSaved, onOpenImage }: {
  rec: ReviewRecord;
  onSaved: (id: number, patch: Partial<ReviewRecord>) => void;
  onOpenImage: (src: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
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

  const pageIssue = rec.flag_reason === "page_number_uncertain";

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
            Couldn&rsquo;t confirm the printed page number for this row - check it against the snapshot and correct
            it above. This is what Akowe Verify uses to pull the right page into a report.
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
        {rec.review_image ? (
          <img loading="lazy" src={fileUrl(rec.review_image)} onClick={() => onOpenImage(fileUrl(rec.review_image!))} />
        ) : (
          <div className="none">no snapshot</div>
        )}
      </div>
    </div>
  );
}

export default function ReviewPage() {
  const params = useParams();
  const jobId = Number(params.id);
  const [job, setJob] = useState<Job | null>(null);
  const [records, setRecords] = useState<ReviewRecord[]>([]);
  const [filter, setFilter] = useState<Filter>("flagged");
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
  }, [jobId]);

  function onSaved(id: number, patch: Partial<ReviewRecord>) {
    setRecords((prev) => prev.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  }

  const counts = useMemo(() => ({
    high: records.filter((r) => r.confidence === "high").length,
    medium: records.filter((r) => r.confidence === "medium").length,
    low: records.filter((r) => r.confidence === "low").length,
    // "Needs review" is the actionable worklist, not just a confidence
    // slice - a row a DES has ticked "reviewed" (whether or not they
    // changed it) is done, and should visibly leave the list rather than
    // sit there forever at the same count. The Low/Medium/High/All views
    // stay complete regardless - those are for browsing everything, not
    // tracking what's left to do.
    needsReview: records.filter((r) => r.confidence !== "high" && !r.reviewed).length,
  }), [records]);

  const visible = useMemo(() => {
    return records.filter((r) => {
      if (filter === "flagged") return r.confidence !== "high" && !r.reviewed;
      if (filter !== "all" && r.confidence !== filter) return false;
      if (search && !r.name.toLowerCase().includes(search.toLowerCase())) return false;
      return true;
    });
  }, [records, filter, search]);

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
    { key: "all", label: "All", count: records.length },
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
          <a className="btn secondary" style={{ marginLeft: "auto" }} href={exportUrl(jobId)}>
            Export clean copy
          </a>
        </div>

        <div className="filters" style={{ marginBottom: 12 }}>
          {filters.map((f) => (
            <button key={f.key} className={filter === f.key ? "active" : ""} onClick={() => setFilter(f.key)}>
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
