import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { listModels, type ModelInfo } from "../../lib/api/models";
import { listProjects } from "../../lib/api/projects";
import { useClickOutside } from "../../lib/hooks/useClickOutside";
import { LEGACY_SELECTED_PROJECT_KEY, selectedProjectKey, useViewMode } from "../../app/ViewModeContext";
import { CheckIcon, ChevronDownIcon } from "../ui/icons";

export interface SelectedModel {
  provider: string;
  model_id: string;
  display_name: string;
}

const STORAGE_KEY = "proseforge:selected-model";

/** Read the persisted model pick (shared between the home and chat composers). */
export function loadSelectedModel(): SelectedModel | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as SelectedModel) : null;
  } catch {
    return null;
  }
}

function saveSelectedModel(model: SelectedModel | null): void {
  if (model) localStorage.setItem(STORAGE_KEY, JSON.stringify(model));
  else localStorage.removeItem(STORAGE_KEY);
}

/**
 * Model picker backed by GET /api/v1/models. The selection is persisted in
 * localStorage. With no configured models the button keeps its placeholder
 * look and the panel explains how to fix it.
 *
 * `onSelectionChange` reports the resolved catalog entry (or null) whenever
 * the effective selection changes — the composer uses it for the context ring.
 */
export function ModelSelect({ onSelectionChange }: { onSelectionChange?: (model: ModelInfo | null) => void }) {
  const modelsQuery = useQuery({ queryKey: ["models"], queryFn: () => listModels(), staleTime: 60_000 });
  const [selected, setSelected] = useState<SelectedModel | null>(loadSelectedModel);
  const [open, setOpen] = useState(false);
  const containerRef = useClickOutside<HTMLDivElement>(() => setOpen(false));
  const { viewMode, chatMode } = useViewMode();

  const models = modelsQuery.data ?? [];

  // Swarm mode (work only): models come from the cluster config, so the
  // picker is a read-only indicator — same disabled treatment as the
  // writing-model lock.
  const swarmMode = viewMode === "work" && chatMode === "swarm";

  // Writing-model lock (work mode only; chat never locks). Rides the shared
  // projects query — same key as the sidebar and the project picker, so in
  // practice no extra request fires.
  const projectsQuery = useQuery({
    queryKey: ["projects", viewMode],
    queryFn: () => listProjects(viewMode),
    staleTime: 60_000,
    enabled: viewMode === "work",
  });
  // The "current project" convention shared with the home page: per-mode
  // localStorage pick, legacy key as fallback. Read during render; project
  // switches re-render the composer tree and refresh it.
  const storedProjectId =
    viewMode === "work"
      ? (localStorage.getItem(selectedProjectKey(viewMode)) ?? localStorage.getItem(LEGACY_SELECTED_PROJECT_KEY))
      : null;
  const currentProject = (projectsQuery.data ?? []).find((project) => project.id === storedProjectId);
  const lockedProvider = currentProject?.model_locked_at ? currentProject.writing_model_provider : null;
  const lockedModelId = currentProject?.model_locked_at ? currentProject.writing_model_id : null;
  const isLocked = Boolean(lockedProvider && lockedModelId);
  const lockedDisplayName =
    models.find((m) => m.provider === lockedProvider && m.model_id === lockedModelId)?.display_name ?? lockedModelId;

  // Keep the stored pick valid against the catalog; default to the first model.
  useEffect(() => {
    if (!modelsQuery.data) return;
    const stillValid = models.some((m) => m.provider === selected?.provider && m.model_id === selected.model_id);
    if (stillValid) return;
    const first = models[0] ? toSelected(models[0]) : null;
    setSelected(first);
    saveSelectedModel(first);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modelsQuery.data]);

  // Emit the resolved catalog entry for the current selection. Swarm mode
  // skips the emit: models come from the cluster config there, and the
  // stale single-model pick would mislead the composer's context ring.
  useEffect(() => {
    if (!onSelectionChange || swarmMode) return;
    const resolved = models.find((m) => m.provider === selected?.provider && m.model_id === selected.model_id) ?? null;
    onSelectionChange(resolved);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, modelsQuery.data, onSelectionChange, swarmMode]);

  function toSelected(model: ModelInfo): SelectedModel {
    return { provider: model.provider, model_id: model.model_id, display_name: model.display_name };
  }

  function pick(model: ModelInfo) {
    const next = toSelected(model);
    setSelected(next);
    saveSelectedModel(next);
    setOpen(false);
  }

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={() => {
          if (!swarmMode && !isLocked) setOpen((v) => !v);
        }}
        disabled={swarmMode || isLocked}
        title={
          swarmMode
            ? "集群模型由集群配置决定"
            : isLocked
              ? "已锁定：写作开始后不可更换模型"
              : selected
                ? selected.display_name
                : undefined
        }
        className={`flex max-w-[220px] items-center gap-1 rounded-lg px-2 py-1.5 text-sm transition-colors ${
          swarmMode || isLocked ? "cursor-not-allowed text-ink-secondary" : "text-ink hover:bg-hover"
        }`}
      >
        <span className="truncate">
          {swarmMode ? "集群自动分配" : isLocked ? lockedDisplayName : selected ? selected.display_name : "选择模型"}
        </span>
        {swarmMode ? (
          <span className="shrink-0 rounded bg-hover px-1.5 py-0.5 text-[10px] text-ink-secondary">集群</span>
        ) : isLocked ? (
          <span className="shrink-0 rounded bg-hover px-1.5 py-0.5 text-[10px] text-ink-secondary">已锁定</span>
        ) : (
          <ChevronDownIcon size={15} className="shrink-0 text-ink-secondary" />
        )}
      </button>

      {open && !swarmMode && !isLocked && (
        <div className="absolute bottom-full right-0 z-20 mb-2 w-80 overflow-hidden rounded-xl border border-line bg-white py-1 shadow-[0_8px_30px_rgba(0,0,0,0.08)]">
          {models.length === 0 ? (
            <p className="px-4 py-3 text-sm leading-relaxed text-ink-secondary">
              暂无可用模型，请先在提供商设置中配置 API Key
            </p>
          ) : (
            <ul className="max-h-72 overflow-y-auto">
              {models.map((model) => {
                const isActive = model.provider === selected?.provider && model.model_id === selected.model_id;
                return (
                  <li key={`${model.provider}/${model.model_id}`}>
                    <button
                      type="button"
                      onClick={() => pick(model)}
                      className="flex w-full items-center gap-2 px-3.5 py-2 text-left text-sm text-ink transition-colors hover:bg-hover"
                    >
                      <span className="min-w-0 flex-1">
                        {/* Full model name wraps instead of truncating */}
                        <span className="block break-all leading-snug" title={model.display_name}>
                          {model.display_name}
                        </span>
                        <span className="block truncate text-xs text-ink-secondary">{model.provider}</span>
                      </span>
                      {isActive && <CheckIcon size={15} className="shrink-0 text-ink" />}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
