"""Built-in tool handlers: thin async wrappers over infrastructure services.

search_web carries the logic migrated from search_rounds._execute_search
(behavior unchanged); the three web-reader tools format webtools outcomes as
markdown. Handlers never raise for expected failures — errors come back as
ToolResult text so the model can see them and carry on.
"""

from __future__ import annotations

import logging

from proseforge.application.tools.types import (
    ExtractLinksArgs,
    FetchDocumentArgs,
    GetPageMetadataArgs,
    ReadPageArgs,
    RunCodeArgs,
    SearchWebArgs,
    ToolContext,
    ToolResult,
)

logger = logging.getLogger(__name__)

_DEFAULT_ENGINES = ("bing", "duckduckgo", "google", "yahoo", "brave", "mojeek", "ecosia", "startpage", "baidu")


def _search_service(settings):
    from proseforge.infrastructure.search import SearchService

    return SearchService(
        engines=tuple(getattr(settings, "search_engines", None) or _DEFAULT_ENGINES),
        timeout_seconds=float(getattr(settings, "search_timeout_seconds", 10.0)),
    )


def _fetcher(settings):
    from proseforge.infrastructure.webtools import SafeFetcher

    return SafeFetcher(
        timeout_seconds=float(getattr(settings, "webtools_timeout_seconds", 20.0)),
        cache_ttl_seconds=float(getattr(settings, "webtools_cache_ttl_seconds", 600)),
    )


async def handle_search_web(args: SearchWebArgs, ctx: ToolContext) -> ToolResult:
    settings = ctx.settings
    service = _search_service(settings)
    max_results = max(1, int(getattr(settings, "search_max_results", 5)))
    max_chars = max(500, int(getattr(settings, "search_fetch_max_chars", 6000)))
    try:
        results = await service.web_search(args.query, max_results)
    except Exception as exc:
        return ToolResult(f"搜索失败：{exc}", {"search_error": str(exc)})
    lines = [f"{index}. [{item.title}]({item.url})\n   {item.snippet}" for index, item in enumerate(results, start=1)]
    markdown = "\n".join(lines)
    top_url = results[0].url
    text, error = await service.web_fetch(top_url, max_chars)
    if text:
        markdown += f"\n\n首选结果正文摘录（{top_url}）：\n{text}"
    elif error:
        markdown += f"\n\n（首选结果正文抓取失败：{error}）"
    resource = {
        "results": [{"title": item.title, "url": item.url, "snippet": item.snippet, "engine": item.engine} for item in results],
        "top_fetch_ok": bool(text),
    }
    return ToolResult(markdown, resource)


_STATUS_NOTES = {
    "paywalled": "页面疑似付费墙，只能读到摘要/引导内容。请换其他来源或直接说明无法获取全文。",
    "login_required": "页面需要登录（401/403），无法读取。",
    "js_required": "页面需要 JS 渲染，暂无法读取（空壳 SPA）。",
    "extraction_failed": "正文提取失败（页面结构无法解析或网络错误）。",
    "timeout": "抓取或解析超时。",
    "ssrf_blocked": "目标地址被安全策略拦截（内网/非法地址）。",
    "unsupported_format": "文件格式不支持（仅支持 PDF / DOCX / XLSX / CSV）。",
    "too_large": "文件超过大小上限，已放弃下载。",
    "corrupted": "文件已损坏或不是有效的文档格式。",
    "scan_rejected": "文件未通过安全扫描。",
}

_KIND_LABELS = {"pdf": "PDF", "docx": "DOCX", "xlsx": "XLSX", "csv": "CSV"}


async def handle_fetch_document(args: FetchDocumentArgs, ctx: ToolContext) -> ToolResult:
    from proseforge.infrastructure.webtools.documents import fetch_document

    outcome = await fetch_document(args.url, max_length=args.max_length, fetcher=_fetcher(ctx.settings))
    status = str(outcome.get("status", "extraction_failed"))
    if status != "ok":
        note = _STATUS_NOTES.get(status, "文档读取失败。")
        return ToolResult(f"读取文档（{args.url}）\n\n{note}", dict(outcome))
    metadata = outcome.get("metadata") or {}
    kind = str(outcome.get("kind", ""))
    header = f"文档类型：{_KIND_LABELS.get(kind, kind)}"
    facts = []
    for key, label in (("pages", "页数"), ("sheets", "表数"), ("rows", "行数"), ("encoding", "编码")):
        if metadata.get(key) is not None:
            facts.append(f"{label}：{metadata[key]}")
    if facts:
        header += f"（{'，'.join(facts)}）"
    if metadata.get("rows_truncated") or metadata.get("pages", 0) > metadata.get("pages_processed", 0):
        header += "（部分内容因上限被截断）"
    return ToolResult(f"{header}\n\n{outcome.get('text', '')}", dict(outcome))


