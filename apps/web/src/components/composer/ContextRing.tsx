/**
 * Context usage ring shown next to the model picker in the composer
 * (Kimi-style circular progress, ~20px, SVG stroke-dasharray).
 * Turns orange once usage exceeds 90% of the effective window.
 *
 * Hovering opens a small card (styled like the ModelSelect panel) with two
 * rows: context usage and enabled-plugin counts. The tools row is omitted
 * when the caller passes no `toolsText` (e.g. query failed — the composer
 * must never break because of it).
 */

interface ContextRingProps {
  usedTokens: number;
  /** Effective window already resolved by the caller (min cap applied). */
  contextWindow: number;
  /** Context-cache hits from the previous turn; shown when > 0. */
  cachedTokens?: number;
  /** Second hover-card row, e.g. "工具：技能 3 · MCP 1（已启用）". */
  toolsText?: string | null;
}

const SIZE = 20;
const STROKE = 2;
const RADIUS = (SIZE - STROKE) / 2;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

/** 0 stays "0%"; tiny usage shows "<0.1%" instead of rounding to nothing. */
function formatPercent(ratio: number): string {
  const p = ratio * 100;
  if (p === 0) return "0%";
  if (p < 0.1) return "<0.1%";
  if (p < 1) return `${p.toFixed(1)}%`;
  return `${Math.round(p)}%`;
}

export function ContextRing({ usedTokens, contextWindow, cachedTokens = 0, toolsText = null }: ContextRingProps) {
  const used = Math.max(0, usedTokens);
  const limit = Math.max(1, contextWindow);
  const ratio = Math.min(1, used / limit);
  const percent = formatPercent(ratio);
  const hot = ratio > 0.9;

  return (
    <span className="group relative flex items-center gap-1.5">
      <svg width={SIZE} height={SIZE} viewBox={`0 0 ${SIZE} ${SIZE}`} aria-hidden="true">
        {/* Track */}
        <circle
          cx={SIZE / 2}
          cy={SIZE / 2}
          r={RADIUS}
          fill="none"
          stroke="#ececec"
          strokeWidth={STROKE}
        />
        {/* Progress arc, starting at 12 o'clock */}
        <circle
          cx={SIZE / 2}
          cy={SIZE / 2}
          r={RADIUS}
          fill="none"
          stroke={hot ? "#f97316" : "#1a1a1a"}
          strokeWidth={STROKE}
          strokeLinecap="round"
          strokeDasharray={`${CIRCUMFERENCE * ratio} ${CIRCUMFERENCE}`}
          transform={`rotate(-90 ${SIZE / 2} ${SIZE / 2})`}
        />
      </svg>
      <span className={`text-[11px] ${hot ? "text-orange-500" : "text-ink-secondary"}`}>{percent}</span>

      {/* Hover card: context usage + cache hits + enabled tools */}
      <span className="pointer-events-none absolute bottom-full left-1/2 z-20 mb-2 hidden w-max -translate-x-1/2 flex-col gap-1 rounded-xl border border-line bg-white px-3.5 py-2.5 text-xs whitespace-nowrap shadow-[0_8px_30px_rgba(0,0,0,0.08)] group-hover:flex">
        <span className="text-ink">
          上下文：已用 {used.toLocaleString()} / 上限 {limit.toLocaleString()}（{percent}）
        </span>
        {cachedTokens > 0 && (
          <span className="text-ink-secondary">缓存：命中 {cachedTokens.toLocaleString()} tokens（上一轮）</span>
        )}
        {toolsText && <span className="text-ink-secondary">{toolsText}</span>}
      </span>
    </span>
  );
}
