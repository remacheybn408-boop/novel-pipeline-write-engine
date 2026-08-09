/**
 * Conversation/chat endpoints — proseforge/api/routes/conversations.py
 * and proseforge/api/routes/branches.py.
 *
 * Confirmed shapes:
 *   POST /api/v1/conversations  {project_id, title?} -> {id, branch_id, title}
 *                               project_id accepts either the id or the slug.
 *   POST /api/v1/conversations/{id}/messages
 *     {branch_id, content, client_request_id, provider?, model?, reasoning_level?}
 *     -> {user_message_id, assistant_message_id, task_id}
 *   GET  /api/v1/conversations/{id}/branches/{branch_id}/messages
 *     -> [{id, role, content, status, context_snapshot_id, generation_attempt, parent_message_id}]
 *     Assistant content accumulates server-side while streaming.
 *   GET  /api/v2/conversations/{id}/branches -> ConversationBranch[]
 *     NOTE: branch routes live under the /api/v2 prefix (branches.py), not v1.
 *     (used to recover the default branch when we did not create the conversation)
 *   GET  /api/v1/conversations/{id}/events  (SSE, see subscribeConversationEvents)
 */
import { request } from "./client";

export interface Conversation {
  id: string;
  branch_id: string;
  title: string;
}

export interface ConversationBranch {
  id: string;
  conversation_id: string;
  name: string;
  parent_branch_id: string | null;
  forked_from_message_id: string | null;
  status: string;
  title: string | null;
}

/** Message status lifecycle: PENDING -> STREAMING -> COMPLETED | FAILED | PARTIAL | CANCELLED. */
export type MessageStatus = "PENDING" | "STREAMING" | "COMPLETED" | "FAILED" | "PARTIAL" | "CANCELLED";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  status: MessageStatus;
  context_snapshot_id: string | null;
  /** Swarm writing-intent placeholder: the agent run producing this reply. */
  agent_run_id?: string | null;
  /** Regenerate grouping: sibling candidates share one parent edge; the
   *  latest attempt wins model context, older ones are display-only. */
  generation_attempt?: number;
  parent_message_id?: string | null;
  /** Files the user attached to this message (download-link chips). */
  attachments?: MessageAttachment[];
}

/** User-uploaded file linked to a message (serialized by list_messages). */
export interface MessageAttachment {
  id: string;
  filename: string;
}

export interface SendMessageInput {
  branch_id: string;
  content: string;
  client_request_id: string;
  /** "swarm" routes the message through the cluster (work mode only). */
  mode?: "swarm";
  provider?: string;
  model?: string;
  reasoning_level?: string;
  /** Ids from uploadProjectFile; the backend links them to the user message
   *  and injects the parsed text into the model context. */
  attachment_ids?: string[];
}

export interface SendMessageResult {
  user_message_id: string;
  assistant_message_id: string;
  task_id: string;
}

/** Statuses in which the assistant message is still being generated. */
export const ACTIVE_STATUSES: ReadonlySet<string> = new Set(["PENDING", "STREAMING"]);

export function createConversation(projectId: string, title?: string): Promise<Conversation> {
  return request<Conversation>("/api/v1/conversations", {
    method: "POST",
    body: title ? { project_id: projectId, title } : { project_id: projectId },
  });
}

export function sendMessage(conversationId: string, input: SendMessageInput): Promise<SendMessageResult> {
  return request<SendMessageResult>(`/api/v1/conversations/${conversationId}/messages`, {
    method: "POST",
    body: input,
  });
}

export function listMessages(conversationId: string, branchId: string): Promise<ChatMessage[]> {
  return request<ChatMessage[]>(`/api/v1/conversations/${conversationId}/branches/${branchId}/messages`);
}

export function listBranches(conversationId: string): Promise<ConversationBranch[]> {
  return request<ConversationBranch[]>(`/api/v2/conversations/${conversationId}/branches`);
}

export interface ConversationSummary {
  id: string;
  project_id: string;
  title: string;
  created_at: string;
  archived?: boolean;
}

