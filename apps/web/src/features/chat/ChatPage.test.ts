/**
 * Regression test for the send/switch race: sendContent guards every
 * post-await state update with isCurrentConversation, so a send that lands
 * after the user switched conversations cannot overwrite the new view.
 */
import { describe, expect, it, vi } from "vitest";

// ChatPage imports ViewModeContext, which reads localStorage at module load;
// stub it before pulling the module in (node environment has no DOM storage).
vi.stubGlobal("localStorage", {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
});

const { isCurrentConversation } = await import("./ChatPage");

describe("isCurrentConversation", () => {
  it("applies results while the route still shows the sent conversation", () => {
    expect(isCurrentConversation("c1", "c1")).toBe(true);
  });

  it("drops results once the user switched conversations", () => {
    expect(isCurrentConversation("c2", "c1")).toBe(false);
  });

  it("drops results when no conversation is routed at all", () => {
    expect(isCurrentConversation(undefined, "c1")).toBe(false);
  });
});
