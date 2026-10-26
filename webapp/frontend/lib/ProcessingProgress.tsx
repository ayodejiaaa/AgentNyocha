"use client";

import type { Job } from "./api";

// A job being read: how far along it is and what it is doing right now.
export default function ProcessingProgress({ job }: { job: Job }) {
  if (job.status !== "processing") return null;
  const pct = Math.max(0, Math.min(99, job.progress ?? 0));
  return (
    <div className="job-progress">
      <div className="bar" role="progressbar" aria-label="Processing progress"
           aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
        <span style={{ width: `${pct}%` }} />
      </div>
      <div className="job-progress-text"><b>{pct}%</b>{job.progress_note ? ` - ${job.progress_note}` : ""}</div>
    </div>
  );
}
