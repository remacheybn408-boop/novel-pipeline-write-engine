/**
 * Regression tests for the home page's first-send pipeline: when the first
 * sendMessage fails right after createConversation succeeded, the empty
 * conversation must be recycled so no ghost entry piles up in the sidebar.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../lib/api/client";

// HomePage imports ViewModeContext (via the composer), which reads
// localStorage at module load; stub it before pulling the module in.
vi.stubGlobal("localStorage", {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
});

const { sendFirstMessage } = await import("./HomePage");

const conversation = { id: "c1", branch_id: "b1", title: "新聊天" };
const input = { branch_id: "b1", content: "hello", client_request_id: "r1" };

function jsonResponse(payload: unknown, status: number): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mockFetchSequence(responses: Response[]) {
  const spy = vi.fn();
  for (const response of responses) {
    spy.mockResolvedValueOnce(response);
  }
  vi.stubGlobal("fetch", spy);
  return spy;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("sendFirstMessage", () => {
  it("returns the send result on success without deleting the conversation", async () => {
    const fetchSpy = mockFetchSequence([
      jsonResponse({ user_message_id: "u1", assistant_message_id: "a1", task_id: "t1" }, 200),
    ]);

    const result = await sendFirstMessage(conversation, input);

    expect(result.assistant_message_id).toBe("a1");
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it("recycles the empty conversation when the first send fails", async () => {
    const fetchSpy = mockFetchSequence([
      jsonResponse({ detail: "模型不可用" }, 500),
      new Response(null, { status: 204 }),
    ]);

    await expect(sendFirstMessage(conversation, input)).rejects.toBeInstanceOf(ApiError);
    await vi.waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(2));

    const [url, init] = fetchSpy.mock.calls[1] as [string, RequestInit];
    expect(url).toBe("/api/v1/conversations/c1");
    expect(init.method).toBe("DELETE");
  });

  it("still surfaces the original send error when the recycle itself fails", async () => {
    const fetchSpy = mockFetchSequence([
      jsonResponse({ detail: "模型不可用" }, 500),
      jsonResponse({ detail: "删除失败" }, 500),
    ]);

    await expect(sendFirstMessage(conversation, input)).rejects.toMatchObject({
      status: 500,
      message: "模型不可用",
    });
    await vi.waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(2));
  });
});
