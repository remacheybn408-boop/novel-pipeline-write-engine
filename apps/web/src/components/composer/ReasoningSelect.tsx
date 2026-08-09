import { useEffect, useState } from "react";
import type { ModelInfo } from "../../lib/api/models";
import { useClickOutside } from "../../lib/hooks/useClickOutside";
import { CheckIcon, ChevronDownIcon } from "../ui/icons";

/**
 * Reasoning-effort picker sitting next to the model picker in the composer.
 * Options come from the selected model's `reasoning_levels` (backend adds
 * this to GET /api/v1/models; models without a profile only get ["auto"],
 * in which case this component renders nothing).
 * The pick is remembered per model in localStorage; values sent to the
 * backend stay the English level names, labels are Chinese.
 */

/** Chinese labels for the standard levels; unknown levels render as-is. */
const LEVEL_LABELS: Record<string, string> = {
  auto: "自动",
  none: "关闭",
  low: "低",
  medium: "中",
  high: "高",
  xhigh: "超高",
  max: "最大",
};

function levelLabel(level: string): string {
  return LEVEL_LABELS[level] ?? level;
}

function storageKey(provider: string, modelId: string): string {
  return `proseforge:reasoning-level:${provider}/${modelId}`;
}

/** Read the remembered level for a model (used by the send call sites). */
export function loadReasoningLevel(provider: string, modelId: string): string | null {
  return localStorage.getItem(storageKey(provider, modelId));
}

export function ReasoningSelect({ model }: { model: ModelInfo | null }) {
  const levels = model?.reasoning_levels ?? ["auto"];
  const [level, setLevel] = useState("auto");
  const [open, setOpen] = useState(false);
  const containerRef = useClickOutside<HTMLDivElement>(() => setOpen(false));

  // Re-validate the remembered pick whenever the model changes.
  useEffect(() => {
    if (!model) return;
    const stored = loadReasoningLevel(model.provider, model.model_id);
    setLevel(stored && levels.includes(stored) ? stored : "auto");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [model?.provider, model?.model_id, model?.reasoning_levels]);

  // Nothing to choose for models that only support auto.
  if (!model || levels.length <= 1) return null;

  function pick(next: string) {
    setLevel(next);
    if (model) localStorage.setItem(storageKey(model.provider, model.model_id), next);
    setOpen(false);
  }

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        title="思考强度"
        className="flex items-center gap-1 rounded-lg px-2 py-1.5 text-sm text-ink transition-colors hover:bg-hover"
      >
        <span>思考：{levelLabel(level)}</span>
        <ChevronDownIcon size={15} className="shrink-0 text-ink-secondary" />
      </button>

      {open && (
        <div className="absolute bottom-full right-0 z-20 mb-2 w-40 overflow-hidden rounded-xl border border-line bg-white py-1 shadow-[0_8px_30px_rgba(0,0,0,0.08)]">
          <ul className="max-h-72 overflow-y-auto">
            {levels.map((option) => (
              <li key={option}>
                <button
                  type="button"
                  onClick={() => pick(option)}
                  className="flex w-full items-center gap-2 px-3.5 py-2 text-left text-sm text-ink transition-colors hover:bg-hover"
                >
                  <span className="min-w-0 flex-1">{levelLabel(option)}</span>
                  {option === level && <CheckIcon size={15} className="shrink-0 text-ink" />}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
