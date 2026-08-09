/**
 * Log endpoints — proseforge/api/routes/logs.py.
 *
 *   GET /api/v1/logs/errors/download -> text/markdown attachment
 *
 * The shared request() wrapper is JSON-oriented, so the download uses fetch
 * directly (same-origin cookie auth), following agentRuns.ts exportRunZip.
 */
import { ApiError } from "./client";

/** Trigger a browser download of the error log Markdown report. */
export async function downloadErrorLogs(): Promise<void> {
  let response: Response;
  try {
    response = await fetch("/api/v1/logs/errors/download", { credentials: "include" });
  } catch {
    throw new ApiError(0, "网络连接失败，请确认后端服务已启动");
  }
  if (!response.ok) throw new ApiError(response.status, "下载失败，请稍后重试");
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  // Local date (not toISOString's UTC date, which rolls a day early near midnight).
  const now = new Date();
  const date = `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, "0")}${String(now.getDate()).padStart(2, "0")}`;
  anchor.download = `proseforge-error-logs-${date}.md`;
  anchor.click();
  URL.revokeObjectURL(url);
}
