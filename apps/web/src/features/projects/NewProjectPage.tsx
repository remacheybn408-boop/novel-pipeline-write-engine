import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { createProject, slugify } from "../../lib/api/projects";
import { uploadProjectFile } from "../../lib/api/files";
import { ApiError } from "../../lib/api/client";
import { selectedProjectKey, useViewMode } from "../../app/ViewModeContext";
import { ArrowUpIcon, FolderIcon, PlusIcon } from "../../components/ui/icons";

const MAX_IMPORT_FILES = 100;

/** Decorative folder cluster: one outlined folder with a plus, two faded behind. */
function FolderIllustration() {
  return (
    <div className="relative flex h-[130px] items-center justify-center" aria-hidden="true">
      <span className="absolute -translate-x-[120px] -rotate-6 text-line">
        <FolderIcon size={72} />
      </span>
      <span className="absolute translate-x-[120px] rotate-6 text-line">
        <FolderIcon size={72} />
      </span>
      <span className="relative text-[#d5d5d5]">
        <FolderIcon size={110} />
      </span>
      <span className="absolute bottom-0 right-[calc(50%-64px)] flex h-8 w-8 items-center justify-center rounded-full bg-ink text-white">
        <PlusIcon size={16} />
      </span>
    </div>
  );
}

/**
 * New-project page shown in the main area when the sidebar "新建项目" entry
 * is clicked. The user either types a name, or picks a local folder — the
 * folder name becomes the project name and its files are imported as
 * project attachments after creation.
 */
export function NewProjectPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { viewMode } = useViewMode();
  const folderInputRef = useRef<HTMLInputElement | null>(null);

  const [name, setName] = useState("");
  const [nameTouched, setNameTouched] = useState(false);
  const [files, setFiles] = useState<File[] | null>(null);
  const [folderName, setFolderName] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [error, setError] = useState<string | null>(null);

  function handleFolderPick(event: React.ChangeEvent<HTMLInputElement>) {
    const picked = Array.from(event.target.files ?? []);
    event.target.value = "";
    if (picked.length === 0) return;
    const root = picked[0]?.webkitRelativePath?.split("/")[0];
    setFolderName(root || null);
    if (!nameTouched && root) setName(root);
    if (picked.length > MAX_IMPORT_FILES) {
      setFiles(picked.slice(0, MAX_IMPORT_FILES));
      setNotice(`一次最多导入 ${MAX_IMPORT_FILES} 个文件，已截取前 ${MAX_IMPORT_FILES} 个`);
    } else {
      setFiles(picked);
      setNotice(null);
    }
    setError(null);
  }

  function handleClearFolder() {
    setFiles(null);
    setFolderName(null);
    setNotice(null);
  }

  async function handleCreate() {
    const title = name.trim() || folderName?.trim() || "";
    if (!title || creating) return;
    setCreating(true);
    setError(null);
    try {
      const project = await createProject({
        slug: slugify(`${title}-${Date.now().toString(36)}`),
        title,
        mode: viewMode,
      });
      if (files?.length) {
        const failures: string[] = [];
        for (let index = 0; index < files.length; index += 1) {
          setProgress({ done: index + 1, total: files.length });
          try {
            await uploadProjectFile(project.id, files[index]);
          } catch {
            failures.push(files[index].name);
          }
        }
        if (failures.length > 0) {
          window.alert(
            `项目已创建，但 ${failures.length} 个文件上传失败：${failures.slice(0, 5).join("、")}${failures.length > 5 ? " 等" : ""}`,
          );
        }
      }
      localStorage.setItem(selectedProjectKey(viewMode), project.id);
      await queryClient.invalidateQueries({ queryKey: ["projects"] });
      navigate("/");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "创建失败，请稍后重试");
      setCreating(false);
      setProgress(null);
    }
  }

  const title = name.trim() || folderName?.trim() || "";

  return (
    <div className="relative flex min-h-full flex-col items-center justify-center px-8 pb-24">
      <h1 className="text-[26px] font-semibold text-ink">新建项目</h1>
      <p className="mt-2 text-sm text-ink-secondary">同一件事，都在这里聊</p>

      <div className="mt-14">
        <FolderIllustration />
      </div>

      {/* Name input with a send button, Enter submits */}
      <div className="mt-4 flex w-[440px] max-w-full items-center gap-2 rounded-full bg-hover py-1.5 pl-5 pr-1.5">
        <input
          value={name}
          onChange={(event) => {
            setName(event.target.value);
            setNameTouched(true);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter") void handleCreate();
          }}
          placeholder="取个名字"
          autoFocus
          className="h-9 flex-1 bg-transparent text-sm text-ink outline-none placeholder:text-ink-secondary"
        />
        <button
          type="button"
          title="创建项目"
          disabled={!title || creating}
          onClick={() => void handleCreate()}
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-ink text-white transition-opacity disabled:opacity-30"
        >
          <ArrowUpIcon size={17} />
        </button>
      </div>

      {/* Local folder import */}
      {files === null ? (
        <button
          type="button"
          onClick={() => folderInputRef.current?.click()}
          className="mt-4 text-sm text-ink-secondary underline-offset-4 transition-colors hover:text-ink hover:underline"
        >
          或直接使用本地文件夹
        </button>
      ) : (
        <div className="mt-4 flex items-center gap-2 rounded-full border border-line bg-white px-4 py-1.5 text-sm text-ink">
          <FolderIcon size={15} />
          <span>
            {folderName ?? "本地文件夹"}（{files.length} 个文件）
          </span>
          <button
            type="button"
            onClick={handleClearFolder}
            className="ml-1 text-ink-secondary transition-colors hover:text-ink"
            title="移除已选文件夹"
          >
            ✕
          </button>
        </div>
      )}
      <input
        ref={(element) => {
          folderInputRef.current = element;
          element?.setAttribute("webkitdirectory", "");
        }}
        type="file"
        multiple
        className="hidden"
        onChange={handleFolderPick}
      />

      {notice && <p className="mt-3 text-xs text-ink-secondary">{notice}</p>}
      {progress && (
        <p className="mt-3 text-sm text-ink-secondary">
          正在导入文件 {progress.done}/{progress.total}…
        </p>
      )}
      {error && <p className="mt-3 text-sm text-red-600">{error}</p>}
    </div>
  );
}
