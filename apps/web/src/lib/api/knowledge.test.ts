/**
 * Contract tests for the knowledge-base API client.
 *
 * The backend contract lives in proseforge/api/routes/knowledge.py:
 *   GET    /api/v1/projects/{project_id}/knowledge-base
 *   POST   same
 *   PATCH  /api/v1/projects/{project_id}/knowledge-base/{document_id} {title?, content?}
 *   DELETE same -> 204
 * These tests pin the client to that contract, in particular that the edit
 * entry on KnowledgePage sends a PATCH (previously updateKnowledgeDocument
 * was dead code with no caller).
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import { request } from "./client";
import {
  createKnowledgeDocument,
  deleteKnowledgeDocument,
  listKnowledgeDocuments,
  updateKnowledgeDocument,
} from "./knowledge";

vi.mock("./client", () => ({
  request: vi.fn(),
  ApiError: class ApiError extends Error {},
}));

const mockedRequest = vi.mocked(request);

beforeEach(() => {
  mockedRequest.mockReset();
});

describe("knowledge api client", () => {
  it("lists documents for a project", async () => {
    mockedRequest.mockResolvedValue([]);
    await listKnowledgeDocuments("proj-1");
    expect(mockedRequest).toHaveBeenCalledWith("/api/v1/projects/proj-1/knowledge-base");
  });

  it("creates a document via POST", async () => {
    mockedRequest.mockResolvedValue({ id: "doc-1" });
    await createKnowledgeDocument("proj-1", { title: "设定集", content: "正文" });
    expect(mockedRequest).toHaveBeenCalledWith("/api/v1/projects/proj-1/knowledge-base", {
      method: "POST",
      body: { title: "设定集", content: "正文" },
    });
  });

  it("updates a document via PATCH with title and content", async () => {
    mockedRequest.mockResolvedValue({ id: "doc-1" });
    await updateKnowledgeDocument("proj-1", "doc-1", { title: "新标题", content: "新正文" });
    expect(mockedRequest).toHaveBeenCalledWith(
      "/api/v1/projects/proj-1/knowledge-base/doc-1",
      { method: "PATCH", body: { title: "新标题", content: "新正文" } },
    );
  });

  it("updates a document via PATCH with a partial body", async () => {
    mockedRequest.mockResolvedValue({ id: "doc-1" });
    await updateKnowledgeDocument("proj-1", "doc-1", { content: "仅改正文" });
    expect(mockedRequest).toHaveBeenCalledWith(
      "/api/v1/projects/proj-1/knowledge-base/doc-1",
      { method: "PATCH", body: { content: "仅改正文" } },
    );
  });

  it("deletes a document via DELETE", async () => {
    mockedRequest.mockResolvedValue(undefined);
    await deleteKnowledgeDocument("proj-1", "doc-1");
    expect(mockedRequest).toHaveBeenCalledWith(
      "/api/v1/projects/proj-1/knowledge-base/doc-1",
      { method: "DELETE" },
    );
  });
});
