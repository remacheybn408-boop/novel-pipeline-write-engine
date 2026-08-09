import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createProject, getProjectClusterConfig, listProjects, slugify, type Project } from "../../lib/api/projects";
import {
  createConversation,
  deleteConversation,
  sendMessage,
  type Conversation,
  type SendMessageInput,
  type SendMessageResult,
} from "../../lib/api/conversations";
import { ApiError } from "../../lib/api/client";
import { uploadAttachmentIds } from "../../lib/api/files";
import { uuid } from "../../lib/uuid";
import { Composer } from "../../components/composer/Composer";
import { ProjectPicker } from "../../components/composer/ProjectPicker";
import { loadSelectedModel } from "../../components/composer/ModelSelect";
import { loadReasoningLevel } from "../../components/composer/ReasoningSelect";
import { LEGACY_SELECTED_PROJECT_KEY, selectedProjectKey, useViewMode } from "../../app/ViewModeContext";

/**
 * Send the first message into a freshly created conversation. If the send
 * fails, recycle the empty conversation so failed attempts do not pile up
 * ghost entries in the sidebar; a failed recycle is silent — the send error
 * is what the user needs to see.
 */
export async function sendFirstMessage(
  conversation: Conversation,
  input: SendMessageInput,
): Promise<SendMessageResult> {
  try {
    return await sendMessage(conversation.id, input);
  } catch (err) {
    void deleteConversation(conversation.id).catch(() => {});
    throw err;
  }
}

export function HomePage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { viewMode, chatMode } = useViewMode();
  const projectsQuery = useQuery({ queryKey: ["projects", viewMode], queryFn: () => listProjects(viewMode) });

  const [value, setValue] = useState("");
  const [attachments, setAttachments] = useState<File[]>([]);
  const [selectedProject, setSelectedProject] = useState<Project | null>(null);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);

  const projects = projectsQuery.data ?? [];

  // Swarm guard (work mode only): when the project's effective cluster config
  // is cluster mode but the account has < 2 usable models, sending is blocked.
  const swarmActive = viewMode === "work" && chatMode === "swarm";
  const projectClusterQuery = useQuery({
    queryKey: ["project-cluster-config", selectedProject?.id],
    queryFn: () => getProjectClusterConfig(selectedProject!.id),
    enabled: swarmActive && Boolean(selectedProject),
    retry: false,
  });
  const swarmConfig = projectClusterQuery.data;
  const swarmBlocked =
    swarmActive && Boolean(selectedProject) && swarmConfig?.mode === "cluster" && swarmConfig.available_models < 2;

  // Clear the selection when switching modes so the per-mode pick is restored.
  useEffect(() => {
    setSelectedProject(null);
  }, [viewMode]);

  // Restore the stored pick once projects load; fall back to the first project.
  useEffect(() => {
    if (!projectsQuery.data || selectedProject) return;
    const storedId =
      localStorage.getItem(selectedProjectKey(viewMode)) ?? localStorage.getItem(LEGACY_SELECTED_PROJECT_KEY);
    const restored = projects.find((p) => p.id === storedId) ?? projects[0] ?? null;
    setSelectedProject(restored);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectsQuery.data, viewMode]);

  function handleSelectProject(project: Project) {
    setSelectedProject(project);
    localStorage.setItem(selectedProjectKey(viewMode), project.id);
    setSendError(null);
  }

  async function handleSend() {
    const content = value.trim();
    if (!content || sending || projectsQuery.isPending || swarmBlocked) return;

    setSending(true);
    setSendError(null);
    try {
      // Auto-create a default project when the current mode has none, so
      // sending never blocks on manual project setup.
      let project = selectedProject;
      if (!project) {
        const title = viewMode === "chat" ? "新聊天" : "未命名项目";
        project = await createProject({
          slug: slugify(`${title}-${Date.now().toString(36)}`),
          title,
          mode: viewMode,
        });
        setSelectedProject(project);
        localStorage.setItem(selectedProjectKey(viewMode), project.id);
        await queryClient.invalidateQueries({ queryKey: ["projects"] });
      }

      // Swarm goes through the normal conversation pipeline: the backend
      // routes intent (chitchat = normal reply, writing = starts a run) and
      // the chat page shows the live workbench. Models come from the cluster
      // config, never from the composer picker.
      const model = loadSelectedModel();
      // Attachments upload before the conversation exists; any failure aborts
      // the send (no message without its files, no ghost conversation).
      let attachmentIds: string[] = [];
      if (attachments.length > 0) {
        attachmentIds = await uploadAttachmentIds(project.id, attachments);
      }
      const conversation = await createConversation(project.id, content.slice(0, 30));
      // The sidebar rides the ["conversations", viewMode] cache and never
      // remounts inside the shell — without this a brand-new conversation
      // only appeared after a manual page refresh.
      await queryClient.invalidateQueries({ queryKey: ["conversations"] });
      const result = await sendFirstMessage(conversation, {
        branch_id: conversation.branch_id,
        content,
        client_request_id: uuid(),
        ...(swarmActive ? { mode: "swarm" as const } : {}),
        ...(attachmentIds.length > 0 ? { attachment_ids: attachmentIds } : {}),
        ...(model
          ? {
              provider: model.provider,
              model: model.model_id,
              reasoning_level: loadReasoningLevel(model.provider, model.model_id) ?? "auto",
            }
          : {}),
      });
      navigate(`/chat/${conversation.id}`, {
        state: { branchId: conversation.branch_id, assistantMessageId: result.assistant_message_id },
      });
    } catch (err) {
      setSendError(err instanceof ApiError ? err.message : "发送失败，请稍后重试");
      setSending(false);
    }
  }

  return (
    <div className="relative flex min-h-full flex-col items-center justify-center px-8 pb-24">
      {/* Wordmark with a cinnabar seal accent */}
      <h1 className="relative mb-10 select-none text-[88px] font-extrabold leading-none tracking-[-0.03em] text-[#d8d8d8]">
        ProseForge
        <span className="absolute -right-9 top-1 flex h-[22px] w-[22px] rotate-3 items-center justify-center rounded-[4px] bg-[#b03a2e] font-serif text-[13px] leading-none text-white shadow-[0_1px_3px_rgba(176,58,46,0.4)]">
          文
        </span>
      </h1>

      <div className="relative w-full max-w-[760px]">
        <Composer
          value={value}
          onChange={setValue}
          onSend={handleSend}
          sending={sending || swarmBlocked}
          usedTokens={0}
          attachments={attachments}
          onAttachmentsChange={setAttachments}
          footer={
            viewMode === "work" ? (
              <ProjectPicker selected={selectedProject} onSelect={handleSelectProject} />
            ) : undefined
          }
        />
        {swarmBlocked && (
          <p className="mt-2 px-2 text-sm text-ink-secondary">
            集群模式至少需要 2 个已配置模型，
            <button type="button" onClick={() => navigate("/plugins")} className="text-ink underline">
              去配置
            </button>
          </p>
        )}
        {sendError && <p className="mt-2 px-2 text-sm text-red-600">{sendError}</p>}
      </div>
    </div>
  );
}
