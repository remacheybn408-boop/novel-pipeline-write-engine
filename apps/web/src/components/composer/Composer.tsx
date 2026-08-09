import type { ChangeEvent, ClipboardEvent, KeyboardEvent, ReactNode } from "react";
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowUpIcon, PaperclipIcon, XIcon } from "../ui/icons";
import { ModelSelect } from "./ModelSelect";
import { useSwarmContextWindow } from "./useSwarmContextWindow";
import { ReasoningSelect } from "./ReasoningSelect";
import { ContextRing } from "./ContextRing";
import type { ModelInfo } from "../../lib/api/models";
import { listMcpServers, listSkills } from "../../lib/api/plugins";
import { useViewMode } from "../../app/ViewModeContext";

/** Window event dispatched to focus the composer's textarea (Ctrl+K shortcut). */
export const FOCUS_COMPOSER_EVENT = "proseforge:focus-composer";

/** Fallback window when the selected model carries no context_window. */
const DEFAULT_CONTEXT_WINDOW = 8192;

/** Attachment whitelist (M1): must match the backend parse whitelist
 *  (proseforge/infrastructure/webtools/documents.py). Images excluded. */
export const ATTACHMENT_ACCEPT = ".txt,.md,.json,.csv,.pdf,.docx,.xlsx";

/** Extension check shared by the file picker and paste filtering. */
export function isSupportedAttachment(file: File): boolean {
  const name = file.name.toLowerCase();
  return ATTACHMENT_ACCEPT.split(",").some((ext) => name.endsWith(ext));
}

interface ComposerProps {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  sending?: boolean;
  autoFocus?: boolean;
  /** Tokens already consumed by the current conversation (usage ring). */
  usedTokens?: number;
  /** Server-side cache hits from the latest final usage record (hover card). */
  cachedTokens?: number;
  /** Optional window override; defaults to the selected model's catalog value. */
  contextWindow?: number;
  /** Optional strip rendered attached under the card (e.g. the project picker). */
  footer?: ReactNode;
  /** Pending attachments (controlled, same style as value/onChange). When
   *  both props are set the paperclip button and paste-to-attach turn on. */
  attachments?: File[];
  onAttachmentsChange?: (files: File[]) => void;
}

/**
 * The shared chat composer card used on the home page and the chat page.
 * Enter sends, Shift+Enter inserts a newline (IME composition is respected).
 */
