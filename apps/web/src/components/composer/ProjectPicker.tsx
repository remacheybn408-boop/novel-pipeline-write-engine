import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { listProjects, type Project } from "../../lib/api/projects";
import { useClickOutside } from "../../lib/hooks/useClickOutside";
import { useViewMode } from "../../app/ViewModeContext";
import { CheckIcon, ChevronDownIcon, FolderIcon } from "../ui/icons";

export interface ProjectPickerProps {
  selected: Project | null;
  onSelect: (project: Project) => void;
  /** Forces the panel open (used when send is attempted without a project). */
  forceOpen?: boolean;
}

/**
 * Project picker chip rendered as the composer's attached bottom strip,
 * backed by GET /api/v1/projects. With no projects it guides the user to
 * the sidebar's "新建项目" entry.
 */
export function ProjectPicker({ selected, onSelect, forceOpen = false }: ProjectPickerProps) {
  const { viewMode } = useViewMode();
  const projectsQuery = useQuery({ queryKey: ["projects", viewMode], queryFn: () => listProjects(viewMode) });
  const [open, setOpen] = useState(false);
  const containerRef = useClickOutside<HTMLDivElement>(() => setOpen(false));

  const projects = projectsQuery.data ?? [];

  useEffect(() => {
    if (forceOpen) setOpen(true);
  }, [forceOpen]);

  return (
    <div ref={containerRef} className="relative mx-5 rounded-b-2xl bg-sidebar px-4 py-2.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 text-sm text-ink-secondary transition-colors hover:text-ink"
      >
        <FolderIcon size={16} />
        <span className={selected ? "text-ink" : undefined}>{selected ? selected.title : "选择项目"}</span>
        <ChevronDownIcon size={14} />
      </button>

      {open && (
        <div className="absolute bottom-full left-2 z-20 mb-2 w-64 overflow-hidden rounded-xl border border-line bg-white py-1 shadow-[0_8px_30px_rgba(0,0,0,0.08)]">
          {projects.length === 0 ? (
            <p className="px-4 py-3 text-sm leading-relaxed text-ink-secondary">
              暂无项目，请先在左侧边栏点击「新建项目」
            </p>
          ) : (
            <ul className="max-h-72 overflow-y-auto">
              {projects.map((project) => (
                <li key={project.id}>
                  <button
                    type="button"
                    onClick={() => {
                      onSelect(project);
                      setOpen(false);
                    }}
                    className="flex w-full items-center gap-2 px-3.5 py-2 text-left text-sm text-ink transition-colors hover:bg-hover"
                  >
                    <FolderIcon size={15} className="shrink-0 text-ink-secondary" />
                    <span className="min-w-0 flex-1 truncate">{project.title}</span>
                    {project.id === selected?.id && <CheckIcon size={15} className="shrink-0 text-ink" />}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
