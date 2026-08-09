import { memo, useCallback, useEffect, useMemo, useRef, useState, isValidElement, type ReactNode } from "react";
import { useLocation, useParams } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import {
  ACTIVE_STATUSES,
  continueMessage,
  editMessage,
  getMessageRetrieval,
  listBranches,
  listConversations,
  listMessages,
  regenerateMessage,
  retryMessage,
  sendMessage,
  stopMessage,
  subscribeConversationEvents,
  type ChatMessage,
  type MessageStatus,
  type RegenerateResult,
} from "../../lib/api/conversations";
import { ApiError } from "../../lib/api/client";
import { uploadAttachmentIds } from "../../lib/api/files";
import { listUsageRecords } from "../../lib/api/usage";
import { hasMark, mark, measure, probeChunk } from "../../lib/devtiming";
import { useSmoothText } from "../../lib/hooks/useSmoothText";
import { stripInternalMarkers } from "../../lib/text";
import { uuid } from "../../lib/uuid";
import { useViewMode } from "../../app/ViewModeContext";
import { controlAgentRun, exportRunZip, getAgentRun } from "../../lib/api/agentRuns";
import { getChapterContent } from "../../lib/api/projects";
import { Composer } from "../../components/composer/Composer";
import { SwarmWorkbench } from "../../components/chat/SwarmWorkbench";
import { WritingProgressPanel } from "../../components/chat/WritingProgressPanel";
import { loadSelectedModel } from "../../components/composer/ModelSelect";
import { loadReasoningLevel } from "../../components/composer/ReasoningSelect";
import { ChevronDownIcon, DownloadIcon, FileTextIcon } from "../../components/ui/icons";

/**
 * Markdown link renderer: download endpoints (/api/v1/files/...) become a
 * file card instead of a bare link. The anchor keeps its href and download
 * semantics — same-origin attachment, no preventDefault, no target=_blank.
 */
function MarkdownLink({ href, children }: { href?: string; children?: ReactNode }) {
  if (href?.startsWith("/api/v1/files/")) {
    return (
      <a
        href={href}
        download
        className="my-2 flex items-center gap-3 rounded-xl border border-line bg-white/80 px-4 py-3 backdrop-blur-sm transition-colors hover:bg-white"
      >
        <FileTextIcon size={18} className="shrink-0 text-ink-secondary" />
        <span className="min-w-0 flex-1 break-all text-sm text-ink">{children}</span>
        <span className="flex shrink-0 items-center gap-1 text-sm text-ink-secondary">
          下载
          <DownloadIcon size={15} />
        </span>
      </a>
    );
  }
  return (
    <a href={href} rel="noreferrer">
      {children}
    </a>
  );
}

// ---------------------------------------------------------------------------
// Code block toolbar (copy / download) — the fallback for when the model
// does not follow the ```file: protocol; every block stays downloadable.
// ---------------------------------------------------------------------------

/** Recursively flatten React children (strings / arrays / elements) to text. */
function extractText(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(extractText).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return extractText(node.props.children);
  return "";
}

const EXT_BY_LANGUAGE: Record<string, string> = {
  markdown: ".md",
  md: ".md",
  python: ".py",
  py: ".py",
  javascript: ".js",
  js: ".js",
  typescript: ".ts",
  ts: ".ts",
  json: ".json",
  html: ".html",
  css: ".css",
  csv: ".csv",
  txt: ".txt",
  plain: ".txt",
  text: ".txt",
};

/** file:<name> info strings (model protocol mid-stream) use the real name. */
function resolveFileName(language: string, ordinal: number): string {
  if (language.startsWith("file:")) {
    const name = language.slice("file:".length).trim();
    return name || `block-${ordinal}.txt`;
  }
  const ext = EXT_BY_LANGUAGE[language.toLowerCase()] ?? ".txt";
  return `block-${ordinal}${ext}`;
}

async function copyText(text: string): Promise<void> {
  // navigator.clipboard is unavailable in insecure contexts (http intranet);
  // lib/uuid.ts has the same fallback pattern.
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  try {
    document.execCommand("copy");
  } finally {
    textarea.remove();
  }
}

