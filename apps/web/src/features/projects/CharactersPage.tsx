import { useState, type FormEvent } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createCharacter,
  deleteCharacter,
  listCharacters,
  updateCharacter,
  type Character,
} from "../../lib/api/characters";
import { listProjects } from "../../lib/api/projects";
import { listConflicts, resolveConflict, type ConflictEvidence } from "../../lib/api/conflicts";
import { ApiError } from "../../lib/api/client";
import { PlusIcon, TrashIcon, XIcon } from "../../components/ui/icons";

const inputClass =
  "h-10 w-full rounded-xl border border-line bg-white px-3.5 text-sm text-ink outline-none transition-colors placeholder:text-ink-secondary focus:border-ink-secondary";

const actionButtonClass =
  "rounded-lg border border-line bg-white px-3 py-1.5 text-xs text-ink transition-colors hover:bg-hover disabled:cursor-not-allowed disabled:opacity-50";

interface Feedback {
  ok: boolean;
  text: string;
}

function errorText(err: unknown): string {
  return err instanceof ApiError ? err.message : "操作失败，请稍后重试";
}

/** Comma-separated input (both , and ，accepted) -> trimmed alias list. */
function parseAliases(text: string): string[] {
  return text
    .split(/[,，]/)
    .map((alias) => alias.trim())
    .filter(Boolean);
}

/** 第 X 章 / 第 X–Y 章; null when the character has not appeared yet. */
function chapterRange(character: Character): string | null {
  if (character.first_seen_chapter === null) return null;
  const last = character.last_seen_chapter ?? character.first_seen_chapter;
  return last > character.first_seen_chapter
    ? `第 ${character.first_seen_chapter}–${last} 章`
    : `第 ${character.first_seen_chapter} 章`;
}

/** Shared form for creating and editing a character. */
function CharacterForm({
  initial,
  busy,
  submitLabel,
  onSubmit,
  onCancel,
}: {
  initial?: Character;
  busy: boolean;
  submitLabel: string;
  onSubmit: (values: { name: string; aliases: string[]; role: string; summary: string }) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState(initial?.name ?? "");
  const [aliasesText, setAliasesText] = useState((initial?.aliases ?? []).join(", "));
  const [role, setRole] = useState(initial?.role ?? "");
  const [summary, setSummary] = useState(initial?.summary ?? "");

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!name.trim() || busy) return;
    onSubmit({ name: name.trim(), aliases: parseAliases(aliasesText), role: role.trim(), summary: summary.trim() });
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-1.5">
          <label htmlFor="character-name" className="text-sm text-ink">名字</label>
          <input
            id="character-name"
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            className={inputClass}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <label htmlFor="character-role" className="text-sm text-ink">
            角色定位 <span className="text-ink-secondary">（可选，如 主角 / 反派）</span>
          </label>
          <input id="character-role" value={role} onChange={(e) => setRole(e.target.value)} className={inputClass} />
        </div>
      </div>
      <div className="flex flex-col gap-1.5">
        <label htmlFor="character-aliases" className="text-sm text-ink">
          别名 <span className="text-ink-secondary">（可选，多个用逗号分隔）</span>
        </label>
        <input
          id="character-aliases"
          placeholder="小王, 王总"
          value={aliasesText}
          onChange={(e) => setAliasesText(e.target.value)}
          className={inputClass}
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <label htmlFor="character-summary" className="text-sm text-ink">
          摘要 <span className="text-ink-secondary">（可选）</span>
        </label>
        <textarea
          id="character-summary"
          rows={3}
          value={summary}
          onChange={(e) => setSummary(e.target.value)}
          className="w-full resize-y rounded-xl border border-line bg-white px-3.5 py-2.5 text-sm text-ink outline-none transition-colors placeholder:text-ink-secondary focus:border-ink-secondary"
        />
      </div>
      <div className="flex items-center gap-2">
        <button
          type="submit"
          disabled={busy}
          className="h-9 rounded-xl bg-ink px-4 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {busy ? "保存中…" : submitLabel}
        </button>
        <button type="button" onClick={onCancel} disabled={busy} className={actionButtonClass}>
          取消
        </button>
      </div>
    </form>
  );
}

/** Best-effort parse of the backend's evidence_json string. */
function parseEvidence(json: string): ConflictEvidence {
  try {
    return JSON.parse(json) as ConflictEvidence;
  } catch {
    return {};
  }
}

/**
 * Open setting conflicts, shown at the top of the characters page. Renders
 * nothing while loading, on error, or when no conflict is open — no empty
 * shell. Tone: a hint, not a warning (heuristic detection, may misfire).
 */
