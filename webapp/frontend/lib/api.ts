// Thin client for the API in webapp/api - one place that knows the base
// URL and how the token is stored, so every page just calls these.

import { readJobReply, sendWithProgress, type UploadProgress } from "./upload";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";
const TOKEN_KEY = "akowe_token";
const NAME_KEY = "akowe_full_name";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function getFullName(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(NAME_KEY) || "";
}

export function logout() {
  window.localStorage.removeItem(TOKEN_KEY);
  window.localStorage.removeItem(NAME_KEY);
  window.location.href = "/login";
}

export async function login(email: string, password: string): Promise<void> {
  const res = await fetch(`${API_URL}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) throw new Error("Wrong email or password");
  const data = await res.json();
  window.localStorage.setItem(TOKEN_KEY, data.token);
  window.localStorage.setItem(NAME_KEY, data.full_name || "");
}

async function authFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(`${API_URL}${path}`, { ...init, headers });
  if (res.status === 401) {
    logout();
    throw new Error("Session expired - please log in again");
  }
  return res;
}

export type Job = {
  id: number;
  university: string;
  year: string;
  filenames: string[];
  status: "queued" | "processing" | "ready" | "failed";
  error_message: string | null;
  progress: number;                // 0-100 while "processing"
  progress_note: string | null;    // what it is doing now, e.g. "Reading page 12 of 80"
  uploaded_by_name: string;
  uploaded_at: string;
  record_count: number;
  high_count: number;
  medium_count: number;
  low_count: number;
};

export type ReviewRecord = {
  id: number;
  job_id: number;
  row_index: number;
  name: string;
  faculty: string;
  department: string;
  course: string;
  degree: string;
  grade: string;
  distinction_notes: string;
  year: string;
  year_note: string;
  page: string;
  programme: string;
  affiliated_institution: string;
  country: string;
  original_fields: Record<string, string>;
  confidence: "high" | "medium" | "low";
  flag_reason: string;
  review_image: string | null;
  page_id: number | null; // which physical page of the booklet this row is on
  // The top and bottom edges of this record's page - where the printed page
  // number is - for checking an uncertain page number against. null for a job
  // processed before these were kept.
  pageno_image: string | null;
  reviewed: boolean;
};

// A page that is holding up the page-image download.
export type PageProblem = {
  page_id: number;
  kind: "unresolved" | "duplicate" | "conflict";
  where: string;        // "page 3 of booklet.pdf"
  label: string;        // the number its rows currently carry, "" if none
  records: number;
  detail: string;       // what is wrong, in a sentence
  number_image: string | null; // the page's top and bottom edges, to check the number against
  note: "" | "two_numbers" | "neighbour_number"; // set when the scan shows another page's number too
  seen: { side: "left" | "right"; value: number }[]; // the numbers read in the page's corners
  suggested: string;    // the number the booklet's sequence points to; prefilled, never applied
};

export type PageImageStatus = {
  available: boolean;   // false for a job processed before page images were kept
  pages: number;
  ready: boolean;       // true only when every page has exactly one confirmed number
  problems: PageProblem[];
};

export async function listJobs(): Promise<Job[]> {
  const res = await authFetch("/jobs");
  if (!res.ok) throw new Error("Failed to load jobs");
  return res.json();
}

// Uploads the booklet's PDFs and queues the job. `onProgress` is called as bytes go out
// (see lib/upload.ts); `signal` cancels it. Throws UploadCancelled if cancelled, or an
// Error whose message says what went wrong in words a reviewer can act on.
export async function createJob(
  university: string, year: string, files: FileList,
  opts: { onProgress?: (p: UploadProgress) => void; signal?: AbortSignal } = {},
): Promise<{ id: number }> {
  const form = new FormData();
  Array.from(files).forEach((f) => form.append("files", f));
  const token = getToken();
  const params = new URLSearchParams({ university, year });
  const res = await sendWithProgress({
    url: `${API_URL}/jobs?${params}`,
    body: form,
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    onProgress: opts.onProgress ?? (() => {}),
    signal: opts.signal,
  });
  if (res.status === 401) {
    logout();
    throw new Error("Session expired - please log in again");
  }
  return readJobReply(res.status, res.text);
}

export async function getJob(id: number): Promise<{ job: Job; records: ReviewRecord[] }> {
  const res = await authFetch(`/jobs/${id}`);
  if (!res.ok) throw new Error("Failed to load job");
  return res.json();
}

export async function patchRecord(id: number, body: Partial<ReviewRecord>): Promise<void> {
  const res = await authFetch(`/records/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error("Save failed");
}

export function fileUrl(key: string): string {
  const token = getToken() || "";
  return `${API_URL}/files/${key}?token=${encodeURIComponent(token)}`;
}

export function exportUrl(jobId: number): string {
  const token = getToken() || "";
  return `${API_URL}/jobs/${jobId}/export?token=${encodeURIComponent(token)}`;
}

export async function getPageImageStatus(jobId: number): Promise<PageImageStatus> {
  const res = await authFetch(`/jobs/${jobId}/page-images/status`);
  if (!res.ok) throw new Error("Failed to load page image status");
  return res.json();
}

// Sets a page's printed number on every row of that page at once.
export async function setPageNumber(pageId: number, page: string): Promise<{ records: number; page: string }> {
  const res = await authFetch(`/pages/${pageId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ page }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(typeof body?.detail === "string" ? body.detail : "Could not set the page number");
  }
  return res.json();
}

// Every page that has names on it, as its own PNG named by printed page number.
export function pageImagesUrl(jobId: number): string {
  const token = getToken() || "";
  return `${API_URL}/jobs/${jobId}/page-images?token=${encodeURIComponent(token)}`;
}

export { API_URL };
