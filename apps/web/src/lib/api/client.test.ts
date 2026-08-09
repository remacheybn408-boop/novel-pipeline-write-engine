import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, SESSION_EXPIRED_EVENT, clearTabToken, request, setTabToken } from "./client";

/**
 * Regression tests for the session-expiry broadcast: a 401 from any API
 * call (other than login/me) must dispatch SESSION_EXPIRED_EVENT so the
 * auth query is invalidated and the user lands back on /login.
 */

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("request() session-expiry handling", () => {
  let windowTarget: EventTarget;
  let expiredCount: number;

  beforeEach(() => {
    // Node has no window; stub it with an EventTarget so client.ts can dispatch.
    windowTarget = new EventTarget();
    vi.stubGlobal("window", windowTarget);
    expiredCount = 0;
    windowTarget.addEventListener(SESSION_EXPIRED_EVENT, () => {
      expiredCount += 1;
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("dispatches SESSION_EXPIRED_EVENT on a 401 from a regular endpoint", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(401, { detail: "unauthorized" })));

    await expect(request("/api/v1/documents")).rejects.toMatchObject({
      name: "ApiError",
      status: 401,
    });
    expect(expiredCount).toBe(1);
  });

  it("does not dispatch on a 401 from the login endpoint (bad credentials)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(401, { detail: "invalid" })));

    await expect(
      request("/api/v1/auth/login", { method: "POST", body: { email: "a@b.c", password: "x" } }),
    ).rejects.toBeInstanceOf(ApiError);
    expect(expiredCount).toBe(0);
  });

  it("does not dispatch on a 401 from the me endpoint (auth query handles it)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(401, { detail: "unauthorized" })));

    await expect(request("/api/v1/auth/me")).rejects.toBeInstanceOf(ApiError);
    expect(expiredCount).toBe(0);
  });

  it("does not dispatch for non-401 errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(500, { detail: "boom" })));

    await expect(request("/api/v1/documents")).rejects.toMatchObject({ status: 500 });
    expect(expiredCount).toBe(0);
  });
});

/**
 * Multi-account coexistence: a tab with a sessionStorage bearer token sends
 * Authorization on every request (bearer wins over the cookie server-side),
 * so two tabs stay pinned to their own accounts; without a token the
 * request falls back to the shared session cookie.
 */
describe("request() tab bearer token", () => {
  let store: Map<string, string>;
  let fetchMock: ReturnType<typeof vi.fn>;

  function lastCallHeaders(): Record<string, string> {
    const init = fetchMock.mock.calls.at(-1)?.[1] as RequestInit;
    return init.headers as Record<string, string>;
  }

  beforeEach(() => {
    store = new Map();
    vi.stubGlobal("window", {
      sessionStorage: {
        getItem: (key: string) => store.get(key) ?? null,
        setItem: (key: string, value: string) => void store.set(key, value),
        removeItem: (key: string) => void store.delete(key),
      },
      dispatchEvent: vi.fn(),
    });
    fetchMock = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse(200, { id: "u" })));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("injects Authorization when a tab token exists", async () => {
    setTabToken("token-a");
    await request("/api/v1/auth/me");
    expect(lastCallHeaders()["Authorization"]).toBe("Bearer token-a");
  });

  it("two tabs each resolve /me with their own token", async () => {
    setTabToken("token-a");
    await request("/api/v1/auth/me");
    expect(lastCallHeaders()["Authorization"]).toBe("Bearer token-a");

    // The other tab overwrites only its own sessionStorage entry.
    setTabToken("token-b");
    await request("/api/v1/auth/me");
    expect(lastCallHeaders()["Authorization"]).toBe("Bearer token-b");
  });

  it("sends no Authorization header without a tab token (cookie fallback)", async () => {
    await request("/api/v1/auth/me");
    expect(lastCallHeaders()).not.toHaveProperty("Authorization");
  });

  it("stops injecting Authorization once the tab token is cleared", async () => {
    setTabToken("token-a");
    clearTabToken();
    await request("/api/v1/auth/me");
    expect(lastCallHeaders()).not.toHaveProperty("Authorization");
  });
});
