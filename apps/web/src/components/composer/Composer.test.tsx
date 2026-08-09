// @vitest-environment jsdom
/**
 * Composer attachments: paste-to-attach (whitelist filtered), the chip row
 * and chip removal. Heavy composer children are mocked; the attachment UI
 * under test is the real thing.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

// ViewModeContext reads localStorage at module load; stub it before import.
vi.stubGlobal("localStorage", {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
});

vi.mock("./ModelSelect", () => ({ ModelSelect: () => null, loadSelectedModel: () => null }));
vi.mock("./ReasoningSelect", () => ({ ReasoningSelect: () => null }));
vi.mock("./ContextRing", () => ({ ContextRing: () => null }));
vi.mock("./useSwarmContextWindow", () => ({ useSwarmContextWindow: () => null }));
vi.mock("../../lib/api/plugins", () => ({ listMcpServers: vi.fn(async () => []), listSkills: vi.fn(async () => []) }));

const { Composer, isSupportedAttachment } = await import("./Composer");

/** Controlled wrapper mirroring how the pages wire the composer. */
function Harness({ withAttachments = true }: { withAttachments?: boolean }) {
  const [value, setValue] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  if (!withAttachments) return <Composer value={value} onChange={setValue} onSend={() => {}} />;
  return <Composer value={value} onChange={setValue} onSend={() => {}} attachments={files} onAttachmentsChange={setFiles} />;
}

function renderComposer(withAttachments = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Harness withAttachments={withAttachments} />
    </QueryClientProvider>,
  );
}

function pasteFiles(textarea: HTMLElement, files: File[]) {
  fireEvent.paste(textarea, { clipboardData: { files } });
}

afterEach(cleanup);

describe("isSupportedAttachment", () => {
  it("accepts whitelist extensions case-insensitively", () => {
    expect(isSupportedAttachment(new File([""], "notes.TXT"))).toBe(true);
    expect(isSupportedAttachment(new File([""], "deck.pdf"))).toBe(true);
    expect(isSupportedAttachment(new File([""], "book.docx"))).toBe(true);
  });

  it("rejects images and other unknown types", () => {
    expect(isSupportedAttachment(new File([""], "photo.png"))).toBe(false);
    expect(isSupportedAttachment(new File([""], "archive.zip"))).toBe(false);
  });
});

describe("Composer attachments", () => {
  it("paste adds whitelist files as chips and skips the rest", () => {
    renderComposer();
    pasteFiles(screen.getByPlaceholderText("输入消息，Enter 发送"), [
      new File(["hello"], "notes.txt", { type: "text/plain" }),
      new File([""], "photo.png", { type: "image/png" }),
    ]);
    expect(screen.getByText("notes.txt")).toBeTruthy();
    expect(screen.queryByText("photo.png")).toBeNull();
  });

  it("the chip remove button drops the attachment", () => {
    renderComposer();
    pasteFiles(screen.getByPlaceholderText("输入消息，Enter 发送"), [new File(["hello"], "notes.txt")]);
    expect(screen.getByText("notes.txt")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("移除附件 notes.txt"));
    expect(screen.queryByText("notes.txt")).toBeNull();
  });

  it("shows the paperclip button only when attachments are wired up", () => {
    renderComposer(true);
    expect(screen.getByLabelText("添加附件")).toBeTruthy();
    cleanup();
    renderComposer(false);
    expect(screen.queryByLabelText("添加附件")).toBeNull();
  });
});
