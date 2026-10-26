"use client";

import { formatBytes, formatDuration, type UploadProgress } from "./upload";

// Shown while a booklet is going to the server. `progress` is null for the instant between
// pressing the button and the browser's first report.
export default function UploadStatus({ progress, fileCount, onCancel }: {
  progress: UploadProgress | null;
  fileCount: number;
  onCancel: () => void;
}) {
  const saving = progress?.phase === "saving";
  const pct = progress?.fraction != null ? Math.floor(progress.fraction * 100) : null;
  const working = progress === null || saving || pct === null; // no percentage to show: animate instead

  let headline: string;
  if (progress === null) headline = "Starting the upload...";
  else if (saving) headline = "All files sent. Saving them and adding the booklet to the queue...";
  else headline = `Uploading ${fileCount} file${fileCount === 1 ? "" : "s"}${pct !== null ? ` - ${pct}%` : ""}`;

  const details: string[] = [];
  if (progress && !saving && progress.total > 0) details.push(`${formatBytes(progress.loaded)} of ${formatBytes(progress.total)}`);
  if (progress && !saving && progress.bytesPerSecond) details.push(`${formatBytes(progress.bytesPerSecond)}/s`);
  if (progress && !saving && progress.secondsLeft !== null) details.push(`about ${formatDuration(progress.secondsLeft)} left`);

  return (
    <div className="upload-status" role="status" aria-live="polite">
      <div className="upload-head">
        <span>{headline}</span>
        {!saving && <button type="button" className="link-btn" style={{ marginBottom: 0 }} onClick={onCancel}>Cancel upload</button>}
      </div>
      <div
        className={`bar${working ? " working" : ""}`}
        role="progressbar"
        aria-label="Upload progress"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={working ? undefined : (pct ?? 0)}
      >
        <span style={working ? undefined : { width: `${pct ?? 0}%` }} />
      </div>
      <div className="upload-detail">
        {saving
          ? "This can take a minute for a large booklet. Keep this page open until it finishes."
          : details.length > 0 ? details.join("  ·  ") : "Keep this page open until the upload finishes."}
      </div>
    </div>
  );
}
