import { describe, expect, it } from "vitest";
import { accountEntryFor } from "./LoginPage";

// Login page account-creation entry: setup for fresh instances, register
// when the instance allows it, plain login otherwise.
describe("accountEntryFor", () => {
  it("offers setup on a fresh instance (not initialized)", () => {
    expect(accountEntryFor({ enabled: false, initialized: false })).toBe("setup");
    expect(accountEntryFor({ enabled: true, initialized: false })).toBe("setup");
  });

  it("offers register once initialized and registration is open", () => {
    expect(accountEntryFor({ enabled: true, initialized: true })).toBe("register");
  });

  it("offers no creation entry once initialized and registration is closed", () => {
    expect(accountEntryFor({ enabled: false, initialized: true })).toBe("none");
  });

  it("shows no creation entry while the status is unavailable (loading/error)", () => {
    expect(accountEntryFor(null)).toBe("loading");
    expect(accountEntryFor(undefined)).toBe("loading");
  });
});
