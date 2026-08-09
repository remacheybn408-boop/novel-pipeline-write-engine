import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate } from "react-router-dom";
import {
  archiveProject,
  deleteProject,
  listProjects,
  restoreProject,
  updateProject,
  type Project,
} from "../../lib/api/projects";
import {
  archiveConversation,
  deleteConversation,
  listConversations,
  type ConversationSummary,
} from "../../lib/api/conversations";
import type { AuthUser } from "../../lib/api/auth";
import { useClickOutside } from "../../lib/hooks/useClickOutside";
import { selectedProjectKey, useViewMode } from "../../app/ViewModeContext";
import {
  ArchiveIcon,
  ChevronDownIcon,
  FileTextIcon,
  FolderIcon,
  FolderPlusIcon,
  HistoryIcon,
  MoreHorizontalIcon,
  PanelLeftIcon,
  PencilIcon,
  PinIcon,
  PlusCircleIcon,
  PuzzleIcon,
  SettingsIcon,
  TrashIcon,
  UserIcon,
} from "../ui/icons";

const PROJECTS_QUERY_KEY = ["projects"] as const;
const PINNED_KEY_PREFIX = "proseforge:pinned-projects:";

/** Pinned project ids per view mode, stored locally (display preference). */
function loadPinned(mode: string): string[] {
  try {
    const raw = JSON.parse(localStorage.getItem(PINNED_KEY_PREFIX + mode) ?? "[]");
    return Array.isArray(raw) ? raw.filter((id) => typeof id === "string") : [];
  } catch {
    return [];
  }
}

function NavItem({
  icon,
  label,
  badge,
  onClick,
  title,
}: {
  icon: ReactNode;
  label: string;
  badge?: ReactNode;
  onClick?: () => void;
  title?: string;
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm text-ink transition-colors hover:bg-hover"
    >
      <span className="text-ink-secondary">{icon}</span>
      <span className="truncate">{label}</span>
      {badge}
    </button>
  );
}

function IconButton({
  icon,
  title,
  onClick,
}: {
  icon: ReactNode;
  title: string;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className="flex h-9 w-9 items-center justify-center rounded-lg text-ink-secondary transition-colors hover:bg-hover hover:text-ink"
    >
      {icon}
    </button>
  );
}