export function Composer({
  value,
  onChange,
  onSend,
  sending = false,
  autoFocus = false,
  usedTokens = 0,
  cachedTokens = 0,
  contextWindow,
  footer,
  attachments,
  onAttachmentsChange,
}: ComposerProps) {
  const { viewMode } = useViewMode();
  const isEmpty = value.trim().length === 0;
  const disabled = isEmpty || sending;
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // Attachments are opt-in: pages pass both props to enable the paperclip
  // button, paste-to-attach and the chip row.
  const attachmentsEnabled = attachments !== undefined && onAttachmentsChange !== undefined;
  // The model picker reports its effective selection so the usage ring can
  // resolve the context window from the catalog.
  const [selectedModel, setSelectedModel] = useState<ModelInfo | null>(null);
  // Swarm mode: the window comes from the resolved five-seat cluster config;
  // null everywhere else (and on any fetch failure) so the single-model
  // fallback below keeps working unchanged.
  const swarmWindow = useSwarmContextWindow();

  // The ring shows the model's real window — the backend resolves the
  // verified known-windows value (no display-side cap).
  const effectiveWindow = contextWindow ?? swarmWindow ?? selectedModel?.context_window ?? DEFAULT_CONTEXT_WINDOW;

  // Enabled-plugin counts for the ring's hover card (same data source as the
  // plugins page: listSkills covers user + built-in skills). Work mode only;
  // chat mode never injects plugins. Failures stay silent — the row hides.
  const skillsQuery = useQuery({
    queryKey: ["skills"],
    queryFn: listSkills,
    staleTime: 60_000,
    enabled: viewMode === "work",
  });
  const mcpServersQuery = useQuery({
    queryKey: ["mcp-servers"],
    queryFn: listMcpServers,
    staleTime: 60_000,
    enabled: viewMode === "work",
  });

  let toolsText: string | null = null;
  if (viewMode === "chat") {
    toolsText = "工具：聊天模式不启用";
  } else if (skillsQuery.isSuccess && mcpServersQuery.isSuccess) {
    const skillCount = skillsQuery.data.filter((skill) => skill.enabled).length;
    const mcpCount = mcpServersQuery.data.filter((server) => server.enabled).length;
    toolsText = `工具：技能 ${skillCount} · MCP ${mcpCount}（已启用）`;
  }

  // Ctrl+K navigates home and broadcasts FOCUS_COMPOSER_EVENT; focus the
  // textarea whenever it arrives.
  useEffect(() => {
    function handleFocusEvent() {
      textareaRef.current?.focus();
    }
    window.addEventListener(FOCUS_COMPOSER_EVENT, handleFocusEvent);
    return () => window.removeEventListener(FOCUS_COMPOSER_EVENT, handleFocusEvent);
  }, []);

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      if (!disabled) onSend();
    }
  }

  /** Filter dropped/picked/pasted candidates by the whitelist and append. */
  function addAttachments(candidates: File[]): number {
    if (!attachmentsEnabled) return 0;
    const accepted = candidates.filter(isSupportedAttachment);
    if (accepted.length > 0) onAttachmentsChange([...attachments, ...accepted]);
    return accepted.length;
  }

  function handleFilePick(event: ChangeEvent<HTMLInputElement>) {
    addAttachments(Array.from(event.target.files ?? []));
    event.target.value = ""; // reset so re-picking the same file fires change
  }

  function handlePaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    if (!attachmentsEnabled) return;
    const files = Array.from(event.clipboardData?.files ?? []);
    if (files.length === 0) return;
    // Only swallow the paste when at least one file became an attachment;
    // otherwise fall through to the default (text) behavior.
    if (addAttachments(files) > 0) event.preventDefault();
  }

  return (
    <div className="w-full">
      <div className="rounded-[24px] border border-line bg-white shadow-[0_2px_16px_rgba(0,0,0,0.05)]">
        {attachmentsEnabled && attachments.length > 0 && (
          <div className="flex flex-wrap gap-2 px-5 pt-4">
            {attachments.map((file, index) => (
              <span
                key={`${file.name}-${index}`}
                className="flex items-center gap-1.5 rounded-full bg-sidebar px-3 py-1 text-xs text-ink"
              >
                <span className="max-w-[220px] truncate">{file.name}</span>
                <button
                  type="button"
                  aria-label={`移除附件 ${file.name}`}
                  onClick={() => onAttachmentsChange(attachments.filter((_, i) => i !== index))}
                  className="text-ink-secondary transition-colors hover:text-ink"
                >
                  <XIcon size={12} />
                </button>
              </span>
            ))}
          </div>
        )}
        <textarea
          ref={textareaRef}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          rows={3}
          autoFocus={autoFocus}
          placeholder="输入消息，Enter 发送"
          className="w-full resize-none rounded-t-[24px] bg-transparent px-5 pb-2 pt-5 text-[15px] leading-relaxed text-ink outline-none placeholder:text-ink-secondary disabled:cursor-not-allowed"
        />
        <div className="flex items-center justify-between px-4 pb-3.5">
          <div className="flex items-center">
            {attachmentsEnabled && (
              <>
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  accept={ATTACHMENT_ACCEPT}
                  className="hidden"
                  aria-label="选择附件文件"
                  onChange={handleFilePick}
                />
                <button
                  type="button"
                  title="添加附件"
                  aria-label="添加附件"
                  onClick={() => fileInputRef.current?.click()}
                  className="flex h-8 w-8 items-center justify-center rounded-full text-ink-secondary transition-colors hover:bg-hover hover:text-ink disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <PaperclipIcon size={17} />
                </button>
              </>
            )}
          </div>
          <div className="flex items-center gap-3">
            <ContextRing usedTokens={usedTokens} contextWindow={effectiveWindow} cachedTokens={cachedTokens} toolsText={toolsText} />
            <ReasoningSelect model={selectedModel} />
            <ModelSelect onSelectionChange={setSelectedModel} />
            <button
              type="button"
              onClick={onSend}
              disabled={disabled}
              title="发送"
              className={`flex h-9 w-9 items-center justify-center rounded-full transition-colors ${
                disabled ? "cursor-not-allowed bg-disabled text-white" : "bg-ink text-white hover:opacity-90"
              }`}
            >
              <ArrowUpIcon size={18} />
            </button>
          </div>
        </div>
      </div>
      {footer}
    </div>
  );
}
