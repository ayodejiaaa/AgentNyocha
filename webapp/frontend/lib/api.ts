// Thin client for the API in webapp/api - one place that knows the base
// URL and how the token is stored, so every page just calls these.

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
  reviewed: boolean;
};

export async function listJobs(): Promise<Job[]> {
  const res = await authFetch("/jobs");
  if (!res.ok) throw new Error("Failed to load jobs");
  return res.json();
}

export async function createJob(university: string, year: string, files: FileList): Promise<{ id: number }> {
  const form = new FormData();
  Array.from(files).forEach((f) => form.append("files", f));
  const token = getToken();
  const params = new URLSearchParams({ university, year });
  const res = await fetch(`${API_URL}/jobs?${params}`, {
    method: "POST",
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    body: form,
  });
  if (!res.ok) throw new Error("Upload failed");
  return res.json();
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

export { API_URL };
