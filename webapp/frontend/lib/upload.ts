// Sending a booklet with live progress. fetch() cannot report how much of a request
// body has been sent; XMLHttpRequest can, which is the only reason this is not fetch.
//
// Two phases are shown to the person, because both take real time on a large booklet:
//   "sending" - the bytes are going from this computer to the server (a percentage).
//   "saving"  - everything has arrived and the server is storing the files and queueing
//               the job; it cannot report progress for this, so it is shown as "working".

export type UploadPhase = "sending" | "saving";

export type UploadProgress = {
  phase: UploadPhase;
  loaded: number;                 // bytes sent so far
  total: number;                  // bytes to send in all (0 if the browser could not say)
  fraction: number | null;        // 0..1, or null when the total is unknown
  bytesPerSecond: number | null;  // smoothed; null until there is enough to go on
  secondsLeft: number | null;     // null until the estimate is steady enough to show
};

export type Tracker = { startedAt: number; lastAt: number; lastLoaded: number; speed: number | null };

const SMOOTHING = 0.3;          // weight of the newest speed sample
const MIN_SAMPLE_MS = 250;      // ignore samples closer together than this
const MIN_ELAPSED_MS = 1500;    // show no estimate before this long
const MIN_FRACTION = 0.02;      // ... or before this much has been sent

export function newTracker(now: number): Tracker {
  return { startedAt: now, lastAt: now, lastLoaded: 0, speed: null };
}

/** Folds one progress event into the tracker and returns what to show. Pure: time is passed in. */
export function sample(t: Tracker, loaded: number, total: number, now: number): UploadProgress {
  const dt = now - t.lastAt;
  if (dt >= MIN_SAMPLE_MS && loaded >= t.lastLoaded) {
    const instant = ((loaded - t.lastLoaded) / dt) * 1000;
    t.speed = t.speed === null ? instant : SMOOTHING * instant + (1 - SMOOTHING) * t.speed;
    t.lastAt = now;
    t.lastLoaded = loaded;
  }
  const fraction = total > 0 ? Math.min(1, loaded / total) : null;
  const steady = fraction !== null && fraction >= MIN_FRACTION && now - t.startedAt >= MIN_ELAPSED_MS;
  const speed = t.speed !== null && t.speed > 0 ? t.speed : null;
  return {
    phase: "sending", loaded, total, fraction,
    bytesPerSecond: speed,
    secondsLeft: steady && speed !== null ? Math.max(0, (total - loaded) / speed) : null,
  };
}

export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "";
  if (n < 1024) return `${Math.round(n)} B`;
  const units = ["KB", "MB", "GB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2)} ${units[i]}`;
}

export function formatDuration(seconds: number): string {
  const s = Math.round(seconds);
  if (s < 5) return "a few seconds";
  if (s < 60) return `${s} seconds`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${s % 60 ? `${s % 60} s` : ""}`.trim();
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

export type SendResult = { status: number; text: string };

export class UploadCancelled extends Error {
  constructor() { super("Upload cancelled"); this.name = "UploadCancelled"; }
}

/**
 * POSTs `body` and reports progress. Resolves with the server's reply whatever its status
 * (the caller decides what a 400 means); rejects only if the request could not be made
 * (network down, timed out) or was cancelled through `signal`.
 */
export function sendWithProgress(opts: {
  url: string;
  body: FormData;
  headers?: Record<string, string>;
  onProgress: (p: UploadProgress) => void;
  signal?: AbortSignal;
  now?: () => number;
}): Promise<SendResult> {
  const now = opts.now ?? (() => Date.now());
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const tracker = newTracker(now());
    let sentAll = false;
    let lastTotal = 0;

    xhr.open("POST", opts.url);
    for (const [k, v] of Object.entries(opts.headers ?? {})) xhr.setRequestHeader(k, v);

    xhr.upload.onprogress = (e) => {
      if (sentAll) return;
      lastTotal = e.lengthComputable ? e.total : 0;
      opts.onProgress(sample(tracker, e.loaded, lastTotal, now()));
    };
    xhr.upload.onload = () => {
      sentAll = true;
      opts.onProgress({ phase: "saving", loaded: lastTotal, total: lastTotal, fraction: 1, bytesPerSecond: null, secondsLeft: null });
    };
    xhr.onload = () => resolve({ status: xhr.status, text: xhr.responseText });
    xhr.onerror = () => reject(new Error("The connection was lost. Check your internet and try again."));
    xhr.ontimeout = () => reject(new Error("The server took too long to answer. Try again."));
    xhr.onabort = () => reject(new UploadCancelled());

    if (opts.signal) {
      if (opts.signal.aborted) { reject(new UploadCancelled()); return; }
      opts.signal.addEventListener("abort", () => xhr.abort(), { once: true });
    }
    xhr.send(opts.body);
  });
}

/**
 * What the server's answer to "queue this booklet" means, in words a reviewer can act on.
 * (A 401 is handled by the caller, which signs the person out.)
 */
export function readJobReply(status: number, text: string): { id: number } {
  let body: any = null;
  try { body = JSON.parse(text); } catch { /* not JSON: for instance a proxy's error page */ }
  if (status === 413) throw new Error("The files are too large for the server to accept.");
  if (status < 200 || status >= 300) {
    const detail = typeof body?.detail === "string" ? body.detail : "";
    throw new Error(detail || `Upload failed (the server answered ${status}). Try again, and tell your admin if it keeps happening.`);
  }
  if (typeof body?.id !== "number") {
    throw new Error("The upload finished but the server's answer was not understood. Check the queue before uploading again.");
  }
  return body;
}
