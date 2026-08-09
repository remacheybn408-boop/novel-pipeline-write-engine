"""Tool registry: every built-in tool the chat tool-call protocol can invoke."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import BaseModel

from proseforge.application.conversations.search_rounds import WEB_SEARCH_SKILL_KEY
from proseforge.application.conversations.tool_contract import (
    CODE_RUNNER_SKILL_KEY,
    DOC_READER_SKILL_KEY,
    WEB_READER_SKILL_KEY,
)
from proseforge.application.tools.builtin import (
    handle_extract_links,
    handle_fetch_document,
    handle_get_page_metadata,
    handle_read_page,
    handle_run_code,
    handle_search_web,
)
from proseforge.application.tools.types import (
    ExtractLinksArgs,
    FetchDocumentArgs,
    GetPageMetadataArgs,
    ReadPageArgs,
    RunCodeArgs,
    SearchWebArgs,
    ToolResult,
)


@dataclass(frozen=True)
class ToolDef:
    name: str
    schema: type[BaseModel]
    handler: Callable[..., Awaitable[ToolResult]]
    timeout_s: float
    toggle_key: str
    label: str
    contract_doc: str  # markdown section injected into the system-prompt contract


TOOL_REGISTRY: dict[str, ToolDef] = {tool.name: tool for tool in (
    ToolDef(
        name="search_web",
        schema=SearchWebArgs,
        handler=handle_search_web,
        timeout_s=30.0,
        toggle_key=WEB_SEARCH_SKILL_KEY,
        label="联网搜索",
        contract_doc=(
            "## search_web（联网搜索）\n"
            "用途：按关键词搜索，返回标题/链接/摘要及首选结果正文摘录。\n"
            "参数：query（搜索词，必填）。\n"
            "何时用：需要最新、实时、你不确定的事实时。何时不用：稳定常识与创作类请求。"
        ),
    ),
    ToolDef(
        name="read_page",
        schema=ReadPageArgs,
        handler=handle_read_page,
        timeout_s=25.0,
        toggle_key=WEB_READER_SKILL_KEY,
        label="阅读网页",
        contract_doc=(
            "## read_page（阅读网页正文）\n"
            "用途：抓取一个 URL 的正文（提取后纯文本）。\n"
            "参数：url（必填）；mode：full 或 summary（summary 正文压到 1/3，默认 full）；max_length（正文上限，默认 4000）。\n"
            "何时用：用户给了链接、或搜索结果中某个页面值得深读时。何时不用：没有具体 URL 时（先用 search_web）。"
        ),
    ),
    ToolDef(
        name="get_page_metadata",
        schema=GetPageMetadataArgs,
        handler=handle_get_page_metadata,
        timeout_s=20.0,
        toggle_key=WEB_READER_SKILL_KEY,
        label="读取网页信息",
        contract_doc=(
            "## get_page_metadata（网页元信息）\n"
            "用途：只取标题/描述/日期/站点/规范链接，不抓正文，比 read_page 快。\n"
            "参数：url（必填）。\n"
            "何时用：只需要确认页面标题、发布日期或来源时。"
        ),
    ),
    ToolDef(
        name="extract_links",
        schema=ExtractLinksArgs,
        handler=handle_extract_links,
        timeout_s=20.0,
        toggle_key=WEB_READER_SKILL_KEY,
        label="提取链接",
        contract_doc=(
            "## extract_links（提取页面链接）\n"
            "用途：列出一个页面里的链接（文字 + 绝对地址）。\n"
            "参数：url（必填）；max_links（默认 20）。\n"
            "何时用：需要找页面里的下载地址、文档入口、相关文章链接时。"
        ),
    ),
    ToolDef(
        name="fetch_document",
        schema=FetchDocumentArgs,
        handler=handle_fetch_document,
        timeout_s=60.0,
        toggle_key=DOC_READER_SKILL_KEY,
        label="读取文档",
        contract_doc=(
            "## fetch_document（读取 PDF/DOCX/CSV/XLSX 文档）\n"
            "用途：下载并解析文档 URL，返回纯文本（类型按文件魔数判定）。\n"
            "参数：url（必填）；max_length（正文上限，默认 8000）。\n"
            "何时用：URL 指向 PDF / DOCX / CSV / XLSX 文档时。何时不用：普通网页（用 read_page）。"
        ),
    ),
    ToolDef(
        name="run_code",
        schema=RunCodeArgs,
        handler=handle_run_code,
        timeout_s=150.0,  # outer cap: 120s sandbox wall clock + startup/collection slack
        toggle_key=CODE_RUNNER_SKILL_KEY,
        label="运行代码",
        contract_doc=(
            "## run_code（沙箱运行 Python）\n"
            "用途：在无网络沙箱里执行 Python 代码（预装 pandas / numpy / matplotlib / openpyxl）。\n"
            "参数：code（必填）；timeout_seconds（默认 60，上限 120）；input_files（对话附件 id 列表，"
            "会只读挂载到 /work/input，可选）。\n"
            "产出：把要交给用户的文件写到 out/ 目录（png/jpg/svg/csv/xlsx/txt/md/json，单文件 ≤10MB，最多 5 个），"
            "系统会作为附件返回下载链接。\n"
            "何时用：计算、数据处理、画图、生成文件。何时不用：只需口头回答、或需要联网的任务（沙箱无网络）。"
        ),
    ),
)}


def tools_for_toggles(toggles: dict[str, bool]) -> list[ToolDef]:
    """Registry tools whose toggle is on, in registry order."""
    return [tool for tool in TOOL_REGISTRY.values() if toggles.get(tool.toggle_key)]