function MenuItem({
  icon,
  label,
  danger,
  onClick,
}: {
  icon: ReactNode;
  label: string;
  danger?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex w-full items-center gap-2.5 rounded-md px-3.5 py-2 text-left text-sm transition-colors hover:bg-hover ${
        danger ? "text-red-600" : "text-ink"
      }`}
    >
      <span className={danger ? "text-red-500" : "text-ink-secondary"}>{icon}</span>
      {label}
    </button>
  );
}

/** One project row with a hover "···" menu: characters / knowledge base / rename / pin / delete / archive. */
function ProjectRow({
  project,
  pinned,
  archived = false,
  expanded,
  onToggleExpand,
  onCharacters,
  onKnowledgeBase,
  onRename,
  onTogglePin,
  onArchive,
  onUnarchive,
  onDelete,
}: {
  project: Project;
  pinned: boolean;
  archived?: boolean;
  /** Conversation-group chevron; rendered only when onToggleExpand is set. */
  expanded?: boolean;
  onToggleExpand?: () => void;
  onCharacters: (project: Project) => void;
  onKnowledgeBase: (project: Project) => void;
  onRename: (project: Project) => void;
  onTogglePin: (project: Project) => void;
  onArchive: (project: Project) => void;
  onUnarchive: (project: Project) => void;
  onDelete: (project: Project) => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useClickOutside<HTMLDivElement>(() => setMenuOpen(false));

  useEffect(() => {
    if (!menuOpen) return;
    function handleKey(event: KeyboardEvent) {
      if (event.key === "Escape") setMenuOpen(false);
    }
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [menuOpen]);

  return (
    <div ref={menuRef} className="group relative">
      <div className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm text-ink transition-colors hover:bg-hover">
        {onToggleExpand && (
          <button
            type="button"
            title={expanded ? "收起会话" : "展开会话"}
            onClick={(event) => {
              // Separate click zone from the project row itself.
              event.stopPropagation();
              onToggleExpand();
            }}
            className="-ml-1 flex h-5 w-5 shrink-0 items-center justify-center rounded text-ink-secondary transition-colors hover:bg-line hover:text-ink"
          >
            <ChevronDownIcon size={14} className={`transition-transform ${expanded ? "" : "-rotate-90"}`} />
          </button>
        )}
        <span className="text-ink-secondary">
          <FolderIcon size={18} />
        </span>
        <span className="truncate" title={project.title}>
          {project.title}
        </span>
        {pinned && (
          <span className="shrink-0 text-ink-secondary">
            <PinIcon size={13} />
          </span>
        )}
        <button
          type="button"
          title="项目操作"
          onClick={(event) => {
            event.stopPropagation();
            setMenuOpen((v) => !v);
          }}
          className={`ml-auto flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-ink-secondary transition-all hover:bg-line hover:text-ink ${
            menuOpen ? "opacity-100" : "opacity-0 group-hover:opacity-100"
          }`}
        >
          <MoreHorizontalIcon size={16} />
        </button>
      </div>
      {menuOpen && (
        <div className="absolute right-0 top-full z-30 mt-1 w-[180px] rounded-xl border border-line bg-white py-1.5 shadow-[0_8px_30px_rgba(0,0,0,0.08)]">
          {archived ? (
            <MenuItem
              icon={<ArchiveIcon size={16} />}
              label="恢复"
              onClick={() => {
                setMenuOpen(false);
                onUnarchive(project);
              }}
            />
          ) : (
            <>
              <MenuItem
                icon={<UserIcon size={16} />}
                label="角色"
                onClick={() => {
                  setMenuOpen(false);
                  onCharacters(project);
                }}
              />
              <MenuItem
                icon={<FileTextIcon size={16} />}
                label="知识库"
                onClick={() => {
                  setMenuOpen(false);
                  onKnowledgeBase(project);
                }}
              />
              <MenuItem
                icon={<PencilIcon size={16} />}
                label="编辑项目标题"
                onClick={() => {
                  setMenuOpen(false);
                  onRename(project);
                }}
              />
              <MenuItem
                icon={<PinIcon size={16} />}
                label={pinned ? "取消置顶" : "置顶"}
                onClick={() => {
                  setMenuOpen(false);
                  onTogglePin(project);
                }}
              />
            </>
          )}
          <MenuItem
            icon={<TrashIcon size={16} />}
            label="删除"
            danger
            onClick={() => {
              setMenuOpen(false);
              onDelete(project);
            }}
          />
          {!archived && (
            <MenuItem
              icon={<ArchiveIcon size={16} />}
              label="归档"
              onClick={() => {
                setMenuOpen(false);
                onArchive(project);
              }}
            />
          )}
        </div>
      )}
    </div>
  );
}

/** One conversation row (chat mode) with a hover "···" menu: archive/restore + delete. */
function ConversationRow({
  conversation,
  archived = false,
  onOpen,
  onArchive,
  onUnarchive,
  onDelete,
}: {
  conversation: ConversationSummary;
  archived?: boolean;
  onOpen: (conversation: ConversationSummary) => void;
  onArchive: (conversation: ConversationSummary) => void;
  onUnarchive: (conversation: ConversationSummary) => void;
  onDelete: (conversation: ConversationSummary) => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useClickOutside<HTMLDivElement>(() => setMenuOpen(false));

  useEffect(() => {
    if (!menuOpen) return;
    function handleKey(event: KeyboardEvent) {
      if (event.key === "Escape") setMenuOpen(false);
    }
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [menuOpen]);

  return (
    <div ref={menuRef} className="group relative">
      <div
        role="button"
        tabIndex={0}
        onClick={() => onOpen(conversation)}
        onKeyDown={(event) => {
          if (event.key === "Enter") onOpen(conversation);
        }}
        className="flex w-full cursor-pointer items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm text-ink transition-colors hover:bg-hover"
      >
        <span className="text-ink-secondary">
          <HistoryIcon size={18} />
        </span>
        <span className="truncate" title={conversation.title}>
          {conversation.title || "未命名会话"}
        </span>
        <button
          type="button"
          title="会话操作"
          onClick={(event) => {
            event.stopPropagation();
            setMenuOpen((v) => !v);
          }}
          className={`ml-auto flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-ink-secondary transition-all hover:bg-line hover:text-ink ${
            menuOpen ? "opacity-100" : "opacity-0 group-hover:opacity-100"
          }`}
        >
          <MoreHorizontalIcon size={16} />
        </button>
      </div>
      {menuOpen && (
        <div className="absolute right-0 top-full z-30 mt-1 w-[180px] rounded-xl border border-line bg-white py-1.5 shadow-[0_8px_30px_rgba(0,0,0,0.08)]">
          {archived && (
            <MenuItem
              icon={<ArchiveIcon size={16} />}
              label="恢复"
              onClick={() => {
                setMenuOpen(false);
                onUnarchive(conversation);
              }}
            />
          )}
          <MenuItem
            icon={<TrashIcon size={16} />}
            label="删除"
            danger
            onClick={() => {
              setMenuOpen(false);
              onDelete(conversation);
            }}
          />
          {!archived && (
            <MenuItem
              icon={<ArchiveIcon size={16} />}
              label="归档"
              onClick={() => {
                setMenuOpen(false);
                onArchive(conversation);
              }}
            />
          )}
        </div>
      )}
    </div>
  );
}

/** User avatar row + upward popup menu (插件 / 设置 / 账号管理), shared by both widths. */function UserMenu({
  user,
  displayName,
  collapsed,
}: {
  user: AuthUser;
  displayName: string;
  collapsed: boolean;
}) {
  const navigate = useNavigate();
  const { viewMode } = useViewMode();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useClickOutside<HTMLDivElement>(() => setMenuOpen(false));

  useEffect(() => {
    if (!menuOpen) return;
    function handleKey(event: KeyboardEvent) {
      if (event.key === "Escape") setMenuOpen(false);
    }
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [menuOpen]);

  const avatar = (
    <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-amber-200 to-orange-300 text-sm font-semibold text-ink">
      {displayName.charAt(0).toUpperCase()}
    </div>
  );

  return (
    <div ref={menuRef} className="relative">
      {menuOpen && (
        <div className="absolute bottom-full left-0 z-30 mb-2 w-[230px] rounded-xl border border-line bg-white py-1.5 shadow-[0_8px_30px_rgba(0,0,0,0.08)]">
          {/* Logs are a work-mode capability; hidden in chat mode, same as
              plugins. Also restricted to ADMIN: the report contains full
              tracebacks and SQL parameters, matching the backend check. */}
          {viewMode === "work" && user.role === "ADMIN" && (
            <MenuItem
              icon={<FileTextIcon size={17} />}
              label="日志"
              onClick={() => {
                setMenuOpen(false);
                navigate("/logs");
              }}
            />
          )}
          {/* Plugins are a work-mode capability; hidden in chat mode */}
          {viewMode === "work" && (
            <MenuItem
              icon={<PuzzleIcon size={17} />}
              label="插件"
              onClick={() => {
                setMenuOpen(false);
                navigate("/plugins");
              }}
            />
          )}
          <MenuItem
            icon={<SettingsIcon size={17} />}
            label="设置"
            onClick={() => {
              setMenuOpen(false);
              navigate("/settings");
            }}
          />
          <MenuItem
            icon={<UserIcon size={17} />}
            label="账号管理"
            onClick={() => {
              setMenuOpen(false);
              navigate("/account");
            }}
          />
        </div>
      )}
      {collapsed ? (
        <button
          type="button"
          title={`${displayName}（点击打开菜单）`}
          onClick={() => setMenuOpen((v) => !v)}
          className="flex h-9 w-9 items-center justify-center rounded-lg transition-colors hover:bg-hover"
        >
          {avatar}
        </button>
      ) : (
        <button
          type="button"
          onClick={() => setMenuOpen((v) => !v)}
          className="flex w-full items-center gap-2.5 rounded-xl px-2 py-2 text-left transition-colors hover:bg-hover"
        >
          {avatar}
          <span className="min-w-0 flex-1 truncate text-sm text-ink" title={user.email}>
            {displayName}
          </span>
          <span className="rounded-md bg-ink px-1.5 py-0.5 text-[10px] font-medium text-white">
            {user.role}
          </span>
        </button>
      )}
    </div>
  );
}

export function Sidebar({
  user,
  collapsed,
  onToggleCollapse,
}: {
  user: AuthUser;
  collapsed: boolean;
  onToggleCollapse: () => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { viewMode } = useViewMode();
  const [pinnedIds, setPinnedIds] = useState<string[]>(() => loadPinned(viewMode));

  // Reload the pinned list when switching view modes.
  useEffect(() => {
    setPinnedIds(loadPinned(viewMode));
  }, [viewMode]);

  const projectsQuery = useQuery({
    queryKey: [...PROJECTS_QUERY_KEY, viewMode],
    queryFn: () => listProjects(viewMode),
  });
  const conversationsQuery = useQuery({
    queryKey: ["conversations", viewMode],
    queryFn: () => listConversations({ mode: viewMode }),
  });

  const renameProjectMutation = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) => updateProject(id, { title }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: PROJECTS_QUERY_KEY }),
  });
  const deleteProjectMutation = useMutation({
    mutationFn: deleteProject,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: PROJECTS_QUERY_KEY });
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
  const deleteConversationMutation = useMutation({
    mutationFn: deleteConversation,
    // Prefix invalidation covers both the normal and the archived lists.
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["conversations"] }),
  });
  const archiveConversationMutation = useMutation({
    mutationFn: ({ id, archived }: { id: string; archived: boolean }) => archiveConversation(id, archived),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["conversations"] }),
  });
  const archiveProjectMutation = useMutation({
    mutationFn: ({ id, archived }: { id: string; archived: boolean }) =>
      archived ? archiveProject(id) : restoreProject(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: PROJECTS_QUERY_KEY }),
  });

  // Archived conversations (chat mode): collapsed by default, fetched only on expand.
  const [archivedOpen, setArchivedOpen] = useState(false);
  const archivedQuery = useQuery({
    queryKey: ["conversations", "archived"],
    queryFn: () => listConversations({ mode: "chat", archived: true }),
    enabled: viewMode === "chat" && archivedOpen,
  });
  // Archived projects (work mode): always fetched (small list) so archived
  // projects' conversations can follow them into the archive section
  // instead of falling into 未分配.
  const [archivedProjectsOpen, setArchivedProjectsOpen] = useState(false);
  const archivedProjectsQuery = useQuery({
    queryKey: ["projects", "archived"],
    queryFn: () => listProjects("work", true),
    enabled: viewMode === "work",
    staleTime: 60_000,
  });

  function handleNewProject() {
    navigate("/projects/new");
  }

  function handleRenameProject(project: Project) {
    const title = window.prompt("项目名称", project.title);
    if (!title?.trim() || title.trim() === project.title) return;
    renameProjectMutation.mutate({ id: project.id, title: title.trim() });
  }

  function handleTogglePin(project: Project) {
    const next = pinnedIds.includes(project.id)
      ? pinnedIds.filter((id) => id !== project.id)
      : [...pinnedIds, project.id];
    setPinnedIds(next);
    localStorage.setItem(PINNED_KEY_PREFIX + viewMode, JSON.stringify(next));
  }

  function handleDeleteProject(project: Project) {
    if (!window.confirm(`删除项目“${project.title}”？删除后不可恢复。`)) return;
    // Clear the per-mode selection if the deleted project was selected.
    if (localStorage.getItem(selectedProjectKey(viewMode)) === project.id) {
      localStorage.removeItem(selectedProjectKey(viewMode));
    }
    if (pinnedIds.includes(project.id)) {
      const next = pinnedIds.filter((id) => id !== project.id);
      setPinnedIds(next);
      localStorage.setItem(PINNED_KEY_PREFIX + viewMode, JSON.stringify(next));
    }
    deleteProjectMutation.mutate(project.id);
  }

  function handleDeleteConversation(conversation: ConversationSummary) {
    if (!window.confirm(`删除会话“${conversation.title || "未命名会话"}”？删除后不可恢复。`)) return;
    // Leave the chat page if the deleted conversation is the one being viewed.
    if (pathname === `/chat/${conversation.id}`) {
      navigate("/");
    }
    deleteConversationMutation.mutate(conversation.id);
  }

  function handleArchiveConversation(conversation: ConversationSummary) {
    // Archiving needs no confirmation; leave the page if it is open.
    if (pathname === `/chat/${conversation.id}`) {
      navigate("/");
    }
    archiveConversationMutation.mutate({ id: conversation.id, archived: true });
  }

  function handleUnarchiveConversation(conversation: ConversationSummary) {
    archiveConversationMutation.mutate({ id: conversation.id, archived: false });
  }

  function handleArchiveProject(project: Project) {
    // Same cleanup as deletion: clear the per-mode selection and the pin.
    if (localStorage.getItem(selectedProjectKey(viewMode)) === project.id) {
      localStorage.removeItem(selectedProjectKey(viewMode));
    }
    if (pinnedIds.includes(project.id)) {
      const next = pinnedIds.filter((id) => id !== project.id);
      setPinnedIds(next);
      localStorage.setItem(PINNED_KEY_PREFIX + viewMode, JSON.stringify(next));
    }
    archiveProjectMutation.mutate({ id: project.id, archived: true });
  }

  function handleUnarchiveProject(project: Project) {
    archiveProjectMutation.mutate({ id: project.id, archived: false });
  }

  const displayName = user.email.split("@")[0] ?? user.email;
  const { pathname } = useLocation();
  // The ink painting spans the whole shell on every route; the sidebar stays
  // a translucent veil so the painting shows through.
  const veil = "bg-white/30 backdrop-blur-sm";
  // Pinned projects first; title order (from the backend) is kept within groups.
  const sortedProjects = [...(projectsQuery.data ?? [])].sort(
    (a, b) => Number(pinnedIds.includes(b.id)) - Number(pinnedIds.includes(a.id)),
  );

  // Work mode: group conversations under their project, client-side only —
  // the list response already carries project_id, so zero extra requests.
  const workConversations = viewMode === "work" ? (conversationsQuery.data ?? []) : [];
  const projectIds = new Set(sortedProjects.map((project) => project.id));
  const archivedProjectIds = new Set((archivedProjectsQuery.data ?? []).map((project) => project.id));
  const conversationsByProject = new Map<string, ConversationSummary[]>();
  const conversationsByArchivedProject = new Map<string, ConversationSummary[]>();
  const unassignedConversations: ConversationSummary[] = [];
  for (const conversation of workConversations) {
    if (projectIds.has(conversation.project_id)) {
      const list = conversationsByProject.get(conversation.project_id) ?? [];
      list.push(conversation);
      conversationsByProject.set(conversation.project_id, list);
    } else if (archivedProjectIds.has(conversation.project_id)) {
      // Archived project: conversations follow it into the 已归档 section.
      const list = conversationsByArchivedProject.get(conversation.project_id) ?? [];
      list.push(conversation);
      conversationsByArchivedProject.set(conversation.project_id, list);
    } else {
      // Project truly missing (deleted or stale residue): catch-all group.
      unassignedConversations.push(conversation);
    }
  }

  // Expansion defaults: the project owning the currently open conversation
  // (reverse-looked-up from /chat/:conversationId) and the stored "selected"
  // project start expanded; explicit chevron toggles override the defaults.
  const [expandedProjects, setExpandedProjects] = useState<Record<string, boolean>>({});
  const openConversationId = pathname.startsWith("/chat/") ? pathname.slice("/chat/".length) : null;
  const openConversationProjectId = openConversationId
    ? workConversations.find((conversation) => conversation.id === openConversationId)?.project_id
    : undefined;
  const selectedProjectId = localStorage.getItem(selectedProjectKey("work"));

  function isProjectExpanded(projectId: string): boolean {
    return expandedProjects[projectId] ?? (projectId === openConversationProjectId || projectId === selectedProjectId);
  }

  function toggleProjectExpanded(projectId: string) {
    setExpandedProjects((prev) => ({ ...prev, [projectId]: !isProjectExpanded(projectId) }));
  }

  // Collapsed rail: icons only (logo, new chat, new project, user avatar).
  if (collapsed) {
    return (
      <aside className={`flex h-screen w-16 shrink-0 flex-col items-center gap-1 ${veil} py-4`}>
        <button
          type="button"
          title="ProseForge 首页"
          onClick={() => navigate("/")}
          className="mb-1 flex h-9 w-9 items-center justify-center rounded-[10px] bg-ink text-lg font-extrabold text-white"
        >
          P
        </button>
        <IconButton icon={<PanelLeftIcon size={18} />} title="展开导航 Ctrl B" onClick={onToggleCollapse} />
        <div className="my-2 h-px w-8 bg-line" />
        <IconButton
          icon={<PlusCircleIcon size={18} />}
          title={viewMode === "chat" ? "新建话题 Ctrl K" : "新建会话 Ctrl K"}
          onClick={() => navigate("/")}
        />
        {viewMode === "work" && (
          <IconButton icon={<FolderPlusIcon size={18} />} title="新建项目" onClick={handleNewProject} />
        )}
        <div className="flex-1" />
        <UserMenu user={user} displayName={displayName} collapsed />
      </aside>
    );
  }

  return (
    <aside className={`flex h-screen w-[290px] shrink-0 flex-col ${veil} px-3 pb-3 pt-4`}>
      {/* Header: logo + collapse toggle */}
      <div className="mb-4 flex items-center justify-between px-1">
        <div className="flex h-9 w-9 items-center justify-center rounded-[10px] bg-ink text-lg font-extrabold text-white">
          P
        </div>
        <IconButton icon={<PanelLeftIcon size={18} />} title="收起导航 Ctrl B" onClick={onToggleCollapse} />
      </div>

      {/* New conversation */}
      <button
        type="button"
        onClick={() => navigate("/")}
        className="mb-4 flex h-11 w-full items-center gap-2 rounded-xl border border-line bg-white px-3.5 text-sm text-ink transition-colors hover:border-ink-secondary/40"
      >
        <PlusCircleIcon size={18} className="text-ink-secondary" />
        <span>{viewMode === "chat" ? "新建话题" : "新建会话"}</span>
        <kbd className="ml-auto rounded-md border border-line bg-sidebar px-1.5 py-0.5 text-[11px] text-ink-secondary">
          Ctrl K
        </kbd>
      </button>

      {/* Scrollable middle: history + projects */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        {/* Conversation history (chat mode only; work conversations group
            under their projects below) */}
        {viewMode === "chat" && (conversationsQuery.data ?? []).length > 0 && (
          <>
            <p className="mb-1 mt-1 px-3 text-xs text-ink-secondary">我的 ProseForge</p>
            <nav className="flex flex-col gap-0.5">
              {(conversationsQuery.data ?? []).map((conversation) => (
                <ConversationRow
                  key={conversation.id}
                  conversation={conversation}
                  onOpen={(item) => navigate(`/chat/${item.id}`)}
                  onArchive={handleArchiveConversation}
                  onUnarchive={handleUnarchiveConversation}
                  onDelete={handleDeleteConversation}
                />
              ))}
            </nav>
          </>
        )}

        {/* Archived conversations (chat mode): collapsed, lazy-fetched */}
        {viewMode === "chat" && (
          <div className="mt-1">
            <button
              type="button"
              onClick={() => setArchivedOpen((v) => !v)}
              className="flex w-full items-center gap-1.5 rounded-lg px-3 py-1.5 text-left text-xs text-ink-secondary transition-colors hover:bg-hover hover:text-ink"
            >
              <ChevronDownIcon
                size={14}
                className={`transition-transform ${archivedOpen ? "rotate-180" : ""}`}
              />
              已归档{archivedQuery.data ? ` (${archivedQuery.data.length})` : ""}
            </button>
            {archivedOpen && (
              <div className="flex flex-col gap-0.5">
                {(archivedQuery.data ?? []).length === 0 ? (
                  <p className="px-3 py-1.5 text-xs text-ink-secondary">
                    {archivedQuery.isPending ? "加载中…" : "暂无已归档会话"}
                  </p>
                ) : (
                  (archivedQuery.data ?? []).map((conversation) => (
                    <ConversationRow
                      key={conversation.id}
                      conversation={conversation}
                      archived
                      onOpen={(item) => navigate(`/chat/${item.id}`)}
                      onArchive={handleArchiveConversation}
                      onUnarchive={handleUnarchiveConversation}
                      onDelete={handleDeleteConversation}
                    />
                  ))
                )}
              </div>
            )}
          </div>
        )}

        {/* Projects (work mode only; chat mode is project-less) */}
        {viewMode === "work" && (
          <>
            <p className="mb-1 mt-5 px-3 text-xs text-ink-secondary">项目</p>
            <div className="flex flex-col gap-0.5">
              <NavItem icon={<FolderPlusIcon size={18} />} label="新建项目" onClick={handleNewProject} />
              {sortedProjects.map((project) => {
                const projectConversations = conversationsByProject.get(project.id) ?? [];
                // No chevron for projects without conversations.
                const expandable = projectConversations.length > 0;
                const expanded = isProjectExpanded(project.id);
                return (
                  <div key={project.id}>
                    <ProjectRow
                      project={project}
                      pinned={pinnedIds.includes(project.id)}
                      {...(expandable ? { expanded, onToggleExpand: () => toggleProjectExpanded(project.id) } : {})}
                      onCharacters={(item) => navigate(`/projects/${item.id}/characters`)}                      onKnowledgeBase={(item) => navigate(`/projects/${item.id}/knowledge-base`)}
                      onRename={handleRenameProject}
                      onTogglePin={handleTogglePin}
                      onArchive={handleArchiveProject}
                      onUnarchive={handleUnarchiveProject}
                      onDelete={handleDeleteProject}
                    />
                    {expandable && expanded && (
                      <div className="flex flex-col gap-0.5 pb-1 pl-6">
                        {projectConversations.map((conversation) => (
                          <ConversationRow
                            key={conversation.id}
                            conversation={conversation}
                            onOpen={(item) => navigate(`/chat/${item.id}`)}
                            onArchive={handleArchiveConversation}
                            onUnarchive={handleUnarchiveConversation}
                            onDelete={handleDeleteConversation}
                          />
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}

              {/* Conversations whose project is truly missing (deleted/stale). */}
              {unassignedConversations.length > 0 && (
                <>
                  <p className="mb-1 mt-3 px-3 text-xs text-ink-secondary">未分配</p>
                  <div className="flex flex-col gap-0.5">
                    {unassignedConversations.map((conversation) => (
                      <ConversationRow
                        key={conversation.id}
                        conversation={conversation}
                        onOpen={(item) => navigate(`/chat/${item.id}`)}
                        onArchive={handleArchiveConversation}
                        onUnarchive={handleUnarchiveConversation}
                        onDelete={handleDeleteConversation}
                      />
                    ))}
                  </div>
                </>
              )}
            </div>

            {/* Archived projects: sibling of the list, collapsed and lazy-fetched */}
            <div className="mt-1">
              <button
                type="button"
                onClick={() => setArchivedProjectsOpen((v) => !v)}
                className="flex w-full items-center gap-1.5 rounded-lg px-3 py-1.5 text-left text-xs text-ink-secondary transition-colors hover:bg-hover hover:text-ink"
              >
                <ChevronDownIcon
                  size={14}
                  className={`transition-transform ${archivedProjectsOpen ? "rotate-180" : ""}`}
                />
                已归档{archivedProjectsQuery.data ? ` (${archivedProjectsQuery.data.length})` : ""}
              </button>
              {archivedProjectsOpen && (
                <div className="flex flex-col gap-0.5">
                  {(archivedProjectsQuery.data ?? []).length === 0 ? (
                    <p className="px-3 py-1.5 text-xs text-ink-secondary">
                      {archivedProjectsQuery.isPending ? "加载中…" : "暂无已归档项目"}
                    </p>
                  ) : (
                    (archivedProjectsQuery.data ?? []).map((project) => (
                      <div key={project.id}>
                        <ProjectRow
                          project={project}
                          pinned={false}
                          archived
                          onCharacters={(item) => navigate(`/projects/${item.id}/characters`)}                          onKnowledgeBase={(item) => navigate(`/projects/${item.id}/knowledge-base`)}
                          onRename={handleRenameProject}
                          onTogglePin={handleTogglePin}
                          onArchive={handleArchiveProject}
                          onUnarchive={handleUnarchiveProject}
                          onDelete={handleDeleteProject}
                        />
                        {/* Conversations follow the archived project. */}
                        {(conversationsByArchivedProject.get(project.id) ?? []).length > 0 && (
                          <div className="ml-4 flex flex-col gap-0.5">
                            {(conversationsByArchivedProject.get(project.id) ?? []).map((conversation) => (
                              <ConversationRow
                                key={conversation.id}
                                conversation={conversation}
                                onOpen={(item) => navigate(`/chat/${item.id}`)}
                                onArchive={handleArchiveConversation}
                                onUnarchive={handleUnarchiveConversation}
                                onDelete={handleDeleteConversation}
                              />
                            ))}
                          </div>
                        )}
                      </div>
                    ))
                  )}
                </div>
              )}
            </div>
          </>
        )}
      </div>

      {/* User row with popup menu */}
      <UserMenu user={user} displayName={displayName} collapsed={false} />
    </aside>
  );
}
