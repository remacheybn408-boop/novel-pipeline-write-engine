import { useLocation, useNavigate } from "react-router-dom";
import type { ComponentType } from "react";
import { useViewMode, type ChatMode, type ViewMode } from "../../app/ViewModeContext";
import { MonitorIcon, NetworkIcon, SmileIcon } from "../ui/icons";

const TABS: { value: ViewMode; label: string; icon: ComponentType<{ size?: number }> }[] = [
  { value: "work", label: "Work", icon: MonitorIcon },
  { value: "chat", label: "Chat", icon: SmileIcon },
];

/** 普通 / 集群模式 pill shown at the right end of the top bar (work home only). */
function ModeToggle() {
  const { chatMode, setChatMode } = useViewMode();
  const options: { value: ChatMode; label: string; icon?: boolean }[] = [
    { value: "normal", label: "普通" },
    { value: "swarm", label: "集群模式", icon: true },
  ];
  return (
    <div className="flex items-center rounded-full border border-line bg-white p-0.5">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          onClick={() => setChatMode(option.value)}
          className={`flex items-center gap-1.5 rounded-full px-3.5 py-1.5 text-sm transition-colors ${
            chatMode === option.value ? "bg-ink text-white" : "text-ink-secondary hover:text-ink"
          }`}
        >
          {option.icon && <NetworkIcon size={14} />}
          {option.label}
        </button>
      ))}
    </div>
  );
}

/**
 * Centered segmented Work | Chat switch above the main content, with the
 * normal/swarm toggle pinned to the right corner (work home only). Only
 * rendered on the home page and chat pages (settings/plugins keep their own
 * close affordance). Switching segments sets the global view mode and
 * returns home.
 */
export function TopBar() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const { viewMode, setViewMode } = useViewMode();

  const visible = pathname === "/" || pathname.startsWith("/chat/");
  if (!visible) return null;

  return (
    <div className="relative flex h-14 shrink-0 items-center justify-center bg-transparent">
      <div className="flex items-center gap-1 rounded-full bg-hover p-1">
        {TABS.map((tab) => {
          const active = viewMode === tab.value;
          const TabIcon = tab.icon;
          return (
            <button
              key={tab.value}
              type="button"
              onClick={() => {
                setViewMode(tab.value);
                navigate("/");
              }}
              className={`flex items-center gap-1.5 rounded-full px-4 py-1.5 text-sm transition-all ${
                active
                  ? "bg-white font-medium text-ink shadow-sm"
                  : "text-ink-secondary hover:text-ink"
              }`}
            >
              <TabIcon size={16} />
              {tab.label}
            </button>
          );
        })}
      </div>
      {pathname === "/" && viewMode === "work" && (
        <div className="absolute right-6 top-1/2 -translate-y-1/2">
          <ModeToggle />
        </div>
      )}
    </div>
  );
}
