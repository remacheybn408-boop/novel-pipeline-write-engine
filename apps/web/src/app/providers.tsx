import { useEffect, type ReactNode } from "react";
import { QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { SESSION_EXPIRED_EVENT } from "../lib/api/client";
import { ME_QUERY_KEY } from "../features/auth/useAuth";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

/**
 * Any API call that answers 401 means the session cookie has expired.
 * Invalidate the auth query so useAuth refetches, reports unauthenticated,
 * and the AppShell route guard redirects to /login.
 */
function SessionExpiryListener() {
  const client = useQueryClient();

  useEffect(() => {
    function handleSessionExpired() {
      void client.invalidateQueries({ queryKey: ME_QUERY_KEY });
    }
    window.addEventListener(SESSION_EXPIRED_EVENT, handleSessionExpired);
    return () => window.removeEventListener(SESSION_EXPIRED_EVENT, handleSessionExpired);
  }, [client]);

  return null;
}

export function AppProviders({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>
      <SessionExpiryListener />
      {children}
    </QueryClientProvider>
  );
}