function ConflictsSection({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const conflictsQuery = useQuery({
    queryKey: ["conflicts", projectId],
    queryFn: () => listConflicts(projectId),
    retry: false,
  });
  const [busyId, setBusyId] = useState<string | null>(null);
  const [notice, setNotice] = useState<Feedback | null>(null);

  async function handleResolve(conflictId: string) {
    if (busyId) return;
    if (!window.confirm("确认已人工核对并解决该冲突？标记后不再显示。")) return;
    setBusyId(conflictId);
    setNotice(null);
    try {
      await resolveConflict(projectId, conflictId, "人工核对已解决");
      await queryClient.invalidateQueries({ queryKey: ["conflicts", projectId] });
    } catch (err) {
      setNotice({ ok: false, text: `操作失败：${errorText(err)}` });
    } finally {
      setBusyId(null);
    }
  }

  const conflicts = conflictsQuery.data ?? [];
  if (!conflictsQuery.isSuccess || conflicts.length === 0) return null;

  return (
    <section className="mb-6">
      <h2 className="mb-1 text-base font-semibold text-ink">设定冲突</h2>
      <p className="mb-3 text-xs text-ink-secondary">
        AI 检测到新章节与已有设定可能不一致，请人工核对，系统不会自动修改设定
      </p>
      {notice && <p className={`mb-3 text-sm ${notice.ok ? "text-emerald-600" : "text-red-600"}`}>{notice.text}</p>}
      <ul className="flex flex-col gap-3">
        {conflicts.map((conflict) => {
          const evidence = parseEvidence(conflict.evidence_json);
          return (
            <li key={conflict.id} className="rounded-2xl border border-amber-200 bg-amber-50 px-5 py-4">
              <div className="flex items-start gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <p className="text-sm font-medium text-ink">{conflict.field_or_claim}</p>
                    {evidence.chapter_no !== undefined && (
                      <span className="shrink-0 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-700">
                        第 {evidence.chapter_no} 章
                      </span>
                    )}
                  </div>
                  <div className="mt-2 flex flex-col gap-1 text-xs">
                    <p className="flex gap-2">
                      <span className="shrink-0 text-ink-secondary">新章写的值</span>
                      <span className="text-ink">{evidence.candidate_value ?? "—"}</span>
                    </p>
                    <p className="flex gap-2">
                      <span className="shrink-0 text-ink-secondary">现有设定的值</span>
                      <span className="text-ink">{evidence.existing_value ?? "—"}</span>
                    </p>
                  </div>
                </div>
                <button
                  type="button"
                  disabled={busyId === conflict.id}
                  onClick={() => void handleResolve(conflict.id)}
                  className={`${actionButtonClass} shrink-0`}
                >
                  标记已解决
                </button>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/**
 * Character management for one work project. Entry: the sidebar project row's
 * "···" menu (work mode only — chat mode has no project list, and the backend
 * 404s chat projects anyway).
 */
export function CharactersPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const charactersQuery = useQuery({
    queryKey: ["characters", projectId],
    queryFn: () => listCharacters(projectId as string),
    enabled: Boolean(projectId),
    retry: false,
  });
  // Project title for the subtitle; rides the shared work-projects cache.
  const projectsQuery = useQuery({ queryKey: ["projects", "work"], queryFn: () => listProjects("work"), staleTime: 60_000 });
  const project = (projectsQuery.data ?? []).find((item) => item.id === projectId);

  const characters = charactersQuery.data ?? [];

  const [notice, setNotice] = useState<Feedback | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);

  async function invalidate() {
    await queryClient.invalidateQueries({ queryKey: ["characters", projectId] });
  }

  async function handleCreate(values: { name: string; aliases: string[]; role: string; summary: string }) {
    setBusyId("new");
    setNotice(null);
    try {
      await createCharacter(projectId as string, {
        name: values.name,
        ...(values.aliases.length > 0 ? { aliases: values.aliases } : {}),
        ...(values.role ? { role: values.role } : {}),
        ...(values.summary ? { summary: values.summary } : {}),
      });
      setNotice({ ok: true, text: `已新建角色「${values.name}」` });
      setCreateOpen(false);
      await invalidate();
    } catch (err) {
      // 409 duplicate name: the backend's message is shown verbatim.
      setNotice({ ok: false, text: `新建失败：${errorText(err)}` });
    } finally {
      setBusyId(null);
    }
  }

  async function handleUpdate(character: Character, values: { name: string; aliases: string[]; role: string; summary: string }) {
    setBusyId(character.id);
    setNotice(null);
    try {
      await updateCharacter(projectId as string, character.id, {
        name: values.name,
        aliases: values.aliases,
        role: values.role,
        summary: values.summary,
      });
      setNotice({ ok: true, text: `已保存「${values.name}」` });
      setEditingId(null);
      await invalidate();
    } catch (err) {
      setNotice({ ok: false, text: `保存失败：${errorText(err)}` });
    } finally {
      setBusyId(null);
    }
  }

  async function handleDelete(character: Character) {
    if (busyId || !window.confirm(`确定删除角色「${character.name}」？`)) return;
    setBusyId(character.id);
    setNotice(null);
    try {
      await deleteCharacter(projectId as string, character.id);
      setNotice({ ok: true, text: `已删除「${character.name}」` });
      await invalidate();
    } catch (err) {
      setNotice({ ok: false, text: `删除失败：${errorText(err)}` });
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="w-full px-8 py-10">
      {/* Close: back to where the user came from */}
      <button
        type="button"
        title="关闭角色页"
        aria-label="关闭角色页"
        onClick={() => navigate(-1)}
        className="fixed right-6 top-6 flex h-9 w-9 items-center justify-center rounded-lg text-ink-secondary transition-colors hover:bg-hover hover:text-ink"
      >
        <XIcon size={20} />
      </button>

      <h1 className="mb-1 text-2xl font-bold text-ink">角色</h1>
      <p className="mb-6 text-sm text-ink-secondary">{project ? `项目「${project.title}」的角色档案` : "项目角色档案"}</p>

      <div className="max-w-[720px]">
        <ConflictsSection projectId={projectId as string} />

        {notice && <p className={`mb-3 text-sm ${notice.ok ? "text-emerald-600" : "text-red-600"}`}>{notice.text}</p>}

        <div className="mb-4">
          {createOpen ? (
            <div className="rounded-2xl border border-line bg-white p-5">
              <CharacterForm
                busy={busyId === "new"}
                submitLabel="新建角色"
                onSubmit={(values) => void handleCreate(values)}
                onCancel={() => setCreateOpen(false)}
              />
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setCreateOpen(true)}
              className="flex items-center gap-1.5 rounded-xl bg-ink px-4 py-2 text-sm font-medium text-white transition-opacity hover:opacity-90"
            >
              <PlusIcon size={15} />
              新建角色
            </button>
          )}
        </div>

        {charactersQuery.isPending ? (
          <p className="text-sm text-ink-secondary">加载中…</p>
        ) : charactersQuery.isError ? (
          <p className="text-sm text-red-600">角色加载失败：{errorText(charactersQuery.error)}</p>
        ) : characters.length === 0 ? (
          <p className="rounded-2xl border border-line bg-white px-5 py-8 text-center text-sm text-ink-secondary">
            还没有角色。采纳章节后 AI 会自动提取角色，也可以手动新建。
          </p>
        ) : (
          <ul className="flex flex-col gap-3">
            {characters.map((character) => {
              const range = chapterRange(character);
              return (
                <li key={character.id} className="rounded-2xl border border-line bg-white px-5 py-4">
                  {editingId === character.id ? (
                    <CharacterForm
                      initial={character}
                      busy={busyId === character.id}
                      submitLabel="保存"
                      onSubmit={(values) => void handleUpdate(character, values)}
                      onCancel={() => setEditingId(null)}
                    />
                  ) : (
                    <div>
                      <div className="flex items-center gap-2">
                        <p className="truncate text-sm font-medium text-ink" title={character.name}>
                          {character.name}
                        </p>
                        {character.role && (
                          <span className="shrink-0 rounded bg-hover px-1.5 py-0.5 text-[10px] text-ink-secondary">
                            {character.role}
                          </span>
                        )}
                        {/* AI-extracted until a human edits (backend promotes to user). */}
                        <span
                          className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] ${
                            character.source === "user" ? "bg-emerald-50 text-emerald-600" : "bg-hover text-ink-secondary"
                          }`}
                        >
                          {character.source === "user" ? "已确认" : "AI 提取"}
                        </span>
                        <span className="ml-auto flex shrink-0 items-center gap-2">
                          {range && <span className="text-xs text-ink-secondary">{range}</span>}
                          <button
                            type="button"
                            disabled={busyId === character.id}
                            onClick={() => setEditingId(character.id)}
                            className={actionButtonClass}
                          >
                            编辑
                          </button>
                          <button
                            type="button"
                            title="删除"
                            disabled={busyId === character.id}
                            onClick={() => void handleDelete(character)}
                            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-ink-secondary transition-colors hover:bg-hover hover:text-red-600 disabled:opacity-50"
                          >
                            <TrashIcon size={16} />
                          </button>
                        </span>
                      </div>
                      {character.aliases.length > 0 && (
                        <div className="mt-2 flex flex-wrap gap-1.5">
                          {character.aliases.map((alias) => (
                            <span key={alias} className="rounded-full border border-line px-2.5 py-0.5 text-xs text-ink-secondary">
                              {alias}
                            </span>
                          ))}
                        </div>
                      )}
                      {character.summary && (
                        <p className="mt-2 line-clamp-3 text-xs leading-5 text-ink-secondary" title={character.summary}>
                          {character.summary}
                        </p>
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
