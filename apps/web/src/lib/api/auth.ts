/**
 * Auth endpoints — proseforge/api/routes/auth.py
 *
 * Confirmed shapes:
 *   POST /api/v1/auth/setup  {email, password} -> 201 {id, email, role}
 *                            409 when the initial setup already completed.
 *   POST /api/v1/auth/login  {email, password} -> {access_token, token_type}
 *                            Also sets the "proseforge_session" httpOnly cookie.
 *                            401 on invalid credentials. Password min length 12.
 *   POST /api/v1/auth/logout -> 204, clears the cookie.
 *   GET  /api/v1/auth/me     -> {id, email, role}, 401 when unauthenticated.
 *   GET  /api/v1/auth/registration-status -> {enabled}, public (no session).
 */
import { clearTabToken, request, setTabToken } from "./client";

// Per-tab bearer-token helpers live in client.ts (the request wrapper reads
// them); re-exported here so auth callers have a single import site.
export { clearTabToken, getTabToken, setTabToken } from "./client";

export interface AuthUser {
  id: string;
  email: string;
  role: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
}

export function fetchMe(): Promise<AuthUser> {
  return request<AuthUser>("/api/v1/auth/me");
}

export async function login(email: string, password: string): Promise<LoginResponse> {
  const response = await request<LoginResponse>("/api/v1/auth/login", {
    method: "POST",
    body: { email, password },
  });
  // Pin this tab to the freshly logged-in account. A new login simply
  // overwrites the tab token; the shared cookie being overwritten too is
  // harmless because every tab's bearer token locks its own account.
  setTabToken(response.access_token);
  return response;
}

export async function logout(): Promise<void> {
  try {
    await request<void>("/api/v1/auth/logout", { method: "POST" });
  } finally {
    // The server no longer revokes sessions on logout (multi-account
    // coexistence); dropping the tab token is what signs this tab out.
    clearTabToken();
  }
}

export function setup(email: string, password: string): Promise<AuthUser> {
  return request<AuthUser>("/api/v1/auth/setup", {
    method: "POST",
    body: { email, password },
  });
}

/**
 * POST /api/v1/auth/register {email, password} -> 201 {id, email, role}
 * Self-service USER registration; only available when the instance enables
 * PROSEFORGE_ALLOW_REGISTRATION (403 otherwise, 409 on duplicate email).
 */
export function register(email: string, password: string): Promise<AuthUser> {
  return request<AuthUser>("/api/v1/auth/register", {
    method: "POST",
    body: { email, password },
  });
}

/**
 * GET /api/v1/auth/registration-status -> {enabled, initialized}. Public
 * endpoint; the login page uses it to pick the account entry (setup for a
 * fresh instance, register when allowed, plain login otherwise).
 */
export function fetchRegistrationStatus(): Promise<{ enabled: boolean; initialized: boolean }> {
  return request<{ enabled: boolean; initialized: boolean }>("/api/v1/auth/registration-status");
}

/**
 * PUT /api/v1/auth/password -> 204. The backend bumps session_version, so
 * every session (including the current one) is revoked on success and the
 * user must log in again with the new password.
 */
export function changePassword(currentPassword: string, newPassword: string): Promise<void> {
  return request<void>("/api/v1/auth/password", {
    method: "PUT",
    body: { current_password: currentPassword, new_password: newPassword },
  });
}
