"""FastAPI application factory for the Web backend."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from ..mcp.client import close_mcp_clients
from .agent_service import AgentService, WebApiError
from .approvals import ApprovalError
from .event_stream import EVENT_HUB
from .schemas import (
    ApprovalDetailResponse,
    ApprovalListResponse,
    ApprovalResolveRequest,
    ApprovalResolveResponse,
    ChatTurnResponse,
    EventListResponse,
    HealthResponse,
    McpConnectRequest,
    McpConnectResponse,
    McpStatusResponse,
    MemoryAppendRequest,
    MemoryAppendResponse,
    MemoryResponse,
    MessageRequest,
    RunDetailResponse,
    RunStartResponse,
    SessionCreateRequest,
    SessionCreateResponse,
    SessionArchiveResponse,
    SessionDetailResponse,
    SessionListResponse,
    TasksResponse,
    TeamStatusResponse,
    TimelineResponse,
    ToolsResponse,
    WorktreesResponse,
)
from .status_service import StatusService


def error_response(status_code: int, code: str, message: str,
                   details: dict[str, Any] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
            }
        },
    )


def create_app(
    *,
    service: AgentService | None = None,
    workspace: str | Path | None = None,
) -> FastAPI:
    agent_service = service or AgentService(workspace)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        agent_service.start_background_services()
        try:
            yield
        finally:
            agent_service.shutdown()
            close_mcp_clients(agent_service.workspace)

    app = FastAPI(
        title="Coding Agent Harness Web API",
        version="0.1",
        lifespan=lifespan,
    )
    event_store = agent_service.event_store
    approval_registry = agent_service.approval_registry
    status_service = StatusService(agent_service.workspace, event_store)
    app.state.agent_service = agent_service
    app.state.event_store = event_store
    app.state.approval_registry = approval_registry
    app.state.status_service = status_service

    @app.exception_handler(WebApiError)
    def handle_web_api_error(_request, exc: WebApiError):
        return error_response(
            exc.status_code,
            exc.code,
            exc.message,
            exc.details,
        )

    @app.exception_handler(ApprovalError)
    def handle_approval_error(_request, exc: ApprovalError):
        return error_response(
            exc.status_code,
            exc.code,
            exc.message,
            exc.details,
        )

    @app.exception_handler(RequestValidationError)
    def handle_validation_error(_request, exc: RequestValidationError):
        return error_response(
            422,
            "validation_error",
            "Request validation failed.",
            {"errors": exc.errors()},
        )

    @app.exception_handler(Exception)
    def handle_unexpected_error(_request, exc: Exception):
        return error_response(
            500,
            "internal_error",
            "Internal error.",
            {"error_type": type(exc).__name__},
        )

    @app.get("/api/health", response_model=HealthResponse)
    def health():
        return agent_service.health()

    @app.post("/api/sessions", response_model=SessionCreateResponse)
    def create_session(request: SessionCreateRequest):
        return agent_service.create_session(
            title=request.title,
            initial_message=request.initial_message,
        )

    @app.get("/api/sessions", response_model=SessionListResponse)
    def list_sessions(limit: int = Query(20, ge=0, le=100)):
        return agent_service.list_sessions(limit=limit)

    @app.get("/api/sessions/{session_id}", response_model=SessionDetailResponse)
    def get_session(session_id: str):
        return agent_service.get_session(session_id)

    @app.post(
        "/api/sessions/{session_id}/archive",
        response_model=SessionArchiveResponse,
    )
    def archive_session(session_id: str):
        return agent_service.archive_session(session_id)

    @app.post(
        "/api/sessions/{session_id}/messages",
        response_model=ChatTurnResponse,
    )
    def post_message(session_id: str, request: MessageRequest):
        return agent_service.post_message(
            session_id,
            request.content,
            save=request.options.save,
        )

    @app.post(
        "/api/sessions/{session_id}/runs",
        response_model=RunStartResponse,
    )
    def start_run(session_id: str, request: MessageRequest):
        return agent_service.start_run(
            session_id,
            request.content,
            save=request.options.save,
        )

    @app.get(
        "/api/sessions/{session_id}/runs/{run_id}",
        response_model=RunDetailResponse,
    )
    def get_run(session_id: str, run_id: str):
        return agent_service.get_run(session_id, run_id)

    @app.post(
        "/api/sessions/{session_id}/runs/{run_id}/cancel",
        response_model=RunDetailResponse,
    )
    def cancel_run(session_id: str, run_id: str):
        return agent_service.cancel_run(session_id, run_id)

    @app.get("/api/sessions/{session_id}/runs/{run_id}/stream")
    def stream_run(
        session_id: str,
        run_id: str,
        request: Request,
        replay_limit: int = Query(500, ge=0, le=1000),
    ):
        agent_service.get_run(session_id, run_id)
        last_event_id = request.headers.get("last-event-id")

        def replay_loader():
            result = event_store.read_events(
                session_id=session_id,
                run_id=run_id,
                limit=replay_limit,
                cursor=last_event_id,
            )
            events = list(result["events"])
            if result.get("warnings"):
                events.insert(0, {
                    "event_id": f"gap_{run_id}",
                    "ts": None,
                    "type": "stream_gap",
                    "session_id": session_id,
                    "run_id": run_id,
                    "source": "web",
                    "payload": {
                        "resync_required": True,
                        "last_event_id": last_event_id,
                        "warnings": result["warnings"],
                    },
                })
            return events

        def run_lookup():
            return agent_service.registry.get_session_run(session_id, run_id)

        return StreamingResponse(
            EVENT_HUB.iter_sse(
                session_id=session_id,
                run_id=run_id,
                replay_loader=replay_loader,
                run_lookup=run_lookup,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/events", response_model=EventListResponse)
    def list_events(
        session_id: str | None = None,
        run_id: str | None = None,
        event_type: str | None = Query(None, alias="type"),
        limit: int = Query(200, ge=0, le=1000),
        cursor: str | None = None,
    ):
        response = event_store.read_events(
            session_id=session_id,
            run_id=run_id,
            event_type=event_type,
            limit=limit,
            cursor=cursor,
        )
        response.setdefault("warnings", [])
        return response

    @app.get(
        "/api/sessions/{session_id}/events",
        response_model=EventListResponse,
    )
    def list_session_events(
        session_id: str,
        run_id: str | None = None,
        limit: int = Query(200, ge=0, le=1000),
    ):
        agent_service.get_session(session_id)
        return event_store.session_events(
            session_id,
            run_id=run_id,
            limit=limit,
        )

    @app.get(
        "/api/sessions/{session_id}/timeline",
        response_model=TimelineResponse,
    )
    def get_session_timeline(
        session_id: str,
        run_id: str | None = None,
        limit: int = Query(200, ge=0, le=1000),
    ):
        agent_service.get_session(session_id)
        return event_store.timeline(
            session_id=session_id,
            run_id=run_id,
            limit=limit,
        )

    @app.get("/api/team/status", response_model=TeamStatusResponse)
    def get_team_status():
        return status_service.team_status()

    @app.get("/api/tasks", response_model=TasksResponse)
    def get_tasks():
        return status_service.tasks()

    @app.get("/api/worktrees", response_model=WorktreesResponse)
    def get_worktrees():
        return status_service.worktrees()

    @app.get("/api/mcp/status", response_model=McpStatusResponse)
    def get_mcp_status():
        return status_service.mcp_status()

    @app.post("/api/mcp/connect", response_model=McpConnectResponse)
    def connect_mcp(request: McpConnectRequest):
        return status_service.connect_mcp(request.name)

    @app.get("/api/tools", response_model=ToolsResponse)
    def get_tools():
        return status_service.tools()

    @app.get("/api/memory", response_model=MemoryResponse)
    def get_memory():
        return status_service.memory()

    @app.post("/api/memory/append", response_model=MemoryAppendResponse)
    def append_memory(
        request: MemoryAppendRequest,
        session_id: str | None = None,
    ):
        return status_service.append_memory(request.content, session_id=session_id)

    @app.get("/api/approvals", response_model=ApprovalListResponse)
    def list_approvals(
        session_id: str | None = None,
        run_id: str | None = None,
        include_resolved: bool = True,
    ):
        return {
            "approvals": approval_registry.list(
                session_id=session_id,
                run_id=run_id,
                include_resolved=include_resolved,
            )
        }

    @app.get(
        "/api/approvals/{approval_id}",
        response_model=ApprovalDetailResponse,
    )
    def get_approval(approval_id: str):
        return {"approval": approval_registry.get(approval_id)}

    @app.post(
        "/api/approvals/{approval_id}",
        response_model=ApprovalResolveResponse,
    )
    def resolve_approval(
        approval_id: str,
        request: ApprovalResolveRequest,
        session_id: str = Query(...),
        run_id: str = Query(...),
    ):
        return {
            "approval": agent_service.resolve_approval(
                approval_id,
                request.decision,
                message=request.message or "",
                session_id=session_id,
                run_id=run_id,
            )
        }

    return app


app = create_app()