async def handle_read_page(args: ReadPageArgs, ctx: ToolContext) -> ToolResult:
    from proseforge.infrastructure.webtools.pages import read_page

    outcome = await read_page(args.url, mode=args.mode, max_length=args.max_length, fetcher=_fetcher(ctx.settings))
    status = str(outcome.get("status", "extraction_failed"))
    if status != "ok":
        note = _STATUS_NOTES.get(status, "读取失败。")
        return ToolResult(f"读取网页（{args.url}）\n\n{note}", dict(outcome))
    header = f"标题：{outcome.get('title') or '(无)'}"
    meta_bits = [str(outcome[key]) for key in ("site", "author", "date") if outcome.get(key)]
    if meta_bits:
        header += f"\n来源：{' / '.join(meta_bits)}"
    header += f"\n置信度：{outcome.get('confidence', 'low')}"
    return ToolResult(f"{header}\n\n{outcome.get('text', '')}", dict(outcome))


async def handle_get_page_metadata(args: GetPageMetadataArgs, ctx: ToolContext) -> ToolResult:
    from proseforge.infrastructure.webtools.pages import get_page_metadata

    outcome = await get_page_metadata(args.url, fetcher=_fetcher(ctx.settings))
    status = str(outcome.get("status", "extraction_failed"))
    if status != "ok":
        note = _STATUS_NOTES.get(status, "读取失败。")
        return ToolResult(f"读取网页信息（{args.url}）\n\n{note}", dict(outcome))
    lines = [f"标题：{outcome.get('title') or '(无)'}"]
    for key, label in (("site", "站点"), ("date", "日期"), ("description", "描述"), ("canonical_url", "链接")):
        if outcome.get(key):
            lines.append(f"{label}：{outcome[key]}")
    return ToolResult("\n".join(lines), dict(outcome))


async def handle_extract_links(args: ExtractLinksArgs, ctx: ToolContext) -> ToolResult:
    from proseforge.infrastructure.webtools.pages import extract_links

    outcome = await extract_links(args.url, max_links=args.max_links, fetcher=_fetcher(ctx.settings))
    status = str(outcome.get("status", "extraction_failed"))
    if status != "ok":
        note = _STATUS_NOTES.get(status, "读取失败。")
        return ToolResult(f"提取链接（{args.url}）\n\n{note}", dict(outcome))
    links = outcome.get("links") or []
    if not links:
        return ToolResult(f"提取链接（{args.url}）\n\n页面没有可用链接。", dict(outcome))
    lines = [f"{index}. [{link['text']}]({link['url']})" for index, link in enumerate(links, start=1)]
    return ToolResult("\n".join(lines), dict(outcome))


_RUN_STATUS_LABELS = {
    "ok": "执行成功",
    "timeout": "执行超时（已终止）",
    "crashed": "执行出错",
    "oom": "内存超限（已终止）",
    "spawn_failed": "沙箱启动失败",
}


