// @vitest-environment jsdom
/**
 * First-send with attachments (HomePage): every file uploads BEFORE the
 * message goes out and the send body carries attachment_ids; an upload
 * failure aborts the send entirely (no message without its files).
 * The composer stays real (paste-to-attach is exercised); heavy children
 * and the projects API are mocked.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.stubGlobal("localStorage", {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
});

vi.mock("../../components/composer/ModelSelect", () => ({ ModelSelect: () => null, loadSelectedModel: () => null }));
vi.mock("../../components/composer/ReasoningSelect", () => ({ ReasoningSelect: () => null, loadReasoningLevel: () => null }));
vi.mock("../../components/composer/ContextRing", () => ({ ContextRing: () => null }));
vi.mock("../../components/composer/useSwarmContextWindow", () => ({ useSwarmContextWindow: () => null }));
vi.mock("../../components/composer/ProjectPicker", () => ({ ProjectPicker: () => null }));
vi.mock("../../lib/api/plugins", () => ({ listMcpServers: vi.fn(async () => []), listSkills: vi.fn(async () => []) }));
vi.mock("../../lib/api/projects", () => ({
  listProjects: vi.fn(async () => []),
  // The composer starts with no stored pick, so the send auto-creates one.
  createProject: vi.fn(async () => ({ id: "p1", slug: "proj", title: "Proj", mode: "work" })),
  getProjectClusterConfig: vi.fn(),
  slugify: (value: string) => value,
}));

const { HomePage } = await import("./HomePage");

function renderHome() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <HomePage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
}

/** fetch mock routing by URL and recording "METHOD url" call order. */
function mockApi(calls: string[], overrides: Record<string, Response> = {}) {
  const spy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push(`${init?.method ?? "GET"} ${url}`);
    if (url in overrides) return overrides[url];
    if (url === "/api/v1/projects/p1/files") return jsonResponse({ id: "f1", filename: "notes.txt", storage_key: "k" }, 201);
    if (url === "/api/v1/conversations") return jsonResponse({ id: "c1", branch_id: "b1", title: "t" });
    if (url === "/api/v1/conversations/c1/messages") return jsonResponse({ user_message_id: "u1", assistant_message_id: "a1", task_id: "t1" });
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", spy);
  return spy;
}

async function typeAndAttach() {
  const textarea = await screen.findByPlaceholderText("输入消息，Enter 发送");
  fireEvent.change(textarea, { target: { value: "帮我总结附件" } });
  fireEvent.paste(textarea, { clipboardData: { files: [new File(["body"], "notes.txt")] } });
  await screen.findByText("notes.txt");
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("HomePage send with attachments", () => {
  it("uploads files before sending and passes attachment_ids", async () => {
    const calls: string[] = [];
    const fetchSpy = mockApi(calls);
    renderHome();
    await typeAndAttach();

    fireEvent.click(screen.getByTitle("发送"));
    await waitFor(() => expect(calls).toContain("POST /api/v1/conversations/c1/messages"));

    const uploadIndex = calls.indexOf("POST /api/v1/projects/p1/files");
    const sendIndex = calls.indexOf("POST /api/v1/conversations/c1/messages");
    expect(uploadIndex).toBeGreaterThanOrEqual(0);
    expect(sendIndex).toBeGreaterThan(uploadIndex);
    const sendCall = fetchSpy.mock.calls.find(([url]) => String(url) === "/api/v1/conversations/c1/messages");
    const body = JSON.parse(String((sendCall?.[1] as RequestInit | undefined)?.body)) as { attachment_ids?: string[] };
    expect(body.attachment_ids).toEqual(["f1"]);
  });

  it("aborts the send when an upload fails", async () => {
    const calls: string[] = [];
    mockApi(calls, { "/api/v1/projects/p1/files": jsonResponse({ detail: "上传失败" }, 500) });
    renderHome();
    await typeAndAttach();

    fireEvent.click(screen.getByTitle("发送"));
    await screen.findByText("上传失败");

    expect(calls).toContain("POST /api/v1/projects/p1/files");
    expect(calls).not.toContain("POST /api/v1/conversations/c1/messages");
  });
});
