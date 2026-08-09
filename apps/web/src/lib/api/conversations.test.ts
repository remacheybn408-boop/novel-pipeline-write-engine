/**
 * Regression tests for the message-control endpoints wired in M10
 * (stop / retry / continue / regenerate / edit-and-fork). Each helper must
 * hit the exact backend route with the expected method and body.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  continueMessage,
  editMessage,
  regenerateMessage,
  retryMessage,
  stopMessage,
} from "./conversations";

function mockFetch(payload: unknown, status = 200) {
  const spy = vi.fn().mockResolvedValue(
    new Response(JSON.stringify(payload), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", spy);
  return spy;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("message control api", () => {
  it("stopMessage POSTs to the v1 stop route without a body", async () => {
    const fetchSpy = mockFetch({ id: "m1", status: "CANCELLED" });

    const result = await stopMessage("m1");

    expect(result).toEqual({ id: "m1", status: "CANCELLED" });
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/messages/m1/stop");
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
  });

  it("retryMessage POSTs to the v1 retry route with an empty override by default", async () => {
    const fetchSpy = mockFetch({ id: "m2", status: "PENDING", task_id: "t1" });

    const result = await retryMessage("m2");

    expect(result).toEqual({ id: "m2", status: "PENDING", task_id: "t1" });
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/messages/m2/retry");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({});
  });

  it("retryMessage forwards an explicit target-model override", async () => {
    const fetchSpy = mockFetch({ id: "m2", status: "PENDING", task_id: "t2" });

    await retryMessage("m2", { provider: "anthropic", model: "claude-sonnet-4", reasoning_level: "high" });

    const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      provider: "anthropic",
      model: "claude-sonnet-4",
      reasoning_level: "high",
    });
  });

  it("continueMessage POSTs to the v1 continue route", async () => {
    const fetchSpy = mockFetch({ id: "m3", status: "PARTIAL", task_id: "t3" });

    await continueMessage("m3");

    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/messages/m3/continue");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({});
  });

  it("regenerateMessage POSTs to the v2 regenerate route", async () => {
    const fetchSpy = mockFetch({ message_id: "m5", task_id: "t4" });

    const result = await regenerateMessage("c1", "m4");

    expect(result).toEqual({ message_id: "m5", task_id: "t4" });
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v2/conversations/c1/messages/m4/regenerate");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({});
  });

  it("editMessage POSTs the edited content to the v2 edit route", async () => {
    const fetchSpy = mockFetch({ branch_id: "b2", source_message_id: "m6", replacement_message_id: "m7" });

    const result = await editMessage("c1", "m6", "改写后的内容");

    expect(result.branch_id).toBe("b2");
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v2/conversations/c1/messages/m6/edit");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ content: "改写后的内容" });
  });
});
