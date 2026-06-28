"""Pydantic schemas for the Web API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..memory.store import APPEND_MAX_LENGTH


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody


class HealthResponse(BaseModel):
    ok: bool
    app: str
    workspace_path: str
    version: str


class SessionCreateRequest(BaseModel):
    title: str | None = None
    initial_message: str | None = None


class MessageOptions(BaseModel):
    save: bool = True


class MessageRequest(BaseModel):
    content: str
    options: MessageOptions = Field(default_factory=MessageOptions)


class SessionSummary(BaseModel):
    session_id: str
    created_at: str | None = None
    updated_at: str | None = None
    workspace_path: str | None = None
    message_count: int = 0
    last_user_prompt_preview: dict[str, Any] | None = None
    status: str = "idle"
    active_run_id: str | None = None


class SessionCreateResponse(BaseModel):
    session: SessionSummary


class SessionListResponse(BaseModel):
    sessions: list[SessionSummary]


class SessionDetailResponse(BaseModel):
    session: SessionSummary
    messages: list[dict[str, Any]]
    display_messages: list[dict[str, Any]] = Field(default_factory=list)


class SessionArchiveResponse(BaseModel):
    ok: bool
    session_id: str
    archived: bool


class RunSummary(BaseModel):
    run_id: str
    session_id: str
    status: str
    started_at: str
    ended_at: str | None = None
    error: str | None = None
    pending_approval_id: str | None = None


class ChatTurnResponse(BaseModel):
    run: RunSummary
    session: SessionSummary
    messages: list[dict[str, Any]]
    timeline: list[dict[str, Any]] = Field(default_factory=list)


class RunStartResponse(BaseModel):
    run: RunSummary
    session: SessionSummary


class RunDetailResponse(BaseModel):
    run: RunSummary


class AuditEvent(BaseModel):
    event_id: str
    ts: str | None = None
    type: str
    session_id: str | None = None
    run_id: str | None = None
    source: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class EventListResponse(BaseModel):
    events: list[AuditEvent]
    next_cursor: str | None = None
    warnings: list[str] = Field(default_factory=list)


class TimelineResponse(BaseModel):
    items: list[dict[str, Any]]
    warnings: list[str] = Field(default_factory=list)


class TeamStatusResponse(BaseModel):
    active_teammates: list[dict[str, Any]]
    pending_requests: list[dict[str, Any]]
    tasks: list[dict[str, Any]]
    worktrees: list[dict[str, Any]]
    raw_text: str


class TasksResponse(BaseModel):
    tasks: list[dict[str, Any]]


class WorktreesResponse(BaseModel):
    worktrees: list[dict[str, Any]]


class McpStatusResponse(BaseModel):
    mock_servers: list[str]
    configured_servers: list[dict[str, Any]]
    connected_servers: list[dict[str, Any]]
    errors: list[dict[str, Any]]


class McpConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=100)


class McpConnectResponse(BaseModel):
    ok: bool
    message: str
    server: dict[str, Any]


class ToolMetadata(BaseModel):
    name: str
    description: str = ""
    source: Literal["builtin", "mcp"]
    server: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)


class ToolsResponse(BaseModel):
    tools: list[ToolMetadata]


class MemoryResponse(BaseModel):
    path: str
    exists: bool
    length: int
    size_bytes: int
    updated_at: str | None = None
    content: str
    truncated: bool
    limit: int


class MemoryAppendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(..., max_length=APPEND_MAX_LENGTH)


class MemoryAppendResponse(BaseModel):
    ok: bool
    message: str
    length: int
    max_length: int
    memory: MemoryResponse


class Approval(BaseModel):
    approval_id: str
    session_id: str
    run_id: str
    tool_name: str
    tool_use_id: str | None = None
    input_preview: dict[str, Any]
    reason: str
    rule: str = ""
    created_at: str
    expires_at: str
    timeout_seconds: float
    status: str
    decision: str | None = None
    message: str | None = None
    resolved_at: str | None = None


class ApprovalListResponse(BaseModel):
    approvals: list[Approval]


class ApprovalDetailResponse(BaseModel):
    approval: Approval


class ApprovalResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["allow", "deny"]
    message: str | None = Field(default=None, max_length=2000)


class ApprovalResolveResponse(BaseModel):
    approval: Approval
