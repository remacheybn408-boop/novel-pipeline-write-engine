/**
 * Shared API request wrapper.
 *
 * All requests go through the Vite dev proxy ("/api" -> localhost:8000) in
 * development and are same-origin in production. Authentication is dual:
 * a per-tab bearer token (sessionStorage) when present — this pins each
 * browser tab to its own account for multi-account coexistence — with the
 * shared "proseforge_session" cookie as fallback. Every request sends
 * credentials so the cookie path keeps working.
 */

const TAB_TOKEN_KEY = "proseforge.tab_token";

function tabStorage(): Storage | null {
  try {
    if (typeof window === "undefined") return null;
    return window.sessionStorage ?? null;
  } catch {
    // Access can throw when storage is disabled (privacy modes, iframes).
    return null;
  }
}

/** Bearer token pinning this tab to its account, or null (cookie fallback). */
export function getTabToken(): string | null {
  return tabStorage()?.getItem(TAB_TOKEN_KEY) ?? null;
}

export function setTabToken(token: string): void {
  tabStorage()?.setItem(TAB_TOKEN_KEY, token);
}

export function clearTabToken(): void {
  tabStorage()?.removeItem(TAB_TOKEN_KEY);
}

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/**
 * Dispatched on `window` when a request fails with 401, meaning the session
 * (tab token or cookie) has expired. AppProviders listens and invalidates
 * the auth query so the route guard drops the user back to /login.
 */
export const SESSION_EXPIRED_EVENT = "proseforge:session-expired";

/**
 * Paths whose 401 must NOT broadcast session expiry:
 * - login: 401 means invalid credentials and is surfaced on the form.
 * - me: the auth query already maps its own 401 to "unauthenticated";
 *   broadcasting here would re-invalidate it in a loop.
 */
const SESSION_EXPIRED_EXCLUDED_PATHS = ["/api/v1/auth/login", "/api/v1/auth/me"];

const STATUS_MESSAGES: Record<number, string> = {
  400: "请求参数有误",
  401: "未登录或登录已过期",
  403: "没有权限执行此操作",
  404: "请求的资源不存在",
  409: "资源冲突，可能已存在",
  422: "请求参数校验失败",
};

function friendlyMessage(status: number, detail: string | null): string {
  if (detail) return detail;
  if (status === 0) return "网络连接失败，请确认后端服务已启动";
  if (status >= 500) return "服务器内部错误，请稍后重试";
  return STATUS_MESSAGES[status] ?? `请求失败（HTTP ${status}）`;
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined>;
  headers?: Record<string, string>;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, query } = options;

  let url = path;
  if (query) {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined) params.set(key, String(value));
    }
    const qs = params.toString();
    if (qs) url += `?${qs}`;
  }

  let response: Response;
  try {
    // Bearer wins over the cookie server-side (_resolve_user), so a tab with
    // a token stays on its own account no matter which session the shared
    // cookie currently holds. Explicit per-call headers still override.
    const tabToken = getTabToken();
    response = await fetch(url, {
      method,
      credentials: "include",
      headers: {
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...(tabToken ? { Authorization: `Bearer ${tabToken}` } : {}),
        ...options.headers,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, friendlyMessage(0, null));
  }

  if (!response.ok) {
    let detail: string | null = null;
    try {
      const payload: unknown = await response.json();
      if (payload && typeof payload === "object" && "detail" in payload) {
        const raw = (payload as { detail: unknown }).detail;
        detail = typeof raw === "string" ? raw : JSON.stringify(raw);
      } else if (payload && typeof payload === "object" && "error" in payload) {
        // v3 routes wrap errors as {"error": {code, message, ...}}.
        const raw = (payload as { error: { message?: unknown } }).error;
        if (typeof raw?.message === "string") detail = raw.message;
      }
    } catch {
      // Non-JSON error body; fall back to the generic message.
    }
    if (
      response.status === 401 &&
      typeof window !== "undefined" &&
      !SESSION_EXPIRED_EXCLUDED_PATHS.some((excluded) => path.startsWith(excluded))
    ) {
      window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
    }
    throw new ApiError(response.status, friendlyMessage(response.status, detail));
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
