// FastAPI 호출 도우미. 관리자 토큰은 브라우저 localStorage에 둔다.
const TOKEN_KEY = "dz_admin_token";

export const adminToken = {
  get: () => (typeof window === "undefined" ? null : localStorage.getItem(TOKEN_KEY)),
  set: (t: string) => localStorage.setItem(TOKEN_KEY, t),
  clear: () => localStorage.removeItem(TOKEN_KEY),
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}, admin = false): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const token = admin ? adminToken.get() : null;
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(`/api${path}`, { ...init, headers: { ...headers, ...(init.headers ?? {}) } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, body.detail ?? `요청 실패 (${res.status})`);
  return body as T;
}

export const STATUS_LABEL: Record<string, string> = {
  invited: "초대됨",
  in_progress: "진행 중",
  completed: "완료",
  report_ready: "리포트 준비됨",
  failed: "실패",
};

export const NAME: Record<string, string> = {
  candidate: "나",
  coworker: "동료",
  lead: "팀장",
  system: "알림",
  narrator: "업무 진행",
  both: "함께",
};

export type Msg = { id: number; channel: string; sender: string; content: string; time: string };
