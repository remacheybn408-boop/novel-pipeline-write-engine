import { useQuery } from "@tanstack/react-query";
import { fetchMe, type AuthUser } from "../../lib/api/auth";
import { ApiError } from "../../lib/api/client";

export const ME_QUERY_KEY = ["auth", "me"] as const;

export interface AuthState {
  user: AuthUser | null;
  isLoading: boolean;
  isAuthenticated: boolean;
}

/**
 * Session state comes from GET /api/v1/auth/me (cookie session).
 * A 401 means "not logged in" rather than a real error.
 */
export function useAuth(): AuthState {
  const query = useQuery({
    queryKey: ME_QUERY_KEY,
    queryFn: fetchMe,
    retry: false,
    staleTime: 60_000,
  });

  if (query.isPending) {
    return { user: null, isLoading: true, isAuthenticated: false };
  }
  if (query.isError) {
    if (query.error instanceof ApiError && query.error.status === 401) {
      return { user: null, isLoading: false, isAuthenticated: false };
    }
    // Network/5xx failures are treated as unauthenticated for routing;
    // the login page surfaces the message on the next attempt.
    return { user: null, isLoading: false, isAuthenticated: false };
  }
  return { user: query.data, isLoading: false, isAuthenticated: true };
}