async def _load_input_files(attachment_ids: list[str], ctx: ToolContext) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Copy conversation attachments into the sandbox input set.

    Returns (files, notes): files are (sanitized_name, bytes) after size and
    magic validation; notes explain every skipped id (never raises).
    """
    from sqlalchemy import select as _select

    from proseforge.application.conversations.file_blocks import sanitize_filename
    from proseforge.infrastructure.blob.local import LocalBlobStore
    from proseforge.infrastructure.database.models.project import ProjectModel
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
    from proseforge.infrastructure.sandbox import runner

    files: list[tuple[str, bytes]] = []
    notes: list[str] = []
    if not attachment_ids:
        return files, notes
    if ctx.session_factory is None:
        return files, ["当前环境不支持输入文件"]
    blob_store = LocalBlobStore(ctx.settings.blob_root)
    async with SqlAlchemyUnitOfWork(ctx.session_factory) as uow:
        # Attachments must live in a project of the same mode as the current
        # conversation's project: a work-mode attachment must never cross
        # into a chat-mode sandbox session (and vice versa).
        session_mode: str | None = None
        if ctx.message_id:
            session_project_id = await uow.conversations.project_id_for_message(ctx.message_id)
            if session_project_id is not None:
                session_mode = await uow.session.scalar(_select(ProjectModel.mode).where(ProjectModel.id == session_project_id))
        for index, attachment_id in enumerate(attachment_ids, start=1):
            try:
                attachment = await uow.attachments.get_owned(attachment_id, ctx.user_id)
                if attachment is None:
                    notes.append(f"附件 {attachment_id} 不存在或无权访问，已跳过")
                    continue
                if session_mode is not None:
                    attachment_mode = await uow.session.scalar(_select(ProjectModel.mode).where(ProjectModel.id == attachment.project_id))
                    if attachment_mode is not None and attachment_mode != session_mode:
                        notes.append(f"附件 {attachment.filename} 属于{'写作' if attachment_mode == 'work' else '对话'}项目，与当前会话模式不一致，已拒绝")
                        continue
                data = await blob_store.get(attachment.storage_key)
            except Exception as exc:
                notes.append(f"附件 {attachment_id} 读取失败（{exc}），已跳过")
                continue
            if len(data) > runner.MAX_INPUT_FILE_BYTES:
                notes.append(f"附件 {attachment.filename} 超过大小上限，已跳过")
                continue
            name = sanitize_filename(attachment.filename, index)
            file_type = runner._FILE_TYPES.get(runner._extension(name))
            if file_type is None or not runner._magic_ok(file_type[1], data):
                notes.append(f"附件 {attachment.filename} 类型不允许进沙箱，已跳过")
                continue
            files.append((name, data))
    return files, notes


async def handle_run_code(args: RunCodeArgs, ctx: ToolContext) -> ToolResult:
    from proseforge.infrastructure.sandbox import runner

    settings = ctx.settings
    input_files, input_notes = await _load_input_files(args.input_files, ctx)
    result = await runner.run_python(
        args.code,
        timeout_seconds=args.timeout_seconds or int(getattr(settings, "code_exec_timeout_seconds", 60)),
        max_timeout_seconds=int(getattr(settings, "code_exec_max_timeout_seconds", 120)),
        venv_path=str(getattr(settings, "sandbox_venv_path", runner.DEFAULT_VENV_PATH)),
        input_files=input_files,
        max_output_chars=int(getattr(settings, "code_exec_max_output_chars", 64000)),
        max_files=int(getattr(settings, "code_exec_max_files", 5)),
        max_file_bytes=int(getattr(settings, "code_exec_max_file_bytes", 10 * 1024 * 1024)),
    )
    status = str(result.get("status", "crashed"))
    label = _RUN_STATUS_LABELS.get(status, status)
    header = f"执行状态：{label}（耗时 {int(result.get('duration_ms', 0))}ms"
    if result.get("exit_code") is not None:
        header += f"，退出码 {result['exit_code']}"
    header += "）"
    sections = [header]
    if input_notes:
        sections.append("输入文件：" + "；".join(input_notes))
    stdout = str(result.get("stdout", "")).strip()
    if stdout:
        sections.append(f"输出：\n{stdout[:4000]}")
    stderr_summary = str(result.get("stderr_summary", "")).strip()
    if stderr_summary and status != "ok":
        sections.append(f"错误摘要：{stderr_summary}")
    # Persist whitelisted artifacts as chat attachments (existing download UI).
    file_links = await _persist_output_files(result.get("files") or [], ctx)
    if file_links:
        sections.append("产出文件：\n" + "\n".join(file_links))
    resource = {
        "status": status,
        "exit_code": result.get("exit_code"),
        "duration_ms": result.get("duration_ms"),
        "files": [{"name": item["name"], "size": item["size"], "mime": item["mime"]} for item in result.get("files") or []],
        "resource": result.get("resource"),
        "stderr_full": result.get("stderr_full", "")[:2000],
    }
    return ToolResult("\n\n".join(sections), resource)


async def _persist_output_files(files: list[dict], ctx: ToolContext) -> list[str]:
    """Store sandbox artifacts via the existing blob/attachment mechanism so
    the front-end renders its usual download cards. Failures skip the file."""
    import hashlib

    if not files or ctx.session_factory is None:
        return []
    from proseforge.infrastructure.blob.local import LocalBlobStore
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

    blob_store = LocalBlobStore(ctx.settings.blob_root)
    links: list[str] = []
    try:
        async with SqlAlchemyUnitOfWork(ctx.session_factory) as uow:
            lookup = getattr(uow.conversations, "project_id_for_message", None)
            project_id = await lookup(ctx.message_id) if lookup else None
            if not project_id:
                return []
            for item in files:
                try:
                    data = item["data"]
                    storage_key = await blob_store.put(data=data, media_type=item["mime"])
                    attachment = await uow.attachments.add(project_id, item["name"], hashlib.sha256(data).hexdigest(), storage_key, message_id=ctx.message_id)
                    links.append(f"- [{item['name']}](/api/v1/files/{attachment.id}/download)（{item['size']} 字节）")
                except Exception:  # best-effort per file: one bad attachment must not drop the rest
                    logger.debug("attachment persist skipped for %r", item.get("name"), exc_info=True)
                    continue
            await uow.commit()
    except Exception:
        return links
    return links