/** GET /api/v1/conversations?mode=&project_id=&archived= — sidebar history list. */
export function listConversations(params?: {
  mode?: string;
  project_id?: string;
  archived?: boolean;
}): Promise<ConversationSummary[]> {
  return request<ConversationSummary[]>("/api/v1/conversations", {
    query: { mode: params?.mode, project_id: params?.project_id, archived: params?.archived },
  });
}

/** DELETE /api/v1/conversations/{id} -> 204 (backend endpoint in parallel dev). */
export function deleteConversation(id: string): Promise<void> {
  return request<void>(`/api/v1/conversations/${id}`, { method: "DELETE" });
}

/** PATCH /api/v1/conversations/{id} {archived} -> 200 {id, archived} (parallel dev). */
export function archiveConversation(id: string, archived: boolean): Promise<void> {
  return request<void>(`/api/v1/conversations/${id}`, { method: "PATCH", body: { archived } });
}

// ---------------------------------------------------------------------------
// Message control — proseforge/api/routes/conversations.py:203-253 (v1
// stop/retry/continue) and proseforge/api/routes/branches.py (v2
// regenerate/edit-and-fork). All of them re-enqueue or settle a generation;
// callers should refetch the branch messages afterwards.
// ---------------------------------------------------------------------------

/** Optional target-model override; omitted fields reuse the message snapshot. */
export interface MessageControlInput {
  provider?: string;
  model?: string;
  reasoning_level?: string;
}

export interface MessageControlResult {
  id: string;
  status: MessageStatus;
  task_id?: string;
}

/** POST /api/v1/messages/{id}/stop -> {id, status: "CANCELLED"}. */
export function stopMessage(messageId: string): Promise<{ id: string; status: MessageStatus }> {
  return request<{ id: string; status: MessageStatus }>(`/api/v1/messages/${messageId}/stop`, { method: "POST" });
}

/** POST /api/v1/messages/{id}/retry (FAILED/PARTIAL) -> {id, status, task_id}. */
export function retryMessage(messageId: string, input: MessageControlInput = {}): Promise<MessageControlResult> {
  return request<MessageControlResult>(`/api/v1/messages/${messageId}/retry`, { method: "POST", body: input });
}

/** POST /api/v1/messages/{id}/continue (PARTIAL only) -> {id, status, task_id}. */
export function continueMessage(messageId: string, input: MessageControlInput = {}): Promise<MessageControlResult> {
  return request<MessageControlResult>(`/api/v1/messages/${messageId}/continue`, { method: "POST", body: input });
}

export interface RegenerateResult {
  message_id: string;
  task_id: string;
}

/** POST /api/v2/conversations/{cid}/messages/{mid}/regenerate -> {message_id, task_id}. */
export function regenerateMessage(
  conversationId: string,
  messageId: string,
  input: MessageControlInput = {},
): Promise<RegenerateResult> {
  return request<RegenerateResult>(`/api/v2/conversations/${conversationId}/messages/${messageId}/regenerate`, {
    method: "POST",
    body: input,
  });
}

export interface EditMessageResult {
  branch_id: string;
  source_message_id: string;
  replacement_message_id: string;
}

/** POST /api/v2/conversations/{cid}/messages/{mid}/edit — forks a branch with the edited content. */
export function editMessage(conversationId: string, messageId: string, content: string): Promise<EditMessageResult> {
  return request<EditMessageResult>(`/api/v2/conversations/${conversationId}/messages/${messageId}/edit`, {
    method: "POST",
    body: { content },
  });
}

// ---------------------------------------------------------------------------
// Retrieval snapshot — GET /api/v1/conversations/{id}/messages/{mid}/retrieval
// (work mode RAG provenance; 404 when the message has no snapshot)
// ---------------------------------------------------------------------------

export interface RetrievalChunk {
  document_title: string;
  chapter_no: number | null;
  score: number;
  /** Neighbour chunk pulled in around a hit. */
  expanded: boolean;
}

export interface RetrievalTrimmed {
  section: string;
  reason: string;
}

