/**
 * Minimal dev-timing probes for diagnosing chat streaming latency. Everything
 * logs at console.debug level, so default consoles (info and above) stay
 * silent and production behavior is unaffected. Labels carry the
 * conversation/message id, e.g. `send:{messageId}`.
 */

const marks = new Map<string, number>();

/** Record a timestamp under `label`. */
export function mark(label: string): void {
  marks.set(label, performance.now());
}

export function hasMark(label: string): boolean {
  return marks.has(label);
}

/**
 * Log the elapsed ms between the `from` mark and the `to` mark (or now when
 * `to` is omitted). No-ops when either mark is missing, so probes against
 * events that never fired (e.g. history loads) stay quiet.
 */
export function measure(label: string, from: string, to?: string): void {
  const start = marks.get(from);
  if (start === undefined) return;
  const end = to !== undefined ? marks.get(to) : undefined;
  if (to !== undefined && end === undefined) return;
  console.debug(`[timing] ${label}: ${((end ?? performance.now()) - start).toFixed(1)}ms`);
}

interface ChunkWindow {
  count: number;
  windowStart: number;
  setStateTotal: number;
  renderTotal: number;
}

const chunkWindows = new Map<string, ChunkWindow>();

/**
 * Wrap a stream-chunk setState with a probe: every 50 chunks it logs the
 * average inter-chunk interval and the average setState→render cost (time
 * from the update to the next animation frame).
 */
export function probeChunk(messageId: string, update: () => void): void {
  const start = performance.now();
  update();
  const setStateCost = performance.now() - start;

  const win = chunkWindows.get(messageId) ?? { count: 0, windowStart: start, setStateTotal: 0, renderTotal: 0 };
  win.count += 1;
  win.setStateTotal += setStateCost;
  // Approximate render cost: from the update until the next frame commits.
  // Late callbacks simply land in the following window — fine for a dev probe.
  requestAnimationFrame(() => {
    win.renderTotal += performance.now() - start;
  });

  if (win.count % 50 === 0) {
    console.debug(
      `[timing] ${messageId} last 50 chunks: avg interval ${((performance.now() - win.windowStart) / 50).toFixed(1)}ms, ` +
        `avg setState ${(win.setStateTotal / 50).toFixed(2)}ms, avg setState→render ${(win.renderTotal / 50).toFixed(1)}ms`,
    );
    win.windowStart = performance.now();
    win.setStateTotal = 0;
    win.renderTotal = 0;
  }
  chunkWindows.set(messageId, win);
}
