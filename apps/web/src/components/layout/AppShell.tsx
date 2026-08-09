import { useEffect, useState } from "react";
import { Navigate, Outlet, useNavigate } from "react-router-dom";
import { useAuth } from "../../features/auth/useAuth";
import { Sidebar } from "./Sidebar";
import { TopBar } from "./TopBar";
import { ViewModeProvider } from "../../app/ViewModeContext";
import { FOCUS_COMPOSER_EVENT } from "../composer/Composer";

const COLLAPSED_STORAGE_KEY = "proseforge:sidebar-collapsed";

function loadCollapsed(): boolean {
  return localStorage.getItem(COLLAPSED_STORAGE_KEY) === "1";
}

/**
 * Authenticated application shell: sidebar on the left, routed content on
 * the right. Unauthenticated visitors are redirected to /login.
 *
 * Global shortcuts (registered here so they work on every page):
 *   Ctrl/Cmd+K -> go home and focus the composer
 *   Ctrl/Cmd+B -> collapse/expand the sidebar
 */
export function AppShell() {
  const { user, isLoading, isAuthenticated } = useAuth();
  const navigate = useNavigate();
  const [collapsed, setCollapsed] = useState(loadCollapsed);

  useEffect(() => {
    function handleKey(event: KeyboardEvent) {
      if (!(event.ctrlKey || event.metaKey)) return;
      const key = event.key.toLowerCase();
      if (key === "k") {
        event.preventDefault();
        navigate("/");
        // Defer so the home composer has mounted when we broadcast focus.
        window.setTimeout(() => window.dispatchEvent(new Event(FOCUS_COMPOSER_EVENT)), 60);
      } else if (key === "b") {
        event.preventDefault();
        setCollapsed((prev) => {
          const next = !prev;
          localStorage.setItem(COLLAPSED_STORAGE_KEY, next ? "1" : "0");
          return next;
        });
      }
    }
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [navigate]);

  function toggleCollapsed() {
    setCollapsed((prev) => {
      const next = !prev;
      localStorage.setItem(COLLAPSED_STORAGE_KEY, next ? "1" : "0");
      return next;
    });
  }

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center bg-white">
        <span className="text-sm text-ink-secondary">加载中…</span>
      </div>
    );
  }
  if (!isAuthenticated || !user) {
    return <Navigate to="/login" replace />;
  }

  return (
    <ViewModeProvider>
      <div className="relative flex h-screen bg-[#f7f7f7] bg-[url(/bg-ink.jpg)] bg-cover bg-[position:center_bottom] bg-no-repeat">
        {/* Translucent white veil over the ink background to keep the tone neutral */}
        <div aria-hidden className="pointer-events-none absolute inset-0 bg-white/25" />
        <div className="relative z-10 flex min-w-0 flex-1">
          <Sidebar user={user} collapsed={collapsed} onToggleCollapse={toggleCollapsed} />
          <div className="flex min-w-0 flex-1 flex-col">
            <TopBar />
            <main className="min-h-0 flex-1 overflow-y-auto">
              <Outlet />
            </main>
          </div>
        </div>
      </div>
    </ViewModeProvider>
  );
}
