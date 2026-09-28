"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import Topbar from "@/lib/Topbar";
import { createJob, getToken, listJobs, type Job } from "@/lib/api";

function StatusPill({ job }: { job: Job }) {
  return <span className={`status-pill ${job.status}`}>{job.status}</span>;
}

function Counts({ job }: { job: Job }) {
  if (job.status !== "ready") return null;
  return (
    <span className="counts">
      {job.record_count} records
      {job.medium_count > 0 && <> &middot; <b className="medium">{job.medium_count} medium</b></>}
      {job.low_count > 0 && <> &middot; <b className="low">{job.low_count} low</b></>}
    </span>
  );
}

export default function JobsPage() {
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [university, setUniversity] = useState("");
  const [year, setYear] = useState("");
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);

  async function refresh() {
    try {
      setJobs(await listJobs());
    } catch (e: any) {
      setError(e.message || "Failed to load");
    }
  }

  useEffect(() => {
    if (!getToken()) {
      window.location.href = "/login";
      return;
    }
    refresh();
    // Jobs move through queued -> processing -> ready in the background
    // (the worker, a separate process); poll rather than push at this
    // scale (15 users, a handful of jobs in flight at once) - simplest
    // thing that keeps the queue looking live without a websocket.
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, []);

  async function onUpload(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const files = fileInput.current?.files;
    if (!files || files.length === 0) {
      setError("Choose at least one PDF");
      return;
    }
    setUploading(true);
    try {
      await createJob(university, year, files);
      setUniversity("");
      setYear("");
      if (fileInput.current) fileInput.current.value = "";
      await refresh();
    } catch (err: any) {
      setError(err.message || "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  return (
    <div>
      <Topbar />
      <div className="container">
        <div className="card">
          <h2 style={{ marginTop: 0, fontSize: 16 }}>Upload a booklet</h2>
          <p style={{ fontSize: 13, color: "#666", marginTop: -6 }}>
            One or more PDFs from the same booklet (a split scan) - they&rsquo;ll be stitched together
            in the order you select them.
          </p>
          <form onSubmit={onUpload}>
            <div style={{ display: "flex", gap: 14 }}>
              <div className="field" style={{ flex: 1 }}>
                <label>University</label>
                <input value={university} onChange={(e) => setUniversity(e.target.value)} placeholder="e.g. LASU" required />
              </div>
              <div className="field" style={{ flex: 1 }}>
                <label>Year</label>
                <input value={year} onChange={(e) => setYear(e.target.value)} placeholder="e.g. 2026" required />
              </div>
            </div>
            <div className="field">
              <label>PDF file(s)</label>
              <input type="file" accept="application/pdf" multiple ref={fileInput} required />
            </div>
            <button className="btn" type="submit" disabled={uploading}>
              {uploading ? "Uploading..." : "Upload & queue for extraction"}
            </button>
            {error && <div className="error">{error}</div>}
          </form>
        </div>

        <div className="card">
          <h2 style={{ marginTop: 0, fontSize: 16 }}>Queue</h2>
          {jobs === null ? (
            <p>Loading...</p>
          ) : jobs.length === 0 ? (
            <p style={{ color: "#888" }}>No jobs yet - upload a booklet above.</p>
          ) : (
            <table className="jobs">
              <thead>
                <tr>
                  <th>Booklet</th><th>Status</th><th>Uploaded by</th><th>Uploaded</th><th></th>
                </tr>
              </thead>
              <tbody>
                {jobs.map((j) => (
                  <tr key={j.id}>
                    <td>
                      <Link className="job-link" href={`/jobs/${j.id}`}>{j.university} {j.year}</Link>
                      <div style={{ fontSize: 11, color: "#999" }}>{j.filenames.join(", ")}</div>
                    </td>
                    <td>
                      <StatusPill job={j} />
                      {j.status === "failed" && (
                        <div style={{ fontSize: 11, color: "#b02a2a", marginTop: 4, maxWidth: 260 }}>
                          {j.error_message?.split("\n")[0]}
                        </div>
                      )}
                    </td>
                    <td>{j.uploaded_by_name}</td>
                    <td>{new Date(j.uploaded_at).toLocaleString()}</td>
                    <td><Counts job={j} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
