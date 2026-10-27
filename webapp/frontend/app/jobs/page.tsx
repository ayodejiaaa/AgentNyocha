"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import Topbar from "@/lib/Topbar";
import ProcessingProgress from "@/lib/ProcessingProgress";
import UploadStatus from "@/lib/UploadStatus";
import { createJob, getToken, listJobs, type Job } from "@/lib/api";
import { UploadCancelled, type UploadProgress } from "@/lib/upload";

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
  const [progress, setProgress] = useState<UploadProgress | null>(null);
  const [sending, setSending] = useState(0);          // how many files are in the upload under way
  const [done, setDone] = useState("");               // confirmation of the last upload that finished
  const [error, setError] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);
  const abort = useRef<AbortController | null>(null);

  // Closing the tab or leaving mid-upload would silently lose it: ask first.
  useEffect(() => {
    if (!uploading) return;
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [uploading]);

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
    setProgress(null);
    setSending(files.length);
    setDone("");
    abort.current = new AbortController();
    try {
      await createJob(university, year, files, { onProgress: setProgress, signal: abort.current.signal });
      setDone(`Uploaded. ${university} ${year} is in the queue below and will start processing shortly.`);
      setUniversity("");
      setYear("");
      if (fileInput.current) fileInput.current.value = "";
      await refresh();
    } catch (err: any) {
      if (err instanceof UploadCancelled) setError("Upload cancelled. Nothing was added to the queue.");
      else setError(err.message || "Upload failed");
    } finally {
      setUploading(false);
      setProgress(null);
      abort.current = null;
    }
  }

  return (
    <div>
      <Topbar />
      <div className="container">
        <div className="card">
          <h2 style={{ marginTop: 0, fontSize: 16 }}>Upload a booklet</h2>
          <p style={{ fontSize: 13, color: "#666", marginTop: -6 }}>
            One or more PDFs from the same booklet (a split scan). Everything you select here becomes one
            job. Parts are joined by the page marker in each file name (for example &ldquo;PGS 1 - 120&rdquo;
            or &ldquo;pg 121&rdquo;), not the order you select them; a file with no marker goes first.
          </p>
          <p style={{ fontSize: 13, color: "#a15c00", marginTop: -6 }}>
            Keep everything in one upload under 100 MB in total. A bigger upload is refused and stops
            early with &ldquo;The connection was lost&rdquo;.
          </p>
          <form onSubmit={onUpload}>
            <div style={{ display: "flex", gap: 14 }}>
              <div className="field" style={{ flex: 1 }}>
                <label>University</label>
                <input value={university} onChange={(e) => setUniversity(e.target.value)} placeholder="e.g. LASU" required disabled={uploading} />
              </div>
              <div className="field" style={{ flex: 1 }}>
                <label>Year</label>
                <input value={year} onChange={(e) => setYear(e.target.value)} placeholder="e.g. 2026" required disabled={uploading} />
              </div>
            </div>
            <div className="field">
              <label>PDF file(s)</label>
              <input type="file" accept="application/pdf" multiple ref={fileInput} required disabled={uploading} />
            </div>
            <button className="btn" type="submit" disabled={uploading}>
              {uploading ? "Uploading..." : "Upload & queue for extraction"}
            </button>
            {uploading && <UploadStatus progress={progress} fileCount={sending} onCancel={() => abort.current?.abort()} />}
            {done && !uploading && <div className="upload-done" role="status">{done}</div>}
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
                      <ProcessingProgress job={j} />
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
