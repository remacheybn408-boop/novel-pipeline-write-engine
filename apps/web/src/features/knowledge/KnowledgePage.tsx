import { useState, type FormEvent } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createKnowledgeDocument,
  deleteKnowledgeDocument,
  listKnowledgeDocuments,
  updateKnowledgeDocument,
  type KnowledgeDocument,
} from "../../lib/api/knowledge";
import { listProjects } from "../../lib/api/projects";
import { ApiError } from "../../lib/api/client";
import { FileTextIcon, PencilIcon, PlusIcon, TrashIcon, XIcon } from "../../components/ui/icons";

const inputClass =
  "h-10 w-full rounded-xl border border-line bg-white px-3.5 text-sm text-ink outline-none transition-colors placeholder:text-ink-secondary focus:border-ink-secondary";

interface Feedback {
  ok: boolean;
  text: string;
}

function errorText(err: unknown): string {
  return err instanceof ApiError ? err.message : "操作失败，请稍后重试";
}

function formatTime(iso: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", { hour12: false });
}

/**
 * Knowledge base for one work project (reserved CRUD skeleton). Entry: the
 * sidebar project row's "···" menu, next to 角色 / 集群配置.
 */
export function KnowledgePage() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const documentsQuery = useQuery({
    queryKey: ["knowledge-base", projectId],
    queryFn: () => listKnowledgeDocuments(projectId as string),
    enabled: Boolean(projectId),
    retry: false,
  });
  // Project title for the subtitle; rides the shared work-projects cache.
  const projectsQuery = useQuery({ queryKey: ["projects", "work"], queryFn: () => listProjects("work"), staleTime: 60_000 });
  const project = (projectsQuery.data ?? []).find((item) => item.id === projectId);

  const documents = documentsQuery.data ?? [];

  const [notice, setNotice] = useState<Feedback | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [editContent, setEditContent] = useState("");

  async function invalidate() {
    await queryClient.invalidateQueries({ queryKey: ["knowledge-base", projectId] });
  }

  function resetForm() {
    setTitle("");
    setContent("");
  }

  async function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!title.trim() || busyId) return;
    setBusyId("new");
    setNotice(null);
    try {
      await createKnowledgeDocument(projectId as string, {
        title: title.trim(),
        ...(content.trim() ? { content } : {}),
      });
      setNotice({ ok: true, text: `已新建文档「${title.trim()}」` });
      setCreateOpen(false);
      resetForm();
      await invalidate();
    } catch (err) {
      setNotice({ ok: false, text: `新建失败：${errorText(err)}` });
    } finally {
      setBusyId(null);
    }
  }

  async function handleDelete(document: KnowledgeDocument) {
    if (busyId || !window.confirm(`确定删除文档「${document.title}」？`)) return;
    setBusyId(document.id);
    setNotice(null);
    try {
      await deleteKnowledgeDocument(projectId as string, document.id);
      setNotice({ ok: true, text: `已删除「${document.title}」` });
      await invalidate();
    } catch (err) {
      setNotice({ ok: false, text: `删除失败：${errorText(err)}` });
    } finally {
      setBusyId(null);
    }
  }

  function startEdit(document: KnowledgeDocument) {
    setEditingId(document.id);
    setEditTitle(document.title);
    setEditContent(document.content);
  }

  function cancelEdit() {
    setEditingId(null);
    setEditTitle("");
    setEditContent("");
  }

  async function handleSaveEdit(event: FormEvent<HTMLFormElement>, document: KnowledgeDocument) {
    event.preventDefault();
    if (!editTitle.trim() || busyId) return;
    setBusyId(document.id);
    setNotice(null);
    try {
      await updateKnowledgeDocument(projectId as string, document.id, {
        title: editTitle.trim(),
        content: editContent,
      });
      setNotice({ ok: true, text: `已保存「${editTitle.trim()}」` });
      cancelEdit();
      await invalidate();
    } catch (err) {
      setNotice({ ok: false, text: `保存失败：${errorText(err)}` });
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="w-full px-8 py-10">
      {/* Close: back to where the user came from */}
      <button
        type="button"
        title="关闭知识库"
        aria-label="关闭知识库"
        onClick={() => navigate(-1)}
        className="fixed right-6 top-6 flex h-9 w-9 items-center justify-center rounded-lg text-ink-secondary transition-colors hover:bg-hover hover:text-ink"
      >
        <XIcon size={20} />
      </button>

      <h1 className="mb-1 text-2xl font-bold text-ink">知识库</h1>
      <p className="mb-6 text-sm text-ink-secondary">
        {project ? `项目「${project.title}」的知识库文档` : "项目知识库文档"}
        （预留功能：文档内容暂不参与检索与写作上下文）
      </p>

      <div className="max-w-[720px]">
        {notice && <p className={`mb-3 text-sm ${notice.ok ? "text-emerald-600" : "text-red-600"}`}>{notice.text}</p>}

        <div className="mb-4">
          {createOpen ? (
            <form onSubmit={(event) => void handleCreate(event)} className="flex flex-col gap-3 rounded-2xl border border-line bg-white p-5">
              <div className="flex flex-col gap-1.5">
                <label htmlFor="knowledge-title" className="text-sm text-ink">标题</label>
                <input
                  id="knowledge-title"
                  required
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  className={inputClass}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <label htmlFor="knowledge-content" className="text-sm text-ink">
                  内容 <span className="text-ink-secondary">（可选）</span>
                </label>
                <textarea
                  id="knowledge-content"
                  rows={5}
                  value={content}
                  onChange={(e) => setContent(e.target.value)}
                  className="w-full resize-y rounded-xl border border-line bg-white px-3.5 py-2.5 text-sm text-ink outline-none transition-colors placeholder:text-ink-secondary focus:border-ink-secondary"
                />
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="submit"
                  disabled={busyId === "new"}
                  className="h-9 rounded-xl bg-ink px-4 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
                >
                  {busyId === "new" ? "保存中…" : "新建文档"}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setCreateOpen(false);
                    resetForm();
                  }}
                  disabled={busyId === "new"}
                  className="rounded-lg border border-line bg-white px-3 py-1.5 text-xs text-ink transition-colors hover:bg-hover disabled:cursor-not-allowed disabled:opacity-50"
                >
                  取消
                </button>
              </div>
            </form>
          ) : (
            <button
              type="button"
              onClick={() => setCreateOpen(true)}
              className="flex items-center gap-1.5 rounded-xl bg-ink px-4 py-2 text-sm font-medium text-white transition-opacity hover:opacity-90"
            >
              <PlusIcon size={15} />
              新建文档
            </button>
          )}
        </div>

        {documentsQuery.isPending ? (
          <p className="text-sm text-ink-secondary">加载中…</p>
        ) : documentsQuery.isError ? (
          <p className="text-sm text-red-600">知识库加载失败：{errorText(documentsQuery.error)}</p>
        ) : documents.length === 0 ? (
          <p className="rounded-2xl border border-line bg-white px-5 py-8 text-center text-sm text-ink-secondary">
            还没有知识库文档。点击「新建文档」添加。（预留功能：文档内容暂不参与检索与写作上下文）
          </p>
        ) : (
          <ul className="flex flex-col gap-3">
            {documents.map((document) => (
              <li key={document.id} className="rounded-2xl border border-line bg-white px-5 py-4">
                {editingId === document.id ? (
                  <form onSubmit={(event) => void handleSaveEdit(event, document)} className="flex flex-col gap-3">
                    <div className="flex flex-col gap-1.5">
                      <label htmlFor={`knowledge-edit-title-${document.id}`} className="text-sm text-ink">标题</label>
                      <input
                        id={`knowledge-edit-title-${document.id}`}
                        required
                        value={editTitle}
                        onChange={(e) => setEditTitle(e.target.value)}
                        className={inputClass}
                      />
                    </div>
                    <div className="flex flex-col gap-1.5">
                      <label htmlFor={`knowledge-edit-content-${document.id}`} className="text-sm text-ink">内容</label>
                      <textarea
                        id={`knowledge-edit-content-${document.id}`}
                        rows={8}
                        value={editContent}
                        onChange={(e) => setEditContent(e.target.value)}
                        className="w-full resize-y rounded-xl border border-line bg-white px-3.5 py-2.5 text-sm text-ink outline-none transition-colors placeholder:text-ink-secondary focus:border-ink-secondary"
                      />
                    </div>
                    <div className="flex items-center gap-2">
                      <button
                        type="submit"
                        disabled={busyId === document.id}
                        className="h-9 rounded-xl bg-ink px-4 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
                      >
                        {busyId === document.id ? "保存中…" : "保存"}
                      </button>
                      <button
                        type="button"
                        onClick={cancelEdit}
                        disabled={busyId === document.id}
                        className="rounded-lg border border-line bg-white px-3 py-1.5 text-xs text-ink transition-colors hover:bg-hover disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        取消
                      </button>
                    </div>
                  </form>
                ) : (
                  <>
                    <div className="flex items-center gap-2">
                      <span className="shrink-0 text-ink-secondary">
                        <FileTextIcon size={16} />
                      </span>
                      <p className="truncate text-sm font-medium text-ink" title={document.title}>
                        {document.title}
                      </p>
                      <span className="ml-auto flex shrink-0 items-center gap-2">
                        {document.updated_at && (
                          <span className="text-xs text-ink-secondary">{formatTime(document.updated_at)}</span>
                        )}
                        <button
                          type="button"
                          onClick={() => setExpandedId(expandedId === document.id ? null : document.id)}
                          className="rounded-lg border border-line bg-white px-2.5 py-1 text-xs text-ink transition-colors hover:bg-hover"
                        >
                          {expandedId === document.id ? "收起" : "查看全文"}
                        </button>
                        <button
                          type="button"
                          title="编辑"
                          disabled={busyId === document.id}
                          onClick={() => startEdit(document)}
                          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-ink-secondary transition-colors hover:bg-hover hover:text-ink disabled:opacity-50"
                        >
                          <PencilIcon size={16} />
                        </button>
                        <button
                          type="button"
                          title="删除"
                          disabled={busyId === document.id}
                          onClick={() => void handleDelete(document)}
                          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-ink-secondary transition-colors hover:bg-hover hover:text-red-600 disabled:opacity-50"
                        >
                          <TrashIcon size={16} />
                        </button>
                      </span>
                    </div>
                    {document.content &&
                      (expandedId === document.id ? (
                        <p className="mt-2 whitespace-pre-wrap text-xs leading-5 text-ink-secondary">
                          {document.content}
                        </p>
                      ) : (
                        <p className="mt-2 line-clamp-3 text-xs leading-5 text-ink-secondary">
                          {document.content}
                        </p>
                      ))}
                  </>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
