// @vitest-environment jsdom
/**
 * Regression test for the settle-time flash: when message.completed /
 * message.failed arrives, the joined stream text is cached and keeps the
 * bubble full (no empty flash, no typing-indicator bounce, no stream cursor)
 * until refreshMessages lands one RTT later with the authoritative content.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatMessage, ConversationStreamHandlers } from "../../lib/api/conversations";

vi.stubGlobal("localStorage", {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
});

// Captured SSE handlers: tests push events through them directly.
let streamHandlers: ConversationStreamHandlers | null = null;
const listMessagesMock = vi.fn<(conversationId: string, branchId: string) => Promise<ChatMessage[]>>();

vi.mock("../../lib/api/conversations", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/conversations")>();
  return {
    ...actual,
    listBranches: vi.fn(async () => [{ id: "b1", parent_branch_id: null, status: "ACTIVE" }]),
    listMessages: (conversationId: string, branchId: string) => listMessagesMock(conversationId, branchId),
    listConversations: vi.fn(async () => []),
    getMessageRetrieval: vi.fn(async () => {
      throw new Error("no snapshot");
    }),
    subscribeConversationEvents: vi.fn((_conversationId: string, handlers: ConversationStreamHandlers) => {
      streamHandlers = handlers;
      return () => {};
    }),
    sendMessage: vi.fn(),
    editMessage: vi.fn(),
    regenerateMessage: vi.fn(),
    retryMessage: vi.fn(),
    continueMessage: vi.fn(),
    stopMessage: vi.fn(),
  };
});
vi.mock("../../lib/api/usage", () => ({ listUsageRecords: vi.fn(async () => []) }));
vi.mock("../../lib/api/files", () => ({ uploadAttachmentIds: vi.fn(async () => []) }));
vi.mock("../../lib/api/agentRuns", () => ({
  controlAgentRun: vi.fn(),
  exportRunZip: vi.fn(),
  getAgentRun: vi.fn(),
}));
vi.mock("../../lib/api/projects", () => ({ getChapterContent: vi.fn() }));
vi.mock("../../app/ViewModeContext", () => ({
  useViewMode: () => ({ viewMode: "chat", chatMode: "normal", setViewMode: () => {}, setChatMode: () => {} }),
}));
vi.mock("../../components/composer/Composer", () => ({ Composer: () => null }));
vi.mock("../../components/chat/SwarmWorkbench", () => ({ SwarmWorkbench: () => null }));
vi.mock("../../components/chat/WritingProgressPanel", () => ({ WritingProgressPanel: () => null }));
vi.mock("../../components/composer/ModelSelect", () => ({ loadSelectedModel: () => null }));
vi.mock("../../components/composer/ReasoningSelect", () => ({ loadReasoningLevel: () => null }));
// Identity reveal: assertions see the full text, not the typewriter animation.
vi.mock("../../lib/hooks/useSmoothText", () => ({ useSmoothText: (text: string) => text }));

const { ChatPage } = await import("./ChatPage");

function userMessage(): ChatMessage {
  return { id: "u1", role: "user", content: "hi", status: "COMPLETED", context_snapshot_id: null };
}

function assistantMessage(status: ChatMessage["status"], content: string): ChatMessage {
  return { id: "a1", role: "assistant", content, status, context_snapshot_id: null };
}

function renderChat() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:conversationId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function pushEvent(event: Parameters<ConversationStreamHandlers["onEvent"]>[0]) {
  act(() => {
    streamHandlers?.onEvent(event);
  });
}

function markdownText(): string {
  return document.querySelector(".markdown")?.textContent ?? "";
}

/** The settle gap must show no typing dots and no stream cursor. */
function expectNoStreamingChrome() {
  expect(document.querySelector(".stream-cursor")).toBeNull();
  expect(document.querySelectorAll(".typing-dot").length).toBe(0);
}

/** Initial load resolves immediately; the terminal-event refetch stays pending. */
function mockMessageLoads(initial: ChatMessage[]) {
  let resolveRefresh: (list: ChatMessage[]) => void = () => {};
  listMessagesMock.mockReset();
  listMessagesMock.mockResolvedValueOnce(initial);
  listMessagesMock.mockImplementationOnce(
    () =>
      new Promise<ChatMessage[]>((resolve) => {
        resolveRefresh = resolve;
      }),
  );
  return {
    settleRefresh: (list: ChatMessage[]) =>
      act(() => {
        resolveRefresh(list);
      }),
  };
}

afterEach(() => {
  cleanup();
  streamHandlers = null;
});

describe("ChatPage stream settle", () => {
  it("keeps the streamed text and suppresses typing until refreshMessages lands", async () => {
    const { settleRefresh } = mockMessageLoads([userMessage(), assistantMessage("STREAMING", "")]);
    renderChat();
    await screen.findByText("hi");

    pushEvent({ event: "message.started", message_id: "a1" });
    pushEvent({ event: "content.delta", message_id: "a1", index: 0, text: "Hello " });
    pushEvent({ event: "content.delta", message_id: "a1", index: 1, text: "world" });
    expect(markdownText()).toContain("Hello world");

    // Terminal event while the authoritative refetch is still in flight:
    // no empty flash, no typing-indicator bounce, no stream cursor.
    pushEvent({ event: "message.completed", message_id: "a1", status: "COMPLETED" });
    expect(markdownText()).toContain("Hello world");
    expectNoStreamingChrome();

    // Once the refetch lands, the authoritative content takes over.
    settleRefresh([userMessage(), assistantMessage("COMPLETED", "Hello world, final.")]);
    await waitFor(() => expect(markdownText()).toContain("Hello world, final."));
    expectNoStreamingChrome();
  });

  it("preserves the partial text on message.failed and adds the failure hint after refresh", async () => {
    const { settleRefresh } = mockMessageLoads([userMessage(), assistantMessage("STREAMING", "")]);
    renderChat();
    await screen.findByText("hi");

    pushEvent({ event: "message.started", message_id: "a1" });
    pushEvent({ event: "content.delta", message_id: "a1", index: 0, text: "partial answer" });
    expect(markdownText()).toContain("partial answer");

    // Failure mid-stream: the text the user already saw must not flash away.
    pushEvent({ event: "message.failed", message_id: "a1", status: "FAILED" });
    expect(markdownText()).toContain("partial answer");
    expectNoStreamingChrome();

    // The refetch lands with the failed status: the hint appears below the
    // preserved text instead of replacing it with an empty bubble.
    settleRefresh([userMessage(), assistantMessage("FAILED", "partial answer")]);
    await waitFor(() => expect(screen.getByText("生成失败，请稍后重试")).toBeTruthy());
    expect(markdownText()).toContain("partial answer");
    expectNoStreamingChrome();
  });
});
