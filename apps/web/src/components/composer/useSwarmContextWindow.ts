/**
 * Swarm-mode context window for the composer usage ring.
 *
 * In swarm mode the model picker is a read-only indicator and its stale
 * localStorage pick must not drive the ring — the window comes from the
 * effective cluster config (project override > global) resolved against
 * the model catalog. Any fetch failure or missing config returns null and
 * the composer falls back to its single-model logic.
 */
import { useQuery } from "@tanstack/react-query";
import { listModels } from "../../lib/api/models";
import { getProjectClusterConfig } from "../../lib/api/projects";
import { getClusterConfig } from "../../lib/api/settings";
import { LEGACY_SELECTED_PROJECT_KEY, selectedProjectKey, useViewMode } from "../../app/ViewModeContext";
import { loadSelectedModel } from "./ModelSelect";
import { swarmContextWindow, type ModelRef } from "./swarmWindow";

export function useSwarmContextWindow(): number | null {
  const { viewMode, chatMode } = useViewMode();
  const swarmActive = viewMode === "work" && chatMode === "swarm";

  // Same query keys as the model picker / cluster config pages, so the
  // cache is shared and their invalidations refresh the ring too.
  const modelsQuery = useQuery({
    queryKey: ["models"],
    queryFn: () => listModels(),
    staleTime: 60_000,
    enabled: swarmActive,
  });

  // Current project convention shared with ModelSelect: per-mode
  // localStorage pick, legacy key as fallback; read during render.
  const projectId = swarmActive
    ? (localStorage.getItem(selectedProjectKey(viewMode)) ?? localStorage.getItem(LEGACY_SELECTED_PROJECT_KEY))
    : null;
  const projectClusterQuery = useQuery({
    queryKey: ["project-cluster-config", projectId],
    queryFn: () => getProjectClusterConfig(projectId!),
    enabled: swarmActive && Boolean(projectId),
    retry: false,
  });
  const globalClusterQuery = useQuery({
    queryKey: ["cluster-config"],
    queryFn: getClusterConfig,
    enabled: swarmActive && !projectClusterQuery.data,
    retry: false,
  });

  if (!swarmActive) return null;
  const config = projectClusterQuery.data ?? globalClusterQuery.data;
  if (!config || !modelsQuery.data) return null;
  const stored = loadSelectedModel();
  const requested: ModelRef | null = stored ? { provider: stored.provider, model: stored.model_id } : null;
  return swarmContextWindow(config, modelsQuery.data, requested);
}
