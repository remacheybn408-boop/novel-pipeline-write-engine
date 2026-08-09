import { useRef, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createMcpServer,
  createSkill,
  deleteMcpServer,
  deleteSkill,
  getCodeRunnerTool,
  getDocReaderTool,
  getWebReaderTool,
  getWebSearchTool,
  listMcpServers,
  listSkills,
  probeMcpServer,
  setBuiltinSkillEnabled,
  setCodeRunnerToolEnabled,
  setDocReaderToolEnabled,
  setWebReaderToolEnabled,
  setWebSearchToolEnabled,
  updateMcpServer,
  updateSkill,
  uploadSkill,
  type McpProbeResult,
  type McpServer,
  type McpTransport,
  type Skill,
} from "../../lib/api/plugins";
import { getToolMetrics, type ToolMetricsDays } from "../../lib/api/tools";
import { getModelUsage, type ModelUsageDays } from "../../lib/api/usage";
import { listModels } from "../../lib/api/models";
import { ApiError } from "../../lib/api/client";
import { groupSkills } from "../../lib/skillGroups";
import { PlusIcon, TrashIcon, UploadIcon, XIcon } from "../../components/ui/icons";

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

/** Small enabled/disabled switch used by both list tabs. */
function EnabledToggle({ enabled, disabled, onToggle }: { enabled: boolean; disabled?: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={enabled}
      title={enabled ? "点击停用" : "点击启用"}
      disabled={disabled}
      onClick={onToggle}
      className={`relative h-5 w-9 shrink-0 rounded-full transition-colors disabled:opacity-50 ${
        enabled ? "bg-ink" : "bg-disabled"
      }`}
    >
      <span
        className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all ${enabled ? "left-[18px]" : "left-0.5"}`}
      />
    </button>
  );
}

// ---------------------------------------------------------------------------
// Skills tab (pure list)
// ---------------------------------------------------------------------------

function SkillsSection() {
  const queryClient = useQueryClient();
  const skillsQuery = useQuery({ queryKey: ["skills"], queryFn: listSkills });
  const skills = skillsQuery.data ?? [];

  const [notice, setNotice] = useState<Feedback | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  async function invalidate() {
    await queryClient.invalidateQueries({ queryKey: ["skills"] });
  }

  async function toggleSkill(skill: Skill) {
    if (busyId) return;
    setBusyId(skill.id);
    setNotice(null);
    try {
      // Built-ins toggle via the dedicated endpoint; user skills via PATCH by id.
      if (skill.builtin && skill.skill_key) {
        await setBuiltinSkillEnabled(skill.skill_key, !skill.enabled);
      } else {
        await updateSkill(skill.id, { enabled: !skill.enabled });
      }
      await invalidate();
    } catch (err) {
      setNotice({ ok: false, text: `更新失败：${errorText(err)}` });
    } finally {
      setBusyId(null);
    }
  }

  async function removeSkill(skill: Skill) {
    if (busyId || !window.confirm(`确定删除 Skill「${skill.name}」？`)) return;
    setBusyId(skill.id);
    setNotice(null);
    try {
      await deleteSkill(skill.id);
      setNotice({ ok: true, text: `已删除「${skill.name}」` });
      await invalidate();
    } catch (err) {
      setNotice({ ok: false, text: `删除失败：${errorText(err)}` });
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div>
      {notice && <p className={`mb-3 text-sm ${notice.ok ? "text-emerald-600" : "text-red-600"}`}>{notice.text}</p>}

      {/* Built-ins split into 小说类 / 工具类 groups; user skills land in
          我的 Skills. Empty groups are dropped by groupSkills. */}
      {skills.length === 0 ? (
        <p className="text-sm text-ink-secondary">
          {skillsQuery.isPending ? "加载中…" : "暂无 Skill，到「上传和创建」添加第一个吧"}
        </p>
      ) : (
        <div className="flex flex-col gap-6">
          {groupSkills(skills).map((group) => (
            <section key={group.key}>
              <h3 className="mb-3 flex items-baseline gap-2 text-sm font-semibold text-ink">
                {group.label}
                <span className="text-xs font-normal text-ink-secondary">{group.skills.length}</span>
              </h3>
              <ul className="grid grid-cols-[repeat(auto-fill,minmax(200px,1fr))] gap-4">
                {group.skills.map((skill) => (
                  <li
                    key={skill.id}
                    className="flex aspect-square flex-col rounded-xl border border-line bg-white p-4 transition-colors hover:border-ink-secondary/50"
                  >
                    <div className="flex items-center gap-1.5">
                      <p className="truncate text-sm font-medium text-ink" title={skill.name}>
                        {skill.name}
                      </p>
                      {skill.builtin && (
                        <span className="shrink-0 rounded bg-hover px-1.5 py-0.5 text-[10px] text-ink-secondary">内置</span>
                      )}
                    </div>
                    <p className="mt-2 line-clamp-4 text-xs leading-5 text-ink-secondary" title={skill.description}>
                      {skill.description || "无描述"}
                    </p>
                    <div className="mt-auto flex items-center justify-between pt-2">
                      <EnabledToggle enabled={skill.enabled} disabled={busyId === skill.id} onToggle={() => void toggleSkill(skill)} />
                      {/* Built-ins cannot be deleted; the toggle stays available. */}
                      {!skill.builtin && (
                        <button
                          type="button"
                          title="删除"
                          disabled={busyId === skill.id}
                          onClick={() => void removeSkill(skill)}
                          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-ink-secondary transition-colors hover:bg-hover hover:text-red-600 disabled:opacity-50"
                        >
                          <TrashIcon size={16} />
                        </button>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// MCP tab (pure list)
// ---------------------------------------------------------------------------

function McpSection() {
  const queryClient = useQueryClient();
  const serversQuery = useQuery({ queryKey: ["mcp-servers"], queryFn: listMcpServers });
  const servers = serversQuery.data ?? [];

  const [notice, setNotice] = useState<Feedback | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [probeResults, setProbeResults] = useState<Record<string, McpProbeResult>>({});
  const [probingId, setProbingId] = useState<string | null>(null);

  async function invalidate() {
    await queryClient.invalidateQueries({ queryKey: ["mcp-servers"] });
  }

  async function toggleServer(server: McpServer) {
    if (busyId) return;
    setBusyId(server.id);
    setNotice(null);
    try {
      await updateMcpServer(server.id, { enabled: !server.enabled });
      await invalidate();
    } catch (err) {
      setNotice({ ok: false, text: `更新失败：${errorText(err)}` });
    } finally {
      setBusyId(null);
    }
  }

  async function removeServer(server: McpServer) {
    if (busyId || !window.confirm(`确定删除 MCP 服务器「${server.name}」？`)) return;
    setBusyId(server.id);
    setNotice(null);
    try {
      await deleteMcpServer(server.id);
      setNotice({ ok: true, text: `已删除「${server.name}」` });
      await invalidate();
    } catch (err) {
      setNotice({ ok: false, text: `删除失败：${errorText(err)}` });
    } finally {
      setBusyId(null);
    }
  }

  async function probe(server: McpServer) {
    if (probingId) return;
    setProbingId(server.id);
    try {
      const result = await probeMcpServer(server.id);
      setProbeResults((prev) => ({ ...prev, [server.id]: result }));
    } catch (err) {
      setProbeResults((prev) => ({ ...prev, [server.id]: { ok: false, error: errorText(err) } }));
    } finally {
      setProbingId(null);
    }
  }

  return (
    <div className="max-w-[720px]">
      {notice && <p className={`mb-3 text-sm ${notice.ok ? "text-emerald-600" : "text-red-600"}`}>{notice.text}</p>}

      {servers.length === 0 ? (
        <p className="text-sm text-ink-secondary">
          {serversQuery.isPending ? "加载中…" : "暂无 MCP 服务器，到「上传和创建」添加第一个吧"}
        </p>
      ) : (
        <div className="flex flex-col gap-3">
          {servers.map((server) => {
            const probeResult = probeResults[server.id];
            return (
              <div key={server.id} className="rounded-2xl border border-line bg-white px-5 py-3.5">
                <div className="flex items-center gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <p className="truncate text-sm font-medium text-ink">{server.name}</p>
                      <span className="shrink-0 rounded-md bg-hover px-1.5 py-0.5 text-[11px] text-ink-secondary">
                        {server.transport}
                      </span>
                    </div>
                    <p className="truncate text-xs text-ink-secondary" title={server.url}>
                      {server.url}
                      {server.header_keys.length > 0 && ` · 请求头：${server.header_keys.join(", ")}`}
                    </p>
                  </div>
                  <EnabledToggle enabled={server.enabled} disabled={busyId === server.id} onToggle={() => void toggleServer(server)} />
                  <button
                    type="button"
                    disabled={probingId === server.id}
                    onClick={() => void probe(server)}
                    className={actionButtonClass}
                  >
                    {probingId === server.id ? "探测中…" : "探测"}
                  </button>
                  <button
                    type="button"
                    title="删除"
                    disabled={busyId === server.id}
                    onClick={() => void removeServer(server)}
                    className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-ink-secondary transition-colors hover:bg-hover hover:text-red-600 disabled:opacity-50"
                  >
                    <TrashIcon size={16} />
                  </button>
                </div>

                {probeResult && (
                  <div className="mt-2.5 border-t border-line pt-2.5">
                    {probeResult.ok ? (
                      probeResult.tools.length > 0 ? (
                        <div className="flex flex-wrap gap-1.5">
                          {probeResult.tools.map((tool) => (
                            <span key={tool} className="rounded-full border border-line px-2.5 py-0.5 text-xs text-ink-secondary">
                              {tool}
                            </span>
                          ))}
                        </div>
                      ) : (
                        <p className="text-xs text-ink-secondary">连接成功，未发现工具</p>
                      )
                    ) : (
                      <p className="text-xs text-red-600">探测失败：{probeResult.error}</p>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tools tab: built-in tool switches
// ---------------------------------------------------------------------------

function ToolsSection() {
  const queryClient = useQueryClient();
  const searchQuery = useQuery({ queryKey: ["web-search-tool"], queryFn: getWebSearchTool, retry: false });
  const readerQuery = useQuery({ queryKey: ["web-reader-tool"], queryFn: getWebReaderTool, retry: false });
  const docQuery = useQuery({ queryKey: ["doc-reader-tool"], queryFn: getDocReaderTool, retry: false });
  const codeQuery = useQuery({ queryKey: ["code-runner-tool"], queryFn: getCodeRunnerTool, retry: false });

  const [notice, setNotice] = useState<Feedback | null>(null);
  const [searchBusy, setSearchBusy] = useState(false);
  const [readerBusy, setReaderBusy] = useState(false);
  const [docBusy, setDocBusy] = useState(false);
  const [codeBusy, setCodeBusy] = useState(false);

  // Cards render regardless; switches stay off/disabled until state loads.
  const searchEnabled = searchQuery.data?.enabled ?? false;
  const readerEnabled = readerQuery.data?.enabled ?? false;
  const docEnabled = docQuery.data?.enabled ?? false;
  const codeEnabled = codeQuery.data?.enabled ?? false;

  async function toggleSearch() {
    if (searchBusy || !searchQuery.isSuccess) return;
    setSearchBusy(true);
    setNotice(null);
    try {
      await setWebSearchToolEnabled(!searchEnabled);
      await queryClient.invalidateQueries({ queryKey: ["web-search-tool"] });
    } catch (err) {
      setNotice({ ok: false, text: `更新失败：${errorText(err)}` });
    } finally {
      setSearchBusy(false);
    }
  }

  async function toggleReader() {
    if (readerBusy || !readerQuery.isSuccess) return;
    setReaderBusy(true);
    setNotice(null);
    try {
      await setWebReaderToolEnabled(!readerEnabled);
      await queryClient.invalidateQueries({ queryKey: ["web-reader-tool"] });
    } catch (err) {
      setNotice({ ok: false, text: `更新失败：${errorText(err)}` });
    } finally {
      setReaderBusy(false);
    }
  }

  async function toggleDoc() {
    if (docBusy || !docQuery.isSuccess) return;
    setDocBusy(true);
    setNotice(null);
    try {
      await setDocReaderToolEnabled(!docEnabled);
      await queryClient.invalidateQueries({ queryKey: ["doc-reader-tool"] });
    } catch (err) {
      setNotice({ ok: false, text: `更新失败：${errorText(err)}` });
    } finally {
      setDocBusy(false);
    }
  }

  async function toggleCode() {
    if (codeBusy || !codeQuery.isSuccess) return;
    setCodeBusy(true);
    setNotice(null);
    try {
      await setCodeRunnerToolEnabled(!codeEnabled);
      await queryClient.invalidateQueries({ queryKey: ["code-runner-tool"] });
    } catch (err) {
      setNotice({ ok: false, text: `更新失败：${errorText(err)}` });
    } finally {
      setCodeBusy(false);
    }
  }

  return (
    <div>
      {notice && <p className={`mb-3 text-sm ${notice.ok ? "text-emerald-600" : "text-red-600"}`}>{notice.text}</p>}

      {/* Same square-card grid as the Skills tab; more built-in tools join here later. */}
      <ul className="grid grid-cols-[repeat(auto-fill,minmax(200px,1fr))] gap-4">
        <li className="flex aspect-square flex-col rounded-xl border border-line bg-white p-4 transition-colors hover:border-ink-secondary/50">
          <div className="flex items-center gap-1.5">
            <p className="truncate text-sm font-medium text-ink">联网搜索</p>
            <span className="shrink-0 rounded bg-hover px-1.5 py-0.5 text-[10px] text-ink-secondary">内置</span>
          </div>
          <p className="mt-2 line-clamp-4 text-xs leading-5 text-ink-secondary">
            Chat / Work 通用 · 模型可查实时资料 · 直连搜索引擎无需 API Key
          </p>
          <div className="mt-auto pt-2">
            <EnabledToggle enabled={searchEnabled} disabled={searchBusy || !searchQuery.isSuccess} onToggle={() => void toggleSearch()} />
          </div>
        </li>
        <li className="flex aspect-square flex-col rounded-xl border border-line bg-white p-4 transition-colors hover:border-ink-secondary/50">
          <div className="flex items-center gap-1.5">
            <p className="truncate text-sm font-medium text-ink">网页阅读</p>
            <span className="shrink-0 rounded bg-hover px-1.5 py-0.5 text-[10px] text-ink-secondary">内置</span>
          </div>
          <p className="mt-2 line-clamp-4 text-xs leading-5 text-ink-secondary">
            阅读网页正文与文档 · 提取页面链接 · 配合联网搜索使用
          </p>
          <div className="mt-auto pt-2">
            <EnabledToggle enabled={readerEnabled} disabled={readerBusy || !readerQuery.isSuccess} onToggle={() => void toggleReader()} />
          </div>
        </li>
        <li className="flex aspect-square flex-col rounded-xl border border-line bg-white p-4 transition-colors hover:border-ink-secondary/50">
          <div className="flex items-center gap-1.5">
            <p className="truncate text-sm font-medium text-ink">文档读取</p>
            <span className="shrink-0 rounded bg-hover px-1.5 py-0.5 text-[10px] text-ink-secondary">内置</span>
          </div>
          <p className="mt-2 line-clamp-4 text-xs leading-5 text-ink-secondary">
            读取链接指向的 PDF / DOCX / CSV / XLSX 文档内容
          </p>
          <div className="mt-auto pt-2">
            <EnabledToggle enabled={docEnabled} disabled={docBusy || !docQuery.isSuccess} onToggle={() => void toggleDoc()} />
          </div>
        </li>
        <li className="flex aspect-square flex-col rounded-xl border border-line bg-white p-4 transition-colors hover:border-ink-secondary/50">
          <div className="flex items-center gap-1.5">
            <p className="truncate text-sm font-medium text-ink">代码执行</p>
            <span className="shrink-0 rounded bg-hover px-1.5 py-0.5 text-[10px] text-ink-secondary">内置</span>
          </div>
          <p className="mt-2 line-clamp-4 text-xs leading-5 text-ink-secondary">
            在隔离沙箱中运行 Python（pandas / numpy / matplotlib）· 生成的图表和文件可直接下载
          </p>
          <div className="mt-auto pt-2">
            <EnabledToggle enabled={codeEnabled} disabled={codeBusy || !codeQuery.isSuccess} onToggle={() => void toggleCode()} />
          </div>
        </li>
      </ul>

      <ToolMetricsSection />
      <ModelUsageSection />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tool usage metrics panel (bottom of the Tools tab; no chart library)
// ---------------------------------------------------------------------------

/** tool_name -> Chinese display name. */
const TOOL_DISPLAY_NAME: Record<string, string> = {
  search_web: "联网搜索",
  read_page: "网页阅读",
  get_page_metadata: "页面信息",
  extract_links: "链接提取",
  fetch_document: "文档读取",
  run_code: "代码执行",
};

/** error_class -> human-readable reason (mirrors the chat status lines). */
const TOOL_METRIC_ERROR_TEXT: Record<string, string> = {
  timeout: "超时",
  rate_limited: "请求过多",
  circuit_breaker: "请求过多，稍后再试",
  validation: "参数错误",
  policy_denied: "未启用",
  upstream: "服务异常",
};

const METRICS_DAY_OPTIONS: { days: 1 | 7 | 30; label: string }[] = [
  { days: 1, label: "24小时" },
  { days: 7, label: "7天" },
  { days: 30, label: "30天" },
];

/** 0.92 -> "92%", 0.925 -> "92.5%". */
function formatPercent(rate: number): string {
  return `${(rate * 100).toFixed(1).replace(/\.0$/, "")}%`;
}

/** 800 -> "800ms", 4200 -> "4.2s"; null -> "—". */
function formatMs(ms: number | null): string {
  if (ms === null) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

/** ISO timestamp -> short relative time, falling back to a date for old rows. */
function formatRelativeTime(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime();
  const minutes = Math.floor(diffMs / 60000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} 天前`;
  return new Date(iso).toLocaleDateString();
}

function ToolMetricsSection() {
  const [days, setDays] = useState<ToolMetricsDays>(7);
  const metricsQuery = useQuery({
    queryKey: ["tool-metrics", days],
    queryFn: () => getToolMetrics(days),
    retry: false,
  });
  const metrics = metricsQuery.data;

  return (
    <section className="mt-10">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-base font-semibold text-ink">使用统计</h2>
        <div className="flex gap-1">
          {METRICS_DAY_OPTIONS.map((option) => (
            <button
              key={option.days}
              type="button"
              onClick={() => setDays(option.days)}
              className={`rounded-lg px-3 py-1.5 text-xs transition-colors ${
                days === option.days ? "bg-ink font-medium text-white" : "border border-line bg-white text-ink-secondary hover:text-ink"
              }`}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      {metricsQuery.isPending ? (
        <p className="text-sm text-ink-secondary">加载中…</p>
      ) : metricsQuery.isError ? (
        <p className="text-sm text-ink-secondary">统计加载失败，请稍后重试</p>
      ) : !metrics || metrics.total_calls === 0 ? (
        // All-zero window: one empty state instead of a table full of 0.00%.
        <p className="rounded-2xl border border-line bg-white px-5 py-8 text-center text-sm text-ink-secondary">
          暂无工具调用记录
        </p>
      ) : (
        <div className="flex flex-col gap-4">
          {/* Overview row */}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {[
              { label: "总调用", value: String(metrics.total_calls) },
              { label: "成功率", value: formatPercent(metrics.success_rate) },
              { label: "超时率", value: formatPercent(metrics.timeout_rate) },
              { label: "缓存命中率", value: formatPercent(metrics.cache_hit_rate) },
            ].map((stat) => (
              <div key={stat.label} className="rounded-xl border border-line bg-white px-4 py-3">
                <p className="text-lg font-semibold text-ink">{stat.value}</p>
                <p className="mt-0.5 text-xs text-ink-secondary">{stat.label}</p>
              </div>
            ))}
          </div>

          {/* Per-tool table */}
          <div className="overflow-x-auto rounded-2xl border border-line bg-white">
            <table className="w-full min-w-[640px] text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs text-ink-secondary">
                  <th className="px-4 py-2.5 font-medium">工具</th>
                  <th className="px-4 py-2.5 font-medium">调用数</th>
                  <th className="px-4 py-2.5 font-medium">成功/失败</th>
                  <th className="px-4 py-2.5 font-medium">成功率</th>
                  <th className="px-4 py-2.5 font-medium">缓存命中</th>
                  <th className="px-4 py-2.5 font-medium">p50</th>
                  <th className="px-4 py-2.5 font-medium">p95</th>
                  <th className="px-4 py-2.5 font-medium">错误分布</th>
                </tr>
              </thead>
              <tbody>
                {metrics.tools.map((row) => (
                  <tr key={row.tool_name} className="border-b border-line last:border-0">
                    <td className="px-4 py-2.5 text-ink">{TOOL_DISPLAY_NAME[row.tool_name] ?? row.tool_name}</td>
                    <td className="px-4 py-2.5 text-ink">{row.calls}</td>
                    <td className="px-4 py-2.5 text-ink">
                      {row.ok}
                      <span className="text-ink-secondary"> / </span>
                      <span className={row.failed > 0 ? "text-red-600" : "text-ink-secondary"}>{row.failed}</span>
                    </td>
                    <td className="px-4 py-2.5 text-ink">{formatPercent(row.success_rate)}</td>
                    <td className="px-4 py-2.5 text-ink">
                      {row.cache_hits}
                      <span className="ml-1 text-xs text-ink-secondary">({formatPercent(row.cache_hit_rate)})</span>
                    </td>
                    <td className="px-4 py-2.5 text-ink">{formatMs(row.p50_ms)}</td>
                    <td className="px-4 py-2.5 text-ink">{formatMs(row.p95_ms)}</td>
                    <td className="px-4 py-2.5 text-xs text-ink-secondary">
                      {Object.entries(row.errors).length === 0
                        ? "—"
                        : Object.entries(row.errors)
                            .map(([errorClass, count]) => `${TOOL_METRIC_ERROR_TEXT[errorClass] ?? errorClass} ×${count}`)
                            .join("、")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Recent failures (backend caps at 10) */}
          {metrics.recent_failures.length > 0 && (
            <div className="rounded-2xl border border-line bg-white">
              <p className="border-b border-line px-4 py-2.5 text-xs font-medium text-ink-secondary">最近失败</p>
              <ul>
                {metrics.recent_failures.map((failure, index) => (
                  <li key={index} className="flex items-baseline gap-3 border-b border-line px-4 py-2.5 text-sm last:border-0">
                    <span className="shrink-0 text-ink">{TOOL_DISPLAY_NAME[failure.tool_name] ?? failure.tool_name}</span>
                    <span className="shrink-0 text-xs text-red-600">
                      {TOOL_METRIC_ERROR_TEXT[failure.error_class] ?? failure.error_class}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-xs text-ink-secondary" title={failure.result_summary}>
                      {failure.result_summary}
                    </span>
                    <span className="shrink-0 text-xs text-ink-secondary">{formatRelativeTime(failure.created_at)}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Model token usage panel (below the tool metrics; no chart library)
// ---------------------------------------------------------------------------

/** 0.0123 -> "$0.0123", 1.5 -> "$1.50"; null -> "—". */
function formatUsd(cost: number | null): string {
  if (cost === null) return "—";
  return `$${cost > 0 && cost < 0.01 ? cost.toFixed(4) : cost.toFixed(2)}`;
}

export function ModelUsageSection() {
  const [days, setDays] = useState<ModelUsageDays>(7);
  const usageQuery = useQuery({
    queryKey: ["model-usage", days],
    queryFn: () => getModelUsage(days),
    retry: false,
  });
  const modelsQuery = useQuery({ queryKey: ["models"], queryFn: () => listModels(), retry: false });
  const usage = usageQuery.data;
  // "provider/model_id" -> display_name; unknown models fall back to model_id.
  const displayNames = new Map(
    (modelsQuery.data ?? []).map((model) => [`${model.provider}/${model.model_id}`, model.display_name]),
  );

  return (
    <section className="mt-10">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-base font-semibold text-ink">模型用量</h2>
        <div className="flex gap-1">
          {METRICS_DAY_OPTIONS.map((option) => (
            <button
              key={option.days}
              type="button"
              onClick={() => setDays(option.days)}
              className={`rounded-lg px-3 py-1.5 text-xs transition-colors ${
                days === option.days ? "bg-ink font-medium text-white" : "border border-line bg-white text-ink-secondary hover:text-ink"
              }`}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      {usageQuery.isPending ? (
        <p className="text-sm text-ink-secondary">加载中…</p>
      ) : usageQuery.isError ? (
        <p className="text-sm text-ink-secondary">统计加载失败，请稍后重试</p>
      ) : !usage || usage.rows.length === 0 ? (
        <p className="rounded-2xl border border-line bg-white px-5 py-8 text-center text-sm text-ink-secondary">
          暂无模型调用记录
        </p>
      ) : (
        <div className="flex flex-col gap-4">
          {/* Overview row */}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {[
              { label: "总调用数", value: String(usage.totals.calls) },
              { label: "总 Tokens", value: usage.totals.total_tokens.toLocaleString() },
              { label: "总成本", value: formatUsd(usage.totals.cost_usd) },
              { label: "模型数", value: String(usage.rows.length) },
            ].map((stat) => (
              <div key={stat.label} className="rounded-xl border border-line bg-white px-4 py-3">
                <p className="text-lg font-semibold text-ink">{stat.value}</p>
                <p className="mt-0.5 text-xs text-ink-secondary">{stat.label}</p>
              </div>
            ))}
          </div>

          {/* Per-model table */}
          <div className="overflow-x-auto rounded-2xl border border-line bg-white">
            <table className="w-full min-w-[640px] text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs text-ink-secondary">
                  <th className="px-4 py-2.5 font-medium">模型</th>
                  <th className="px-4 py-2.5 font-medium">调用数</th>
                  <th className="px-4 py-2.5 font-medium">输入 Tokens</th>
                  <th className="px-4 py-2.5 font-medium">输出 Tokens</th>
                  <th className="px-4 py-2.5 font-medium">推理 Tokens</th>
                  <th className="px-4 py-2.5 font-medium">成本</th>
                  <th className="px-4 py-2.5 font-medium">平均延迟</th>
                  <th className="px-4 py-2.5 font-medium">最近使用</th>
                </tr>
              </thead>
              <tbody>
                {usage.rows.map((row) => (
                  <tr key={`${row.provider}/${row.model_id}`} className="border-b border-line last:border-0">
                    <td className="px-4 py-2.5">
                      <p className="text-ink">{displayNames.get(`${row.provider}/${row.model_id}`) ?? row.model_id}</p>
                      <p className="text-xs text-ink-secondary">
                        {row.provider}/{row.model_id}
                      </p>
                    </td>
                    <td className="px-4 py-2.5 text-ink">{row.calls}</td>
                    <td className="px-4 py-2.5 text-ink">{row.input_tokens.toLocaleString()}</td>
                    <td className="px-4 py-2.5 text-ink">{row.output_tokens.toLocaleString()}</td>
                    <td className="px-4 py-2.5 text-ink">{row.reasoning_tokens.toLocaleString()}</td>
                    <td className="px-4 py-2.5 text-ink">{formatUsd(row.cost_usd)}</td>
                    <td className="px-4 py-2.5 text-ink">{formatMs(row.avg_latency_ms)}</td>
                    <td className="px-4 py-2.5 text-xs text-ink-secondary">{formatRelativeTime(row.last_used_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Create tab: upload skill + manual skill form + new MCP server form
// ---------------------------------------------------------------------------

interface HeaderRow {
  key: string;
  value: string;
}

function CreateSection({ onCreated }: { onCreated: (kind: "skill" | "mcp") => void }) {
  const queryClient = useQueryClient();

  // --- Upload skill state ---
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadNotice, setUploadNotice] = useState<Feedback | null>(null);

  // --- Manual skill form state ---
  const [skillName, setSkillName] = useState("");
  const [skillDescription, setSkillDescription] = useState("");
  const [skillContent, setSkillContent] = useState("");
  const [skillCreating, setSkillCreating] = useState(false);
  const [skillNotice, setSkillNotice] = useState<Feedback | null>(null);

  // --- MCP form state ---
  const [mcpName, setMcpName] = useState("");
  const [mcpTransport, setMcpTransport] = useState<McpTransport>("streamable-http");
  const [mcpUrl, setMcpUrl] = useState("");
  const [headerRows, setHeaderRows] = useState<HeaderRow[]>([]);
  const [mcpEnabled, setMcpEnabled] = useState(true);
  const [mcpCreating, setMcpCreating] = useState(false);
  const [mcpNotice, setMcpNotice] = useState<Feedback | null>(null);

  async function handleUpload(files: FileList | null) {
    const file = files?.[0];
    if (!file || uploading) return;
    setUploading(true);
    setUploadNotice(null);
    try {
      const skill = await uploadSkill(file);
      setUploadNotice({ ok: true, text: `已上传 Skill「${skill.name}」` });
      await queryClient.invalidateQueries({ queryKey: ["skills"] });
      onCreated("skill");
    } catch (err) {
      setUploadNotice({ ok: false, text: `上传失败：${errorText(err)}` });
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  async function handleCreateSkill(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!skillName.trim() || !skillContent.trim() || skillCreating) return;
    setSkillCreating(true);
    setSkillNotice(null);
    try {
      await createSkill({
        name: skillName.trim(),
        content: skillContent,
        ...(skillDescription.trim() ? { description: skillDescription.trim() } : {}),
        enabled: true,
      });
      setSkillName("");
      setSkillDescription("");
      setSkillContent("");
      setSkillNotice({ ok: true, text: "Skill 已创建" });
      await queryClient.invalidateQueries({ queryKey: ["skills"] });
      onCreated("skill");
    } catch (err) {
      setSkillNotice({ ok: false, text: `创建失败：${errorText(err)}` });
    } finally {
      setSkillCreating(false);
    }
  }

  function updateHeaderRow(index: number, patch: Partial<HeaderRow>) {
    setHeaderRows((rows) => rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  }

  async function handleCreateMcp(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!mcpName.trim() || !mcpUrl.trim() || mcpCreating) return;
    const headers = Object.fromEntries(
      headerRows.filter((row) => row.key.trim()).map((row) => [row.key.trim(), row.value]),
    );
    setMcpCreating(true);
    setMcpNotice(null);
    try {
      await createMcpServer({
        name: mcpName.trim(),
        transport: mcpTransport,
        url: mcpUrl.trim(),
        ...(Object.keys(headers).length > 0 ? { headers } : {}),
        enabled: mcpEnabled,
      });
      setMcpName("");
      setMcpUrl("");
      setHeaderRows([]);
      setMcpEnabled(true);
      setMcpNotice({ ok: true, text: "MCP 服务器已添加" });
      await queryClient.invalidateQueries({ queryKey: ["mcp-servers"] });
      onCreated("mcp");
    } catch (err) {
      setMcpNotice({ ok: false, text: `添加失败：${errorText(err)}` });
    } finally {
      setMcpCreating(false);
    }
  }

  return (
    <div className="max-w-[720px]">
      {/* Upload skill */}
      <section className="mb-10">
        <h2 className="mb-1 text-base font-semibold text-ink">上传 Skill</h2>
        <p className="mb-4 text-sm text-ink-secondary">支持 .md 或 .zip 格式的 Skill 文件。</p>
        <input
          ref={fileInputRef}
          type="file"
          accept=".md,.zip"
          className="hidden"
          onChange={(e) => void handleUpload(e.target.files)}
        />
        <button
          type="button"
          disabled={uploading}
          onClick={() => fileInputRef.current?.click()}
          className="flex items-center gap-1.5 rounded-xl bg-ink px-4 py-2 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          <UploadIcon size={15} />
          {uploading ? "上传中…" : "选择文件上传"}
        </button>
        {uploadNotice && (
          <p className={`mt-3 text-sm ${uploadNotice.ok ? "text-emerald-600" : "text-red-600"}`}>{uploadNotice.text}</p>
        )}
      </section>

      {/* Manual skill form */}
      <section className="mb-10">
        <h2 className="mb-1 text-base font-semibold text-ink">手动新建 Skill</h2>
        <p className="mb-4 text-sm text-ink-secondary">直接编写 Skill 的名称、描述与内容。</p>
        <form onSubmit={handleCreateSkill} className="flex flex-col gap-3 rounded-2xl border border-line bg-white p-5">
          <div className="flex flex-col gap-1.5">
            <label htmlFor="skill-name" className="text-sm text-ink">名称</label>
            <input id="skill-name" required value={skillName} onChange={(e) => setSkillName(e.target.value)} className={inputClass} />
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="skill-description" className="text-sm text-ink">
              描述 <span className="text-ink-secondary">（可选）</span>
            </label>
            <input
              id="skill-description"
              value={skillDescription}
              onChange={(e) => setSkillDescription(e.target.value)}
              className={inputClass}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="skill-content" className="text-sm text-ink">内容</label>
            <textarea
              id="skill-content"
              required
              rows={6}
              value={skillContent}
              onChange={(e) => setSkillContent(e.target.value)}
              className="w-full resize-y rounded-xl border border-line bg-white px-3.5 py-2.5 text-sm text-ink outline-none transition-colors placeholder:text-ink-secondary focus:border-ink-secondary"
            />
          </div>
          {skillNotice && <p className={`text-sm ${skillNotice.ok ? "text-emerald-600" : "text-red-600"}`}>{skillNotice.text}</p>}
          <div>
            <button
              type="submit"
              disabled={skillCreating}
              className="h-10 rounded-xl bg-ink px-5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              {skillCreating ? "创建中…" : "创建 Skill"}
            </button>
          </div>
        </form>
      </section>

      {/* New MCP server form */}
      <section>
        <h2 className="mb-1 text-base font-semibold text-ink">新建 MCP 服务器</h2>
        <p className="mb-4 text-sm text-ink-secondary">接入远程 MCP 服务，扩展可调用的工具。</p>
        <form onSubmit={handleCreateMcp} className="flex flex-col gap-3 rounded-2xl border border-line bg-white p-5">
          <div className="flex flex-col gap-1.5">
            <label htmlFor="mcp-name" className="text-sm text-ink">名称</label>
            <input id="mcp-name" required value={mcpName} onChange={(e) => setMcpName(e.target.value)} className={inputClass} />
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="mcp-transport" className="text-sm text-ink">传输方式</label>
            <select
              id="mcp-transport"
              value={mcpTransport}
              onChange={(e) => setMcpTransport(e.target.value as McpTransport)}
              className={inputClass}
            >
              <option value="streamable-http">streamable-http</option>
              <option value="sse">sse</option>
            </select>
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="mcp-url" className="text-sm text-ink">URL</label>
            <input
              id="mcp-url"
              type="url"
              required
              placeholder="https://example.com/mcp"
              value={mcpUrl}
              onChange={(e) => setMcpUrl(e.target.value)}
              className={inputClass}
            />
          </div>

          {/* Headers key/value editor */}
          <div className="flex flex-col gap-1.5">
            <span className="text-sm text-ink">
              请求头 <span className="text-ink-secondary">（可选，如 Authorization）</span>
            </span>
            {headerRows.map((row, index) => (
              <div key={index} className="flex items-center gap-2">
                <input
                  placeholder="Key"
                  value={row.key}
                  onChange={(e) => updateHeaderRow(index, { key: e.target.value })}
                  className={inputClass}
                />
                <input
                  placeholder="Value"
                  value={row.value}
                  onChange={(e) => updateHeaderRow(index, { value: e.target.value })}
                  className={inputClass}
                />
                <button
                  type="button"
                  title="移除"
                  onClick={() => setHeaderRows((rows) => rows.filter((_, i) => i !== index))}
                  className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-ink-secondary transition-colors hover:bg-hover hover:text-ink"
                >
                  <XIcon size={15} />
                </button>
              </div>
            ))}
            <div>
              <button
                type="button"
                onClick={() => setHeaderRows((rows) => [...rows, { key: "", value: "" }])}
                className="flex items-center gap-1 rounded-lg border border-line bg-white px-3 py-1.5 text-xs text-ink transition-colors hover:bg-hover"
              >
                <PlusIcon size={13} />
                添加请求头
              </button>
            </div>
          </div>

          <label className="flex items-center gap-2 text-sm text-ink-secondary">
            <input
              type="checkbox"
              checked={mcpEnabled}
              onChange={(e) => setMcpEnabled(e.target.checked)}
              className="h-4 w-4 accent-ink"
            />
            启用该服务器
          </label>

          {mcpNotice && <p className={`text-sm ${mcpNotice.ok ? "text-emerald-600" : "text-red-600"}`}>{mcpNotice.text}</p>}
          <div>
            <button
              type="submit"
              disabled={mcpCreating}
              className="h-10 rounded-xl bg-ink px-5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              {mcpCreating ? "添加中…" : "添加服务器"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

const TABS = [
  { value: "skills", label: "Skills" },
  { value: "mcp", label: "MCP" },
  { value: "tools", label: "工具" },
  { value: "create", label: "上传和创建" },
] as const;

type PluginsTab = (typeof TABS)[number]["value"];

export function PluginsPage() {
  const navigate = useNavigate();
  const [tab, setTab] = useState<PluginsTab>("skills");

  return (
    <div className="w-full px-8 py-10">
      {/* Close: back to the chat home */}
      <button
        type="button"
        title="关闭插件页"
        aria-label="关闭插件页"
        onClick={() => navigate("/")}
        className="fixed right-6 top-6 flex h-9 w-9 items-center justify-center rounded-lg text-ink-secondary transition-colors hover:bg-hover hover:text-ink"
      >
        <XIcon size={20} />
      </button>

      <h1 className="mb-6 text-2xl font-bold text-ink">插件</h1>

      {/* Tabs */}
      <div className="mb-6 flex items-center gap-6 border-b border-line">
        {TABS.map((item) => {
          const active = tab === item.value;
          return (
            <button
              key={item.value}
              type="button"
              onClick={() => setTab(item.value)}
              className={`relative pb-2.5 text-sm transition-colors ${
                active ? "font-semibold text-ink" : "text-ink-secondary hover:text-ink"
              }`}
            >
              {item.label}
              {active && <span className="absolute bottom-0 left-1/2 h-0.5 w-6 -translate-x-1/2 rounded-full bg-ink" />}
            </button>
          );
        })}
      </div>

      {tab === "skills" && <SkillsSection />}
      {tab === "mcp" && <McpSection />}
      {tab === "tools" && <ToolsSection />}
      {tab === "create" && (
        <CreateSection
          onCreated={(kind) => {
            // Jump to the corresponding list tab after a successful creation.
            setTab(kind === "skill" ? "skills" : "mcp");
          }}
        />
      )}
    </div>
  );
}
