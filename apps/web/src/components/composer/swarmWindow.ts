/**
 * Pure swarm-mode context window resolution for the composer usage ring.
 *
 * Mirrors the backend's cluster role resolution
 * (proseforge/application/models/cluster_config.py resolve_from_config,
 * cluster branch) so the ring shows the real window of the five seats
 * instead of the stale single-model pick kept in localStorage.
 */
import type { ModelInfo } from "../../lib/api/models";
import type { ClusterRoles } from "../../lib/api/settings";

export interface ModelRef {
  provider: string;
  model: string;
}

/** Minimal cluster-config shape shared by the global and per-project endpoints. */
export interface SwarmClusterConfig {
  mode: "normal" | "cluster";
  roles: ClusterRoles;
}

function explicitRole(role: ClusterRoles[keyof ClusterRoles] | undefined): ModelRef | null {
  return !role || role === "auto" ? null : role;
}

function sameRef(a: ModelRef, b: ModelRef): boolean {
  return a.provider === b.provider && a.model === b.model;
}

/**
 * Resolve the five seat refs exactly like the backend in cluster mode:
 * explicit refs that dropped out of the pool degrade like "auto"; auto
 * write falls back to the requested model when runnable, else the first
 * pool model; orchestrator/analyst follow write; review/revise prefer the
 * first pool model that is not the write model. Returns null in normal
 * mode or with an empty pool — the caller falls back to its legacy logic.
 */
export function resolveSwarmRoleRefs(
  config: SwarmClusterConfig,
  models: ModelInfo[],
  requested: ModelRef | null,
): ModelRef[] | null {
  if (config.mode !== "cluster") return null;
  // Sorted like the backend's available pool for deterministic auto-picks.
  const pool: ModelRef[] = models
    .map((model) => ({ provider: model.provider, model: model.model_id }))
    .sort((a, b) => (a.provider === b.provider ? a.model.localeCompare(b.model) : a.provider.localeCompare(b.provider)));
  if (pool.length === 0) return null;

  const inPool = (ref: ModelRef) => pool.some((entry) => sameRef(entry, ref));
  const explicit = (role: ClusterRoles[keyof ClusterRoles] | undefined, fallback: ModelRef): ModelRef => {
    const ref = explicitRole(role);
    return ref && inPool(ref) ? ref : fallback;
  };

  const requestedRef = requested && inPool(requested) ? requested : pool[0];
  const write = explicit(config.roles.write, requestedRef);
  const orchestrator = explicit(config.roles.orchestrator, write);
  const analyst = explicit(config.roles.analyst, orchestrator);
  const backup = pool.find((ref) => !sameRef(ref, write)) ?? write;
  const review = explicit(config.roles.review, backup);
  const revise = explicit(config.roles.revise, backup);
  return [orchestrator, analyst, write, review, revise];
}

/**
 * The smallest context_window across the five resolved seats (models
 * without a known window are skipped). Null when nothing is resolvable —
 * the ring must then fall back to the single-model logic.
 */
export function swarmContextWindow(
  config: SwarmClusterConfig,
  models: ModelInfo[],
  requested: ModelRef | null,
): number | null {
  const refs = resolveSwarmRoleRefs(config, models, requested);
  if (!refs) return null;
  const windows = refs
    .map((ref) => models.find((model) => model.provider === ref.provider && model.model_id === ref.model)?.context_window)
    .filter((window): window is number => typeof window === "number" && window > 0);
  return windows.length > 0 ? Math.min(...windows) : null;
}
