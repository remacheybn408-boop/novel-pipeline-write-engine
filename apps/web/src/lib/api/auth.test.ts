/**
 * Contract tests for the auth API client additions.
 *
 * The backend contract lives in proseforge/api/routes/auth.py:
 *   GET /api/v1/auth/registration-status -> {enabled} (public, no session)
 * The login page hides the self-registration entry unless this flag is true.
 *
 * Multi-account coexistence: login() pins the tab via sessionStorage and
 * logout() drops that pin (the server no longer revokes on logout).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { request } from "./client";
import { fetchRegistrationStatus, getTabToken, login, logout, setTabToken } from "./auth";

// Only the HTTP call is mocked; the tab-token storage helpers stay real so
// login/logout token handling is exercised end to end.
vi.mock("./client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./client")>();
  return { ...actual, request: vi.fn() };
});

const mockedRequest = vi.mocked(request);

function stubSessionStorage(): Storage {
  const store = new Map<string, string>();
  return {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (key: string) => store.get(key) ?? null,
    key: () => null,
    removeItem: (key: string) => {
      store.delete(key);
    },
    setItem: (key: string, value: string) => {
      store.set(key, value);
    },
  };
}

beforeEach(() => {
  mockedRequest.mockReset();
  vi.stubGlobal("window", { sessionStorage: stubSessionStorage() });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("auth api client", () => {
  it("fetches the public registration-status flag", async () => {
    mockedRequest.mockResolvedValue({ enabled: true });
    const status = await fetchRegistrationStatus();
    expect(mockedRequest).toHaveBeenCalledWith("/api/v1/auth/registration-status");
    expect(status).toEqual({ enabled: true });
  });

  it("login pins this tab to the fresh access token", async () => {
    setTabToken("stale-token");
    mockedRequest.mockResolvedValue({ access_token: "token-a", token_type: "bearer" });
    const response = await login("a@b.co", "p".repeat(12));
    expect(response.access_token).toBe("token-a");
    // A new login overwrites any previous tab token.
    expect(getTabToken()).toBe("token-a");
  });

  it("logout clears the tab token", async () => {
    setTabToken("token-a");
    mockedRequest.mockResolvedValue(undefined);
    await logout();
    expect(getTabToken()).toBeNull();
  });

  it("logout clears the tab token even when the request fails", async () => {
    setTabToken("token-a");
    mockedRequest.mockRejectedValue(new Error("network down"));
    await expect(logout()).rejects.toThrow("network down");
    expect(getTabToken()).toBeNull();
  });
});
