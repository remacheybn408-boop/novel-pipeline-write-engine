import { createContext, useContext, useState, type ReactNode } from "react";

/**
 * Global view mode: "work" (project-oriented writing workbench) vs "chat"
 * (plain conversations). Persisted in localStorage; defaults to "work" to
 * preserve the existing behavior for current users.
 */

export type ViewMode = "work" | "chat";
export type ChatMode = "normal" | "swarm";

const STORAGE_KEY = "proseforge:view-mode";
const CHAT_MODE_KEY = "proseforge:chat-mode";

function loadViewMode(): ViewMode {
  return localStorage.getItem(STORAGE_KEY) === "chat" ? "chat" : "work";
}

function loadChatMode(): ChatMode {
  return localStorage.getItem(CHAT_MODE_KEY) === "swarm" ? "swarm" : "normal";
}

interface ViewModeContextValue {
  viewMode: ViewMode;
  setViewMode: (mode: ViewMode) => void;
  chatMode: ChatMode;
  setChatMode: (mode: ChatMode) => void;
}

const ViewModeContext = createContext<ViewModeContextValue>({
  viewMode: loadViewMode(),
  setViewMode: () => {},
  chatMode: loadChatMode(),
  setChatMode: () => {},
});

export function ViewModeProvider({ children }: { children: ReactNode }) {
  const [viewMode, setViewModeState] = useState<ViewMode>(loadViewMode);
  const [chatMode, setChatModeState] = useState<ChatMode>(loadChatMode);

  function setViewMode(mode: ViewMode) {
    setViewModeState(mode);
    localStorage.setItem(STORAGE_KEY, mode);
  }

  function setChatMode(mode: ChatMode) {
    setChatModeState(mode);
    localStorage.setItem(CHAT_MODE_KEY, mode);
  }

  return (
    <ViewModeContext.Provider value={{ viewMode, setViewMode, chatMode, setChatMode }}>
      {children}
    </ViewModeContext.Provider>
  );
}

export function useViewMode(): ViewModeContextValue {
  return useContext(ViewModeContext);
}

/** localStorage key for the per-mode selected project (falls back to the legacy key). */
export function selectedProjectKey(mode: ViewMode): string {
  return `proseforge:selected-project:${mode}`;
}

export const LEGACY_SELECTED_PROJECT_KEY = "proseforge:selected-project";
