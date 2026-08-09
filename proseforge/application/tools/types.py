"""Shared types for the tool system: pydantic arg schemas + handler IO."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field


class SearchWebArgs(BaseModel):
    query: str = Field(min_length=1, max_length=500)


class ReadPageArgs(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    mode: Literal["full", "summary"] = "full"
    max_length: int = Field(default=4000, ge=200, le=20000)


class GetPageMetadataArgs(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


class ExtractLinksArgs(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    max_links: int = Field(default=20, ge=1, le=100)


class FetchDocumentArgs(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    max_length: int = Field(default=8000, ge=500, le=50000)


class RunCodeArgs(BaseModel):
    code: str = Field(min_length=1, max_length=20000)
    timeout_seconds: int = Field(default=60, ge=1, le=120)
    # Attachment ids from the conversation, copied read-only into /work/input.
    input_files: list[str] = Field(default_factory=list, max_length=5)


@dataclass(frozen=True)
class ToolContext:
    """Everything a handler may need from the worker. The optional fields are
    only set by the orchestrator (attachment-writing tools need them)."""

    settings: object
    session_factory: object = None
    message_id: str = ""
    user_id: str = ""


@dataclass(frozen=True)
class ToolResult:
    """Handler output: markdown text for the result block + structured data
    for tool_call_log.resource_json (never the full text)."""

    text: str
    resource: dict = field(default_factory=dict)