function downloadText(text: string, filename: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

const toolbarButtonClass =
  "rounded-md border border-line bg-white/80 px-2 py-1 text-xs text-ink-secondary backdrop-blur-sm transition-colors hover:bg-white hover:text-ink";

/** Swarm quick-action buttons under a settled run placeholder message. */
const quickActionClass =
  "rounded-lg border border-line bg-white px-3 py-1.5 text-xs text-ink transition-colors hover:bg-hover disabled:cursor-not-allowed disabled:opacity-50";

function MarkdownPre({ children }: { children?: ReactNode }) {
  const [copied, setCopied] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const copyTimer = useRef<number>(0);

  useEffect(() => () => window.clearTimeout(copyTimer.current), []);

  // The child is typically a single <code class="language-xxx"> element.
  let language = "";
  if (isValidElement<{ className?: string }>(children)) {
    language = /language-(\S+)/.exec(children.props.className ?? "")?.[1] ?? "";
  }
  const text = extractText(children);

  function handleCopy() {
    void copyText(text).then(() => {
      setCopied(true);
      window.clearTimeout(copyTimer.current);
      copyTimer.current = window.setTimeout(() => setCopied(false), 1500);
    });
  }

  function handleDownload() {
    // Block ordinal within this message, resolved from the DOM at click time
    // so it stays correct across streaming re-renders.
    const root = wrapperRef.current?.closest(".markdown");
    const blocks = root ? Array.from(root.querySelectorAll(".markdown-pre")) : [];
    const ordinal = Math.max(1, blocks.indexOf(wrapperRef.current as HTMLDivElement) + 1);
    downloadText(text, resolveFileName(language, ordinal));
  }

  return (
    <div ref={wrapperRef} className="markdown-pre group relative">
      <pre>{children}</pre>
      <div className="absolute right-2 top-2 flex gap-1 opacity-0 transition-opacity group-hover:opacity-100">
        <button type="button" onClick={handleCopy} className={toolbarButtonClass}>
          {copied ? "已复制" : "复制"}
        </button>
        <button type="button" onClick={handleDownload} className={toolbarButtonClass}>
          下载
        </button>
      </div>
    </div>
  );
}

const MARKDOWN_COMPONENTS = { a: MarkdownLink, pre: MarkdownPre };

/** Passed via navigate() state from the home page right after creating the conversation. */
interface NavigationState {
  branchId?: string;
  assistantMessageId?: string;
}

/** A send resolves after the user may have switched conversations; its
 *  results only apply while the route still shows the conversation it was
 *  sent from. */
export function isCurrentConversation(currentId: string | undefined, sentId: string): boolean {
  return currentId === sentId;
}

/** Per-message streamed chunks, keyed by chunk index so replays/dedupes are safe. */
type StreamChunks = Record<string, Record<number, string>>;

/** Live view of one tool call (message.tool.status), keyed by call_id. */
interface ToolCallState {
  tool: string;
  label: string;
  status: "started" | "done" | "failed";
  durationMs?: number;
  errorClass?: string;
}

/** message_id -> call_id -> call state, for the bubble status lines. */
type ToolCalls = Record<string, Record<string, ToolCallState>>;

/** error_class -> human-readable reason for failed tool calls. */
const TOOL_ERROR_TEXT: Record<string, string> = {
  timeout: "超时",
  rate_limited: "请求过多",
  circuit_breaker: "请求过多，稍后再试",
  validation: "参数错误",
  policy_denied: "未启用",
  upstream: "服务异常",
};

function formatDuration(durationMs: number): string {
  return durationMs >= 1000 ? `${(durationMs / 1000).toFixed(1)}s` : `${durationMs}ms`;
}

/** Per-tool label overrides for the status line (backend label is the fallback). */
const TOOL_STATUS_LABEL: Record<string, string> = {
  fetch_document: "📄 正在读取文档",
  run_code: "⏳ 正在计算",
};

function toolLabel(call: ToolCallState): string {
  return TOOL_STATUS_LABEL[call.tool] ?? call.label;
}

function joinChunks(chunks: Record<number, string> | undefined): string {
  if (!chunks) return "";
  return Object.keys(chunks)
    .map(Number)
    .sort((a, b) => a - b)
    .map((index) => chunks[index])
    .join("");
}

function TypingIndicator() {
  return (
    <div className="flex items-center gap-2 py-1 text-sm text-ink-secondary">
      <span className="flex items-center gap-1">
        <span className="typing-dot" />
        <span className="typing-dot" />
        <span className="typing-dot" />
      </span>
      正在思考…
    </div>
  );
}

/**
 * "参考来源" disclosure for finished assistant messages, gated by data
 * existence rather than project mode: the immutable retrieval snapshot is
 * fetched once on mount (cached per message id) and a 404 (no snapshot) or
 * any failure hides the entry silently.
 */
function RetrievalToggle({ conversationId, messageId }: { conversationId: string; messageId: string }) {
  const [open, setOpen] = useState(false);
  const retrievalQuery = useQuery({
    queryKey: ["message-retrieval", messageId],
    queryFn: () => getMessageRetrieval(conversationId, messageId),
    retry: false,
    staleTime: Infinity,
  });

  // Existence probe: until the snapshot is confirmed there is no entry.
  if (retrievalQuery.isPending || retrievalQuery.isError) return null;
  const retrieval = retrievalQuery.data;

  return (
    <div className="mt-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1 text-xs text-ink-secondary transition-colors hover:text-ink"
      >
        <ChevronDownIcon size={13} className={`transition-transform ${open ? "rotate-180" : ""}`} />
        {retrieval ? `参考了 ${retrieval.chunks.length} 条设定与证据 · ${retrieval.elapsed_ms}ms` : "参考来源"}
      </button>
      {open && retrieval && (
        <div className="mt-1.5 rounded-xl border border-line bg-white px-4 py-3 text-xs">
          <p className="text-ink-secondary">检索意图：{retrieval.intent}</p>
          <p className="mt-0.5 truncate text-ink-secondary" title={retrieval.query_text}>
            查询：{retrieval.query_text}
          </p>
          {retrieval.chunks.length > 0 && (
            <ul className="mt-2 flex flex-col gap-1">
              {retrieval.chunks.map((chunk, index) => (
                <li key={index} className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate text-ink" title={chunk.document_title}>
                    {chunk.chapter_no !== null && `【第${chunk.chapter_no}章】`}
                    {chunk.document_title}
                  </span>
                  {chunk.expanded && (
                    <span className="shrink-0 rounded bg-hover px-1.5 py-0.5 text-[10px] text-ink-secondary">邻块</span>
                  )}
                  <span className="shrink-0 text-ink-secondary">{chunk.score.toFixed(2)}</span>
                </li>
              ))}
            </ul>
          )}
          {retrieval.trimmed.map((item, index) => (
            <p key={index} className="mt-1.5 text-ink-secondary" title={item.reason}>
              因预算裁剪：{item.section}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Quick actions under a settled swarm run placeholder: ZIP export always,
 * chapter download when the run produced a chapter, retry on failure.
 */
function SwarmActionRow({
  message,
  projectId,
  onRetry,
  onError,
}: {
  message: ChatMessage;
  projectId: string | null;
  onRetry: (runId: string) => void;
  onError: (text: string) => void;
}) {
  const runId = message.agent_run_id as string;
  // The message is settled, so the run is terminal: cache the lookup long.
  const runQuery = useQuery({
    queryKey: ["agent-run", runId],
    queryFn: () => getAgentRun(runId),
    staleTime: 300_000,
    retry: false,
  });
  const [busy, setBusy] = useState(false);
  const chapterId = runQuery.data?.chapter_id ?? null;

  async function downloadZip() {
    if (busy) return;
    setBusy(true);
    try {
      await exportRunZip(runId);
    } catch (err) {
      onError(`导出失败：${err instanceof ApiError ? err.message : "请稍后重试"}`);
    } finally {
      setBusy(false);
    }
  }

  async function downloadChapter() {
    if (busy || !projectId || !chapterId) return;
    setBusy(true);
    try {
      const chapter = await getChapterContent(projectId, chapterId);
      const safeTitle = chapter.title.replace(/[\\/:*?"<>|]/g, "-") || "未命名";
      downloadText(chapter.content, `第${chapter.chapter_no}章-${safeTitle}.md`);
    } catch (err) {
      onError(`章节下载失败：${err instanceof ApiError ? err.message : "请稍后重试"}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-2 flex items-center gap-2">
      {message.status === "COMPLETED" && (
        <>
          <button type="button" disabled={busy} onClick={() => void downloadZip()} className={quickActionClass}>
            下载 ZIP
          </button>
          {chapterId && projectId && (
            <button type="button" disabled={busy} onClick={() => void downloadChapter()} className={quickActionClass}>
              下载本章
            </button>
          )}
        </>
      )}
      {message.status === "FAILED" && (
        <button type="button" disabled={busy} onClick={() => onRetry(runId)} className={quickActionClass}>
          重试
        </button>
      )}
    </div>
  );
}

/**
 * Message controls for a normal (non-swarm) assistant message: stop while the
 * reply streams, retry/continue on FAILED/PARTIAL, regenerate once settled.
 * Swarm run placeholders never render this row — they are controlled through
 * the agent run (SwarmActionRow / workbench), not the single-model endpoints.
 */
function AssistantActionRow({
  conversationId,
  message,
  isStreaming,
  onStreamStart,
  onSettled,
  onError,
}: {
  conversationId: string;
  message: ChatMessage;
  isStreaming: boolean;
  /** A generation was (re-)enqueued for this id; arm the streaming bubble. */
  onStreamStart: (messageId: string) => void;
  /** Authoritative refetch after the mutation lands. */
  onSettled: () => void;
  onError: (text: string) => void;
}) {
  type ControlAction = "stop" | "retry" | "continue" | "regenerate";
  type ControlResult = { id: string; status: MessageStatus } | RegenerateResult;
  const controlMutation = useMutation({
    mutationFn: (action: ControlAction): Promise<ControlResult> => {
      if (action === "stop") return stopMessage(message.id);
      if (action === "retry") return retryMessage(message.id);
      if (action === "continue") return continueMessage(message.id);
      return regenerateMessage(conversationId, message.id);
    },
    onSuccess: (result, action) => {
      // Regenerate creates a NEW assistant message; retry/continue re-enqueue
      // the same one. Stop settles via the message.failed SSE event.
      if (action === "regenerate") onStreamStart((result as RegenerateResult).message_id);
      else if (action === "retry" || action === "continue") onStreamStart(message.id);
      onSettled();
    },
    onError: (err) => onError(err instanceof ApiError ? err.message : "操作失败，请稍后重试"),
  });

  const busy = controlMutation.isPending;

  if (isStreaming) {
    return (
      <div className="mt-2 flex items-center gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => controlMutation.mutate("stop")}
          className={quickActionClass}
        >
          停止
        </button>
      </div>
    );
  }

  const retryable = message.status === "FAILED" || message.status === "PARTIAL";
  if (!retryable && message.status !== "COMPLETED" && message.status !== "CANCELLED") return null;

  return (
    <div className="mt-2 flex items-center gap-2">
      {message.status === "PARTIAL" && (
        <button type="button" disabled={busy} onClick={() => controlMutation.mutate("continue")} className={quickActionClass}>
          续写
        </button>
      )}
      {retryable && (
        <button type="button" disabled={busy} onClick={() => controlMutation.mutate("retry")} className={quickActionClass}>
          重试
        </button>
      )}
      <button type="button" disabled={busy} onClick={() => controlMutation.mutate("regenerate")} className={quickActionClass}>
        重新生成
      </button>
    </div>
  );
}

/**
 * User bubble with an edit-and-fork entry: saving forks a new branch carrying
 * the edited content and the page switches to it (no reply is auto-enqueued
 * by the backend; the user continues from the forked branch).
 */
function UserMessageBubble({
  message,
  pending,
  onEdit,
}: {
  message: ChatMessage;
  /** An edit submission for this message is in flight. */
  pending: boolean;
  onEdit: (content: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(message.content);

  if (editing) {
    return (
      <div className="flex justify-end">
        <div className="w-full max-w-[75%]">
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            rows={Math.min(10, Math.max(2, draft.split("\n").length))}
            className="w-full resize-y rounded-2xl border border-line bg-white px-4 py-2.5 text-sm leading-relaxed text-ink focus:outline-none"
          />
          <div className="mt-2 flex justify-end gap-2">
            <button
              type="button"
              disabled={pending}
              onClick={() => {
                setEditing(false);
                setDraft(message.content);
              }}
              className={quickActionClass}
            >
              取消
            </button>
            <button
              type="button"
              disabled={pending || !draft.trim()}
              onClick={() => onEdit(draft.trim())}
              className={quickActionClass}
            >
              保存并分叉
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="group flex justify-end">
      <div className="flex max-w-[75%] flex-col items-end">
        <div className="whitespace-pre-wrap rounded-2xl bg-ink px-4 py-2.5 text-sm leading-relaxed text-white">
          {message.content}
        </div>
        {(message.attachments?.length ?? 0) > 0 && (
          <div className="mt-1.5 flex flex-wrap justify-end gap-1.5">
            {message.attachments!.map((attachment) => (
              <a
                key={attachment.id}
                href={`/api/v1/files/${attachment.id}/download`}
                download
                title={`下载附件 ${attachment.filename}`}
                className="flex items-center gap-1.5 rounded-full border border-line bg-white px-2.5 py-1 text-xs text-ink-secondary transition-colors hover:text-ink"
              >
                <FileTextIcon size={13} />
                <span className="max-w-[200px] truncate">{attachment.filename}</span>
                <DownloadIcon size={12} />
              </a>
            ))}
          </div>
        )}
        <button
          type="button"
          onClick={() => {
            setDraft(message.content);
            setEditing(true);
          }}
          className="mt-1 text-xs text-ink-secondary opacity-0 transition-opacity hover:text-ink group-hover:opacity-100"
        >
          编辑
        </button>
      </div>
    </div>
  );
}

/**
 * Memoized so a streamed chunk only re-renders the bubble that owns it:
 * history bubbles keep stable props (`message` identity from state, `text` by
 * string equality) and skip reconciliation entirely.
 */
const AssistantMessage = memo(function AssistantMessage({
  message,
  text,
  isStreaming,
  isThinking = false,
  searchQuery,
  toolCalls,
  conversationId,
  showRetrieval = false,
}: {
  message: ChatMessage;
  /** Fully assembled display text (SSE-joined while streaming, else content). */
  text: string;
  isStreaming: boolean;
  /** reasoning.delta is arriving: the model is still thinking, no body yet. */
  isThinking?: boolean;
  /** Web search in progress (message.searching); undefined when idle. */
  searchQuery?: string;
  /** Tool call states for this message (message.tool.status), keyed by call_id. */
  toolCalls?: Record<string, ToolCallState>;
  /** Owning conversation (retrieval snapshot lookup). */
  conversationId: string;
  /** Show the "参考来源" disclosure under finished messages (visible only
   *  when a retrieval snapshot actually exists — see RetrievalToggle). */
  showRetrieval?: boolean;
}) {
  // Streaming text reveals through the typewriter hook; finished messages and
  // the degraded-mode polling fallback render their text in full.
  const smoothText = useSmoothText(text);
  // Strip backend protocol markers (e.g. <!-- search:done -->) from both the
  // streaming and the finished rendering paths; display-only.
  const rendered = useMemo(
    () => stripInternalMarkers(isStreaming ? smoothText : text),
    [isStreaming, smoothText, text],
  );

  // Dev timing: first render of this bubble after its first SSE event.
  const mountProbedRef = useRef(false);
  useEffect(() => {
    if (mountProbedRef.current) return;
    mountProbedRef.current = true;
    measure(`${message.id} first-event→first-render`, `first-event:${message.id}`);
  }, [message.id]);

  return (
    <div className="text-[15px] text-ink">
      {searchQuery !== undefined && (
        <div className="mb-1 flex items-center gap-1.5 text-xs text-ink-secondary">
          🔍 正在搜索：{searchQuery}
        </div>
      )}
      {/* Tool call status lines: spinner while running, duration when done,
          mapped reason when failed. Compact by design (no expand panel yet). */}
      {toolCalls &&
        Object.entries(toolCalls).map(([callId, call]) => (
          <div key={callId} className="mb-1 flex items-center gap-1.5 text-xs text-ink-secondary">
            {call.status === "started" && (
              <>
                <span className="typing-dot" />
                {toolLabel(call)}
              </>
            )}
            {call.status === "done" && (
              <>
                {toolLabel(call)}
                {call.durationMs !== undefined && ` · ${formatDuration(call.durationMs)}`}
              </>
            )}
            {call.status === "failed" && (
              <>
                {toolLabel(call)}失败：{(call.errorClass && TOOL_ERROR_TEXT[call.errorClass]) ?? "服务异常"}
              </>
            )}
          </div>
        ))}
      {isThinking && (
        <div className="mb-1 flex items-center gap-1.5 text-xs text-ink-secondary">
          <span className="typing-dot" />
          正在思考…
        </div>
      )}
      {rendered ? (
        <div className="markdown">
          <ReactMarkdown components={MARKDOWN_COMPONENTS}>{rendered}</ReactMarkdown>
          {isStreaming && <span className="stream-cursor" />}
        </div>
      ) : isStreaming && !isThinking ? (
        // Swarm placeholder: the run works in the background; the summary is
        // written back as normal markdown when it finishes.
        message.agent_run_id ? (
          <div className="flex animate-pulse items-center gap-2 py-1 text-sm text-ink-secondary">
            <span className="typing-dot" />
            集群工作中，进度见右侧工作台
          </div>
        ) : (
          <TypingIndicator />
        )
      ) : null}
      {message.status === "FAILED" && <p className="mt-2 text-sm text-red-600">生成失败，请稍后重试</p>}
      {message.status === "PARTIAL" && <p className="mt-2 text-sm text-ink-secondary">回答已中断</p>}
      {message.status === "CANCELLED" && <p className="mt-2 text-sm text-ink-secondary">已取消</p>}
      {/* Retrieval provenance: finished messages with a snapshot, any mode. */}
      {showRetrieval && !isStreaming && <RetrievalToggle conversationId={conversationId} messageId={message.id} />}
    </div>
  );
});

export function ChatPage() {
  const { conversationId } = useParams<{ conversationId: string }>();
  const location = useLocation();
  const navState = (location.state ?? {}) as NavigationState;
  const { viewMode, chatMode } = useViewMode();
  const swarmActive = viewMode === "work" && chatMode === "swarm";

  const [branchId, setBranchId] = useState<string | null>(navState.branchId ?? null);
  const [messages, setMessages] = useState<ChatMessage[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [streamChunks, setStreamChunks] = useState<StreamChunks>({});
  // message_id -> fully joined stream text, kept after the terminal event.
  // The local messages state still holds the pre-stream content ("" for a
  // fresh assistant message) until refreshMessages lands one RTT later, so
  // without this cache the bubble would flash empty + typing in between.
  const [lastStreamedText, setLastStreamedText] = useState<Record<string, string>>({});
  const [streamingIds, setStreamingIds] = useState<ReadonlySet<string>>(
    () => new Set(navState.assistantMessageId ? [navState.assistantMessageId] : []),
  );
  // Messages currently emitting reasoning.delta (model still thinking).
  const [thinkingIds, setThinkingIds] = useState<ReadonlySet<string>>(() => new Set());
  // message_id -> search query while a web search runs (message.searching).
  const [searchingQueries, setSearchingQueries] = useState<Record<string, string>>({});
  // message_id -> call_id -> tool call state (message.tool.status).
  const [toolCalls, setToolCalls] = useState<ToolCalls>({});
  const [degraded, setDegraded] = useState(false);
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<File[]>([]);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  // Last input_tokens reported by usage.updated events (drives the composer's
  // context usage ring).
  const [usedTokens, setUsedTokens] = useState(0);
  // Server-side context-cache hits from the latest final usage record.
  const [cachedTokens, setCachedTokens] = useState(0);

  const scrollRef = useRef<HTMLDivElement>(null);
  // True while the user is near the bottom; auto-follow only applies then.
  const atBottomRef = useRef(true);
  const scrollRafRef = useRef(0);
  const [showJumpToBottom, setShowJumpToBottom] = useState(false);
  // Latest route conversation id for async callbacks: a send landing after
  // the user switched conversations must not overwrite the new view.
  const conversationIdRef = useRef(conversationId);
  conversationIdRef.current = conversationId;
  // Latest streamChunks for the terminal-event handler: joining inside the
  // setStreamChunks updater would be a side effect, so read from a ref.
  const streamChunksRef = useRef<StreamChunks>({});
  streamChunksRef.current = streamChunks;
  // Bumped after a run retry to re-arm the workbench's polling loop (it
  // stopped at the terminal status and does not remount with the same key).
  const [workbenchPollGeneration, setWorkbenchPollGeneration] = useState(0);

  // Render-time state reset (React docs: "adjusting state when props
  // change"). The component does NOT unmount when the route switches between
  // conversations, so every per-conversation state must be re-seeded here —
  // otherwise the stale branchId would make the branch-resolve effect skip
  // and the new conversation's messages would be fetched with the old branch
  // (404 "conversation or branch not found" in production). navState only
  // applies to the navigation that carried it (fresh create), so switching
  // away and back cannot leak it.
  const [prevConversationId, setPrevConversationId] = useState(conversationId);
  if (conversationId !== prevConversationId) {
    setPrevConversationId(conversationId);
    setBranchId(navState.branchId ?? null);
    setMessages(null);
    setLoadError(null);
    setStreamChunks({});
    setLastStreamedText({});
    setStreamingIds(new Set(navState.assistantMessageId ? [navState.assistantMessageId] : []));
    setThinkingIds(new Set());
    setSearchingQueries({});
    setToolCalls({});
    setDegraded(false);
    setSendError(null);
    setUsedTokens(0);
    setCachedTokens(0);
  }

  // Resolve the branch when we did not arrive with one (e.g. deep link):
  // the default branch is the root (no parent) ACTIVE branch.
  useEffect(() => {
    if (!conversationId || branchId) return;
    let cancelled = false;
    listBranches(conversationId)
      .then((branches) => {
        if (cancelled) return;
        const root = branches.find((b) => b.parent_branch_id === null && b.status === "ACTIVE") ?? branches[0];
        if (root) setBranchId(root.id);
        else setLoadError("未找到会话分支");
      })
      .catch((err: unknown) => {
        if (!cancelled) setLoadError(err instanceof ApiError ? err.message : "会话加载失败");
      });
    return () => {
      cancelled = true;
    };
  }, [conversationId, branchId]);

  const refreshMessages = useCallback(async () => {
    if (!conversationId || !branchId) return;
    const list = await listMessages(conversationId, branchId);
    setMessages(list);
    // Reconcile against the authoritative list: a lost message.completed event
    // would otherwise leave the stream cursor / thinking / search indicators
    // stuck forever (event replays re-add the id, nothing ever deletes it).
    // Only terminal statuses are cleaned — PENDING/STREAMING is never touched.
    // Every updater returns the previous reference when nothing was removed.
    const terminalIds = new Set(list.filter((m) => !ACTIVE_STATUSES.has(m.status)).map((m) => m.id));
    if (terminalIds.size === 0) return;
    setStreamingIds((prev) => {
      if (![...prev].some((id) => terminalIds.has(id))) return prev;
      return new Set([...prev].filter((id) => !terminalIds.has(id)));
    });
    setThinkingIds((prev) => {
      if (![...prev].some((id) => terminalIds.has(id))) return prev;
      return new Set([...prev].filter((id) => !terminalIds.has(id)));
    });
    setSearchingQueries((prev) => {
      if (!Object.keys(prev).some((id) => terminalIds.has(id))) return prev;
      return Object.fromEntries(Object.entries(prev).filter(([id]) => !terminalIds.has(id)));
    });
    setToolCalls((prev) => {
      if (!Object.keys(prev).some((id) => terminalIds.has(id))) return prev;
      return Object.fromEntries(Object.entries(prev).filter(([id]) => !terminalIds.has(id)));
    });
    // Finished messages render message.content; leftover chunks are dead weight.
    setStreamChunks((prev) => {
      if (!Object.keys(prev).some((id) => terminalIds.has(id))) return prev;
      return Object.fromEntries(Object.entries(prev).filter(([id]) => !terminalIds.has(id)));
    });
    // The fetched list now carries the final content, so the settle-time
    // text cache can go. Batched with setMessages above: the cache drops in
    // the same commit that swaps in the authoritative content (no flash).
    setLastStreamedText((prev) => {
      if (!Object.keys(prev).some((id) => terminalIds.has(id))) return prev;
      return Object.fromEntries(Object.entries(prev).filter(([id]) => !terminalIds.has(id)));
    });
  }, [conversationId, branchId]);

  // Baseline for the context ring: the last final usage record carries the
  // authoritative input_tokens (and cache hits). Silently ignored on failure.
  const refreshUsageBaseline = useCallback(async () => {
    if (!conversationId) return;
    // Reset first so the previous conversation's value cannot bleed into the
    // new one through the monotonic Math.max below.
    setUsedTokens(0);
    setCachedTokens(0);
    try {
      const records = await listUsageRecords(conversationId);
      // Records come back created_at DESC: the first valid final record is
      // the latest generation round.
      const lastFinal = records.find(
        (record) => record.is_final && typeof record.input_tokens === "number" && (record.input_tokens ?? 0) > 0,
      );
      if (!lastFinal) return;
      setUsedTokens((prev) => Math.max(prev, lastFinal.input_tokens ?? 0));
      setCachedTokens(lastFinal.cached_input_tokens ?? 0);
    } catch {
      // The ring is decorative; never block the conversation on usage data.
    }
  }, [conversationId]);

  // Initial history load (+ usage baseline for the context ring).
  useEffect(() => {
    refreshMessages().catch((err: unknown) => {
      setLoadError(err instanceof ApiError ? err.message : "消息加载失败");
    });
    void refreshUsageBaseline();
  }, [refreshMessages, refreshUsageBaseline]);

  // Live SSE subscription. Events drive the streaming bubble; terminal events
  // trigger an authoritative refetch. A permanently closed stream flips the
  // page into degraded polling mode.
  useEffect(() => {
    if (!conversationId) return;
    const unsubscribe = subscribeConversationEvents(conversationId, {
      onEvent: (event) => {
        if (event.event === "message.started") {
          // Dev timing: first SSE event for this message (send → first event).
          if (hasMark(`send:${event.message_id}`) && !hasMark(`first-event:${event.message_id}`)) {
            mark(`first-event:${event.message_id}`);
            measure(`${event.message_id} send→first-event`, `send:${event.message_id}`);
          }
          setStreamingIds((prev) => new Set(prev).add(event.message_id));
          // A (re-)started generation supersedes any settle-time cached text.
          setLastStreamedText((prev) => {
            if (!(event.message_id in prev)) return prev;
            const next = { ...prev };
            delete next[event.message_id];
            return next;
          });
        } else if (event.event === "reasoning.delta") {
          setThinkingIds((prev) => new Set(prev).add(event.message_id));
        } else if (event.event === "content.delta") {
          // Dev timing: a first content.delta also counts as the first event.
          if (hasMark(`send:${event.message_id}`) && !hasMark(`first-event:${event.message_id}`)) {
            mark(`first-event:${event.message_id}`);
            measure(`${event.message_id} send→first-event`, `send:${event.message_id}`);
          }
          // The first body chunk ends the thinking phase.
          setThinkingIds((prev) => {
            const next = new Set(prev);
            next.delete(event.message_id);
            return next;
          });
          setStreamingIds((prev) => new Set(prev).add(event.message_id));
          probeChunk(event.message_id, () =>
            setStreamChunks((prev) => ({
              ...prev,
              [event.message_id]: { ...prev[event.message_id], [event.index]: event.text },
            })),
          );
        } else if (event.event === "message.searching") {
          // Web search kickoff (auto or fenced): pin the query on the bubble.
          setSearchingQueries((prev) => ({ ...prev, [event.message_id]: event.query ?? "" }));
        } else if (event.event === "message.tool.status") {
          // Tool call lifecycle: started creates the entry, done/failed update it.
          setToolCalls((prev) => ({
            ...prev,
            [event.message_id]: {
              ...prev[event.message_id],
              [event.call_id]: {
                tool: event.tool,
                label: event.label,
                status: event.status,
                ...(event.duration_ms !== undefined ? { durationMs: event.duration_ms } : {}),
                ...(event.error_class ? { errorClass: event.error_class } : {}),
              },
            },
          }));
        } else if (event.event === "usage.updated") {
          // Track the latest reported input tokens for the context ring.
          // Monotonic: providers may push several increasing usage chunks per
          // stream, and interim reports must never lower the displayed value.
          const payload = event as { input_tokens?: unknown };
          if (typeof payload.input_tokens === "number") {
            setUsedTokens((prev) => Math.max(prev, payload.input_tokens as number));
          }
        } else if (event.event === "message.completed" || event.event === "message.failed") {
          // Cache the joined stream text BEFORE dropping the chunks. Local
          // messages still carry the pre-stream content ("" for a fresh
          // assistant message) until refreshMessages lands one RTT later;
          // without this the bubble would flash empty and bounce back to a
          // typing indicator. On failure this also preserves the partial
          // text the user already saw.
          const settledText = joinChunks(streamChunksRef.current[event.message_id]);
          if (settledText) {
            setLastStreamedText((prev) => ({ ...prev, [event.message_id]: settledText }));
          }
          setStreamingIds((prev) => {
            const next = new Set(prev);
            next.delete(event.message_id);
            return next;
          });
          setThinkingIds((prev) => {
            const next = new Set(prev);
            next.delete(event.message_id);
            return next;
          });
          setStreamChunks((prev) => {
            const next = { ...prev };
            delete next[event.message_id];
            return next;
          });
          setSearchingQueries((prev) => {
            const next = { ...prev };
            delete next[event.message_id];
            return next;
          });
          // Settle calls that never sent their own terminal event: started
          // becomes done, failed stays failed.
          setToolCalls((prev) => {
            const calls = prev[event.message_id];
            if (!calls) return prev;
            return {
              ...prev,
              [event.message_id]: Object.fromEntries(
                Object.entries(calls).map(([callId, call]) => [
                  callId,
                  call.status === "started" ? { ...call, status: "done" as const } : call,
                ]),
              ),
            };
          });
          refreshMessages().catch(() => {});
          // Authoritative correction once the generation reaches a terminal state.
          void refreshUsageBaseline();
        }
      },
      onFatal: () => {
        // SSE is unusable: render server-accumulated content instead.
        setStreamChunks({});
        setLastStreamedText({});
        setStreamingIds(new Set());
        setThinkingIds(new Set());
        setSearchingQueries({});
        setToolCalls({});
        setDegraded(true);
      },
    });
    return unsubscribe;
  }, [conversationId, refreshMessages, refreshUsageBaseline]);

  // Degraded mode: poll the branch messages every second until no assistant
  // message is in an active (PENDING/STREAMING) status anymore.
  const hasActiveMessages = useMemo(
    () => (messages ?? []).some((m) => m.role === "assistant" && ACTIVE_STATUSES.has(m.status)),
    [messages],
  );

  // Latest swarm run in this conversation (drives the right-side workbench);
  // the goal is the user message that triggered it.
  const latestRunInfo = useMemo(() => {
    const list = messages ?? [];
    for (let index = list.length - 1; index >= 0; index--) {
      const runId = list[index].agent_run_id;
      if (runId) {
        const goal = [...list.slice(0, index)].reverse().find((m) => m.role === "user")?.content;
        return { runId, goal };
      }
    }
    return null;
  }, [messages]);

  // project_id of this conversation (chapter downloads, attachment uploads);
  // rides the sidebar's conversations cache. Needed in BOTH modes now that
  // chat conversations accept file attachments.
  const conversationsQuery = useQuery({
    queryKey: ["conversations", viewMode],
    queryFn: () => listConversations({ mode: viewMode }),
    staleTime: 60_000,
    enabled: Boolean(conversationId),
  });
  const projectId = (conversationsQuery.data ?? []).find((c) => c.id === conversationId)?.project_id ?? null;
  useEffect(() => {
    if (!degraded || !hasActiveMessages) return;
    const timer = setInterval(() => {
      refreshMessages().catch(() => {});
    }, 1000);
    return () => clearInterval(timer);
  }, [degraded, hasActiveMessages, refreshMessages]);

  // Track whether the user is near the bottom; scrolling up opts out of
  // auto-follow until they return (or hit the jump button).
  function handleScroll() {
    const node = scrollRef.current;
    if (!node) return;
    const atBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 80;
    atBottomRef.current = atBottom;
    setShowJumpToBottom(!atBottom);
  }

  function jumpToBottom() {
    const node = scrollRef.current;
    if (!node) return;
    atBottomRef.current = true;
    setShowJumpToBottom(false);
    node.scrollTo({ top: node.scrollHeight, behavior: "smooth" });
  }

  // New conversation: re-arm auto-follow so the fresh history pins to the end.
  useEffect(() => {
    atBottomRef.current = true;
    setShowJumpToBottom(false);
  }, [conversationId]);

  // Follow the latest content, but only while pinned to the bottom. Writes are
  // merged through rAF (at most one scroll assignment per frame) and stay
  // instant — smooth scrolling cannot keep up with a stream.
  useEffect(() => {
    if (!atBottomRef.current || scrollRafRef.current) return;
    scrollRafRef.current = requestAnimationFrame(() => {
      scrollRafRef.current = 0;
      const node = scrollRef.current;
      if (node && atBottomRef.current) node.scrollTop = node.scrollHeight;
    });
  }, [messages, streamChunks, streamingIds, searchingQueries]);

  useEffect(() => () => cancelAnimationFrame(scrollRafRef.current), []);

  /** Shared send pipeline: composer sends and swarm quick actions both use it.
   *  Files (composer attachments) upload first; any upload failure aborts the
   *  send with an error instead of posting a message missing its files. */
  async function sendContent(content: string, files: File[] = []) {
    if (!content || !conversationId || !branchId || sending) return;
    setSending(true);
    setSendError(null);
    try {
      let attachmentIds: string[] = [];
      if (files.length > 0) {
        if (!projectId) throw new ApiError(0, "附件上传失败：未找到会话所属项目");
        attachmentIds = await uploadAttachmentIds(projectId, files);
      }
      const model = loadSelectedModel();
      const result = await sendMessage(conversationId, {
        branch_id: branchId,
        content,
        client_request_id: uuid(),
        // Swarm conversations stay in swarm routing; the backend decides
        // chitchat vs. starting a run by intent.
        ...(swarmActive ? { mode: "swarm" as const } : {}),
        ...(attachmentIds.length > 0 ? { attachment_ids: attachmentIds } : {}),
        ...(model
          ? {
              provider: model.provider,
              model: model.model_id,
              reasoning_level: loadReasoningLevel(model.provider, model.model_id) ?? "auto",
            }
          : {}),
      });
      // The user may have switched conversations while the send was in
      // flight: drop the stale result instead of overwriting the new view.
      if (!isCurrentConversation(conversationIdRef.current, conversationId)) return;
      setInput("");
      setAttachments([]);
      // Dev timing: anchor for send → first-event / first-render measurements.
      mark(`send:${result.assistant_message_id}`);
      setStreamingIds((prev) => new Set(prev).add(result.assistant_message_id));
      await refreshMessages();
    } catch (err) {
      if (isCurrentConversation(conversationIdRef.current, conversationId)) {
        setSendError(err instanceof ApiError ? err.message : "发送失败，请稍后重试");
      }
    } finally {
      setSending(false);
    }
  }

  async function handleSend() {
    await sendContent(input.trim(), attachments);
  }

  async function handleRetryRun(runId: string) {
    try {
      await controlAgentRun(runId, "retry");
      await refreshMessages();
      // Re-arm the workbench polling loop: it stopped at the terminal status
      // and the component does not remount (same run id, same key).
      setWorkbenchPollGeneration((n) => n + 1);
    } catch (err) {
      setSendError(err instanceof ApiError ? err.message : "重试失败，请稍后重试");
    }
  }

  /** A control action (re-)enqueued a generation: arm its streaming bubble. */
  const handleStreamStart = useCallback((messageId: string) => {
    setStreamingIds((prev) => new Set(prev).add(messageId));
    // The re-enqueued generation supersedes any settle-time cached text.
    setLastStreamedText((prev) => {
      if (!(messageId in prev)) return prev;
      const next = { ...prev };
      delete next[messageId];
      return next;
    });
  }, []);

  // Edit-and-fork: the backend forks a branch carrying the edited content;
  // switch to it and let the load effect refetch that branch's history.
  const editMutation = useMutation({
    mutationFn: ({ messageId, content }: { messageId: string; content: string }) =>
      editMessage(conversationId ?? "", messageId, content),
    onSuccess: (result) => {
      setStreamChunks({});
      setLastStreamedText({});
      setStreamingIds(new Set());
      setThinkingIds(new Set());
      setSearchingQueries({});
      setToolCalls({});
      setMessages(null);
      setBranchId(result.branch_id);
    },
    onError: (err) => setSendError(err instanceof ApiError ? err.message : "编辑失败，请稍后重试"),
  });

  // Join chunks once per streamChunks change; equal strings keep memoized
  // bubbles stable (Object.is on string props), so only the streaming bubble
  // re-renders per chunk.
  const streamTexts = useMemo(() => {
    const map: Record<string, string> = {};
    for (const id of Object.keys(streamChunks)) map[id] = joinChunks(streamChunks[id]);
    return map;
  }, [streamChunks]);

  // A terminal SSE event arrives one RTT before refreshMessages lands. While
  // the settle-time text cache covers that gap, never let the bubble fall
  // back to a typing state just because the stale local message status is
  // still STREAMING.
  const isBubbleStreaming = (messageId: string, status: MessageStatus) =>
    streamingIds.has(messageId) ||
    (ACTIVE_STATUSES.has(status) && !streamTexts[messageId] && !lastStreamedText[messageId]);

  return (
    <div className="flex h-full">
      {/* Chat column */}
      <div className="flex min-w-0 flex-1 flex-col">
      {/* Message list */}
      <div className="relative min-h-0 flex-1">
        <div ref={scrollRef} onScroll={handleScroll} className="h-full overflow-y-auto overscroll-contain">
          <div className="mx-auto flex w-full max-w-[768px] flex-col gap-7 px-6 py-10">
            {loadError && <p className="py-10 text-center text-sm text-red-600">{loadError}</p>}
            {!loadError && messages === null && <p className="py-10 text-center text-sm text-ink-secondary">加载中…</p>}
            {!loadError && messages !== null && messages.length === 0 && (
              <p className="py-10 text-center text-sm text-ink-secondary">开始新的对话吧</p>
            )}
            {(messages ?? []).map((message) =>
              message.role === "user" ? (
                <UserMessageBubble
                  key={message.id}
                  message={message}
                  pending={editMutation.isPending && editMutation.variables?.messageId === message.id}
                  onEdit={(content) => editMutation.mutate({ messageId: message.id, content })}
                />
              ) : (
                <div key={message.id}>
                  <AssistantMessage
                    message={message}
                    text={streamTexts[message.id] || lastStreamedText[message.id] || message.content}
                    isStreaming={isBubbleStreaming(message.id, message.status)}
                    isThinking={thinkingIds.has(message.id)}
                    searchQuery={searchingQueries[message.id]}
                    toolCalls={toolCalls[message.id]}
                    conversationId={conversationId ?? ""}
                    showRetrieval={Boolean(conversationId)}
                  />
                  {message.agent_run_id ? (
                    // Swarm run placeholder: controlled via the agent run, never
                    // the single-model message endpoints.
                    viewMode === "work" &&
                    !streamingIds.has(message.id) &&
                    !ACTIVE_STATUSES.has(message.status) && (
                      <SwarmActionRow
                        message={message}
                        projectId={projectId}
                        onRetry={(runId) => void handleRetryRun(runId)}
                        onError={setSendError}
                      />
                    )
                  ) : (
                    <AssistantActionRow
                      conversationId={conversationId ?? ""}
                      message={message}
                      isStreaming={isBubbleStreaming(message.id, message.status)}
                      onStreamStart={handleStreamStart}
                      onSettled={() => void refreshMessages()}
                      onError={setSendError}
                    />
                  )}
                </div>
              ),
            )}
          </div>
        </div>

        {showJumpToBottom && (
          <button
            type="button"
            title="回到底部"
            aria-label="回到底部"
            onClick={jumpToBottom}
            className="absolute bottom-4 right-6 flex h-9 w-9 items-center justify-center rounded-full border border-line bg-white text-ink-secondary shadow-md transition-colors hover:text-ink"
          >
            <ChevronDownIcon size={18} />
          </button>
        )}
      </div>

      {/* Composer */}
      <div className="shrink-0 px-6 pb-5 pt-2">
        <div className="mx-auto w-full max-w-[768px]">
          <Composer value={input} onChange={setInput} onSend={handleSend} sending={sending || !branchId} usedTokens={usedTokens} cachedTokens={cachedTokens} attachments={attachments} onAttachmentsChange={setAttachments} />
          {sendError && <p className="mt-2 px-2 text-sm text-red-600">{sendError}</p>}
        </div>
      </div>
      </div>

      {/* Right sidebar: batch-wide writing progress above the per-run swarm
          workbench. Shown in work mode whenever the conversation resolves a
          project (the workbench additionally needs an agent run). Hidden on
          small screens; the detail page remains the fallback. */}
      {viewMode === "work" && (projectId || latestRunInfo) && (
        <div className="hidden h-full lg:flex lg:shrink-0 lg:flex-col">
          {projectId && <WritingProgressPanel projectId={projectId} />}
          {latestRunInfo && (
            <div className="min-h-0 flex-1">
              <SwarmWorkbench
                key={latestRunInfo.runId}
                runId={latestRunInfo.runId}
                goal={latestRunInfo.goal}
                pollGeneration={workbenchPollGeneration}
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}
