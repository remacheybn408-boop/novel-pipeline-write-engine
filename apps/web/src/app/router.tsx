import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom";
import { AppShell } from "../components/layout/AppShell";
import { useAuth } from "../features/auth/useAuth";
import { useViewMode } from "./ViewModeContext";
import { LoginPage } from "../features/auth/LoginPage";
import { HomePage } from "../features/home/HomePage";
import { ChatPage } from "../features/chat/ChatPage";
import { SettingsPage } from "../features/settings/SettingsPage";
import { AgentRunPage } from "../features/agent/AgentRunPage";
import { PluginsPage } from "../features/plugins/PluginsPage";
import { NewProjectPage } from "../features/projects/NewProjectPage";
import { CharactersPage } from "../features/projects/CharactersPage";
import { KnowledgePage } from "../features/knowledge/KnowledgePage";
import { LogsPage } from "../features/logs/LogsPage";
import { AccountPage } from "../features/account/AccountPage";

/**
 * Direct-URL guard for /logs: the sidebar only renders the entry for ADMIN
 * users in work mode (Sidebar.tsx) and the download endpoint enforces ADMIN
 * server-side; mirror both checks here so typing the URL is not enough.
 * Rendered inside AppShell's outlet, so ViewModeProvider is available.
 */
function LogsRouteGuard() {
  const { user, isLoading } = useAuth();
  const { viewMode } = useViewMode();
  if (isLoading) {
    return null; // AppShell renders the loading state around the outlet.
  }
  if (viewMode !== "work" || !user || user.role !== "ADMIN") {
    return <Navigate to="/" replace />;
  }
  return <LogsPage />;
}

const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },
  {
    element: <AppShell />,
    children: [
      { path: "/", element: <HomePage /> },
      { path: "/chat/:conversationId", element: <ChatPage /> },
      { path: "/agent-runs/:runId", element: <AgentRunPage /> },
      { path: "/settings", element: <SettingsPage /> },
      { path: "/plugins", element: <PluginsPage /> },
      { path: "/projects/new", element: <NewProjectPage /> },
      { path: "/projects/:projectId/characters", element: <CharactersPage /> },
      { path: "/projects/:projectId/knowledge-base", element: <KnowledgePage /> },
      { path: "/logs", element: <LogsRouteGuard /> },
      { path: "/account", element: <AccountPage /> },
    ],
  },
  { path: "*", element: <Navigate to="/" replace /> },
]);

export function AppRouter() {
  return <RouterProvider router={router} />;
}
