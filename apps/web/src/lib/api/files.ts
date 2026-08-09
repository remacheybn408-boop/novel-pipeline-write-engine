/**
 * Project file endpoints — proseforge/api/routes/files.py
 *
 * The shared client.ts request() is JSON-only, so multipart uploads use a
 * native fetch with the same cookie-session credentials.
 */
import { ApiError } from "./client";

export interface ProjectFile {
  id: string;
  filename: string;
  storage_key: string;
}

export async function uploadProjectFile(projectId: string, file: File): Promise<ProjectFile> {
  const form = new FormData();
  form.append("file", file);
  const response = await fetch(`/api/v1/projects/${projectId}/files`, {
    method: "POST",
    body: form,
    credentials: "include",
  });
  if (!response.ok) {
    let message = `上传失败（HTTP ${response.status}）`;
    try {
      const data: unknown = await response.json();
      if (data && typeof (data as { detail?: unknown }).detail === "string") {
        message = (data as { detail: string }).detail;
      }
    } catch {
      // Keep the default message when the body is not JSON.
    }
    throw new ApiError(response.status, message);
  }
  return (await response.json()) as ProjectFile;
}

/**
 * Upload every pending attachment and return the created ids, ready for
 * SendMessageInput.attachment_ids. All-or-nothing: the first failure rejects
 * so the caller aborts the send instead of posting a message missing files.
 */
export async function uploadAttachmentIds(projectId: string, files: File[]): Promise<string[]> {
  const uploaded = await Promise.all(files.map((file) => uploadProjectFile(projectId, file)));
  return uploaded.map((file) => file.id);
}
