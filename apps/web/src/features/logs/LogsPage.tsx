import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { downloadErrorLogs } from "../../lib/api/logs";
import { ApiError } from "../../lib/api/client";
import { DownloadIcon, FileTextIcon, XIcon } from "../../components/ui/icons";

function errorText(err: unknown): string {
  return err instanceof ApiError ? err.message : "操作失败，请稍后重试";
}

/**
 * Log page: explains where the backend writes app.log and offers the error
 * log Markdown report download. Entry: the sidebar user menu (work mode).
 */
export function LogsPage() {
  const navigate = useNavigate();
  const [downloading, setDownloading] = useState(false);
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null);

  async function handleDownload() {
    if (downloading) return;
    setDownloading(true);
    setNotice(null);
    try {
      await downloadErrorLogs();
      setNotice({ ok: true, text: "错误日志报告已开始下载" });
    } catch (err) {
      setNotice({ ok: false, text: `下载失败：${errorText(err)}` });
    } finally {
      setDownloading(false);
    }
  }

  return (
    <div className="w-full px-8 py-10">
      {/* Close: back to where the user came from */}
      <button
        type="button"
        title="关闭日志页"
        aria-label="关闭日志页"
        onClick={() => navigate(-1)}
        className="fixed right-6 top-6 flex h-9 w-9 items-center justify-center rounded-lg text-ink-secondary transition-colors hover:bg-hover hover:text-ink"
      >
        <XIcon size={20} />
      </button>

      <h1 className="mb-1 text-2xl font-bold text-ink">日志</h1>
      <p className="mb-6 text-sm text-ink-secondary">后端运行日志与错误报告</p>

      <div className="max-w-[720px]">
        {notice && <p className={`mb-3 text-sm ${notice.ok ? "text-emerald-600" : "text-red-600"}`}>{notice.text}</p>}

        <div className="rounded-2xl border border-line bg-white p-5">
          <div className="flex items-start gap-3">
            <span className="mt-0.5 shrink-0 text-ink-secondary">
              <FileTextIcon size={18} />
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium text-ink">运行日志</p>
              <p className="mt-1 text-xs leading-5 text-ink-secondary">
                后端会把运行日志写入数据目录下的 logs/app.log（自动轮转，最多保留 3 个历史文件）。
                遇到异常时可以下载错误日志报告，其中只包含 ERROR / CRITICAL 级别的记录及其堆栈信息，
                便于排查或反馈问题。
              </p>
              <button
                type="button"
                disabled={downloading}
                onClick={() => void handleDownload()}
                className="mt-4 flex items-center gap-1.5 rounded-xl bg-ink px-4 py-2 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
              >
                <DownloadIcon size={15} />
                {downloading ? "下载中…" : "下载错误日志 (.md)"}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