export interface MessageRetrieval {
  query_text: string;
  intent: string;
  chunks: RetrievalChunk[];
  trimmed: RetrievalTrimmed[];
  elapsed_ms: number;
  token_cost: number;
}

export function getMessageRetrieval(conversationId: string, messageId: string): Promise<MessageRetrieval> {
  return request<MessageRetrieval>(`/api/v1/conversations/${conversationId}/messages/${messageId}/retrieval`);
}

// ---------------------------------------------------------------------------
// SSE stream — GET /api/v1/conversations/{id}/events
//
// Frames are written by proseforge/api/sse/encoder.py as
//   id: <n>\nevent: <name>\ndata: <json>\n\n
// where <name> is the payload's own "event" key. Published names (see
// proseforge/application/conversations/generate_reply.py and workflows/tasks.py):
//   message.started   {event, message_id}
//   content.delta     {event, message_id, index, text}
//   reasoning.delta   {event, message_id, index?, text}  (thinking pass-through,
//                       not persisted; may never fire for non-reasoning models)
//   message.completed {event, message_id, status: "COMPLETED", content_hash}
//   message.failed    {event, message_id, status, reason?}
//   message.searching {event, message_id, query}  (web search in progress,
//                       fired by both auto and fenced search; may never fire)
//   message.tool.status {event, message_id, call_id, tool, status, label,
//                       duration_ms?, error_class?}  (tool call lifecycle:
//                       started → done|failed; started has no duration_ms,
//                       failed carries error_class)
//   usage.updated     {event, message_id, ...usage fields}
// The backend sends ": heartbeat" comments every 15s and honors Last-Event-ID
// on reconnect, both of which native EventSource handles for us.
// ---------------------------------------------------------------------------

export type ConversationEvent =
  | { event: "message.started"; message_id: string }
  | { event: "content.delta"; message_id: string; index: number; text: string }
  | { event: "reasoning.delta"; message_id: string; index?: number; text: string }
  | { event: "message.completed"; message_id: string; status: string; content_hash?: string | null }
  | { event: "message.failed"; message_id: string; status: string; reason?: string }
  | { event: "message.searching"; message_id: string; query?: string }
  | {
      event: "message.tool.status";
      message_id: string;
      call_id: string;
      tool: "search_web" | "read_page" | "get_page_metadata" | "extract_links" | "fetch_document" | "run_code";
      status: "started" | "done" | "failed";
      label: string;
      duration_ms?: number;
      error_class?: "validation" | "policy_denied" | "circuit_breaker" | "timeout" | "rate_limited" | "upstream" | null;
    }
  | { event: "usage.updated"; message_id: string } & Record<string, unknown>;

const SSE_EVENT_NAMES = ["message.started", "content.delta", "reasoning.delta", "message.completed", "message.failed", "message.searching", "message.tool.status", "usage.updated"] as const;

export interface ConversationStreamHandlers {
  onEvent: (event: ConversationEvent) => void;
  /** Fired when the connection is permanently closed; callers should fall back to polling. */
  onFatal?: () => void;
}

/**
 * Subscribe to a conversation's SSE stream. Requests are same-origin (the Vite
 * proxy in dev), so the session cookie rides along automatically; withCredentials
 * is set explicitly for good measure. Returns an unsubscribe function.
 */
export function subscribeConversationEvents(conversationId: string, handlers: ConversationStreamHandlers): () => void {
  const source = new EventSource(`/api/v1/conversations/${conversationId}/events`, { withCredentials: true });

  for (const name of SSE_EVENT_NAMES) {
    source.addEventListener(name, (raw) => {
      try {
        handlers.onEvent(JSON.parse((raw as MessageEvent).data) as ConversationEvent);
      } catch {
        // Ignore malformed frames; heartbeats arrive as comments and never reach here.
      }
    });
  }

  source.onerror = () => {
    // CONNECTING means the browser is auto-retrying; CLOSED is terminal.
    if (source.readyState === EventSource.CLOSED) {
      handlers.onFatal?.();
    }
  };

  return () => source.close();
}
