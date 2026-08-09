/**
 * Knowledge base endpoints — proseforge/api/routes/knowledge.py (work-mode
 * projects only; chat projects and foreign projects 404).
 *
 * Confirmed shapes:
 *   GET    /api/v1/projects/{project_id}/knowledge-base -> KnowledgeDocument[]
 *   POST   same  {title, content?} -> 201 KnowledgeDocument
 *   GET    /api/v1/projects/{project_id}/knowledge-base/{document_id} -> KnowledgeDocument
 *   PATCH  same  {title?, content?} -> KnowledgeDocument
 *   DELETE same -> 204
 */
import { request } from "./client";

export interface KnowledgeDocument {
  id: string;
  project_id: string;
  title: string;
  content: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface KnowledgeDocumentInput {
  title: string;
  content?: string;
}

export type KnowledgeDocumentPatch = Partial<KnowledgeDocumentInput>;

export function listKnowledgeDocuments(projectId: string): Promise<KnowledgeDocument[]> {
  return request<KnowledgeDocument[]>(`/api/v1/projects/${projectId}/knowledge-base`);
}

export function createKnowledgeDocument(
  projectId: string,
  input: KnowledgeDocumentInput,
): Promise<KnowledgeDocument> {
  return request<KnowledgeDocument>(`/api/v1/projects/${projectId}/knowledge-base`, {
    method: "POST",
    body: input,
  });
}

export function updateKnowledgeDocument(
  projectId: string,
  documentId: string,
  patch: KnowledgeDocumentPatch,
): Promise<KnowledgeDocument> {
  return request<KnowledgeDocument>(`/api/v1/projects/${projectId}/knowledge-base/${documentId}`, {
    method: "PATCH",
    body: patch,
  });
}

export function deleteKnowledgeDocument(projectId: string, documentId: string): Promise<void> {
  return request<void>(`/api/v1/projects/${projectId}/knowledge-base/${documentId}`, {
    method: "DELETE",
  });
}
