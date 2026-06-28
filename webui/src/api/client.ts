import type {
  ApprovalDecision,
  ApprovalDetailResponse,
  ApprovalListResponse,
  ApprovalResolveResponse,
  ApiErrorBody,
  ChatTurnResponse,
  EventListResponse,
  HealthResponse,
  McpConnectResponse,
  McpStatusResponse,
  MemoryAppendResponse,
  MemoryResponse,
  RunDetailResponse,
  RunStartResponse,
  SessionCreateResponse,
  SessionArchiveResponse,
  SessionDetailResponse,
  SessionListResponse,
  TasksResponse,
  TeamStatusResponse,
  TimelineResponse,
  ToolsResponse,
  WorktreesResponse
} from "./types";
import { ApiError } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";

function apiPath(path: string): string {
  if (!API_BASE) {
    return path;
  }
  return `${API_BASE.replace(/\/$/, "")}${path}`;
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(apiPath(path), {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {})
    }
  });

  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    let code = "http_error";
    let details: Record<string, unknown> = {};

    try {
      const payload = (await response.json()) as ApiErrorBody;
      if (payload?.error) {
        message = payload.error.message || message;
        code = payload.error.code || code;
        details = payload.error.details ?? {};
      }
    } catch {
      // Keep the stable fallback message when the server does not return JSON.
    }

    throw new ApiError(message, code, response.status, details);
  }

  return (await response.json()) as T;
}

export const apiClient = {
  health(): Promise<HealthResponse> {
    return requestJson<HealthResponse>("/api/health");
  },

  listSessions(limit = 20): Promise<SessionListResponse> {
    return requestJson<SessionListResponse>(`/api/sessions?limit=${limit}`);
  },

  createSession(initialMessage = ""): Promise<SessionCreateResponse> {
    return requestJson<SessionCreateResponse>("/api/sessions", {
      method: "POST",
      body: JSON.stringify({
        initial_message: initialMessage || null
      })
    });
  },

  getSession(sessionId: string): Promise<SessionDetailResponse> {
    return requestJson<SessionDetailResponse>(`/api/sessions/${encodeURIComponent(sessionId)}`);
  },

  archiveSession(sessionId: string): Promise<SessionArchiveResponse> {
    return requestJson<SessionArchiveResponse>(
      `/api/sessions/${encodeURIComponent(sessionId)}/archive`,
      { method: "POST" }
    );
  },

  postMessage(sessionId: string, content: string): Promise<ChatTurnResponse> {
    return requestJson<ChatTurnResponse>(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, {
      method: "POST",
      body: JSON.stringify({
        content,
        options: {
          save: true
        }
      })
    });
  },

  startRun(sessionId: string, content: string, options: Record<string, unknown> = {}): Promise<RunStartResponse> {
    return requestJson<RunStartResponse>(`/api/sessions/${encodeURIComponent(sessionId)}/runs`, {
      method: "POST",
      body: JSON.stringify({
        content,
        options: {
          save: true,
          ...options
        }
      })
    });
  },

  getRun(sessionId: string, runId: string): Promise<RunDetailResponse> {
    return requestJson<RunDetailResponse>(
      `/api/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}`
    );
  },

  cancelRun(sessionId: string, runId: string): Promise<RunDetailResponse> {
    return requestJson<RunDetailResponse>(
      `/api/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/cancel`,
      { method: "POST" }
    );
  },

  getRunStreamUrl(sessionId: string, runId: string): string {
    return apiPath(
      `/api/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/stream`
    );
  },

  getSessionTimeline(sessionId: string): Promise<TimelineResponse> {
    return requestJson<TimelineResponse>(`/api/sessions/${encodeURIComponent(sessionId)}/timeline?limit=100`);
  },

  getSessionEvents(sessionId: string): Promise<EventListResponse> {
    return requestJson<EventListResponse>(`/api/sessions/${encodeURIComponent(sessionId)}/events?limit=80`);
  },

  getTeamStatus(): Promise<TeamStatusResponse> {
    return requestJson<TeamStatusResponse>("/api/team/status");
  },

  getTasks(): Promise<TasksResponse> {
    return requestJson<TasksResponse>("/api/tasks");
  },

  getWorktrees(): Promise<WorktreesResponse> {
    return requestJson<WorktreesResponse>("/api/worktrees");
  },

  getMcpStatus(): Promise<McpStatusResponse> {
    return requestJson<McpStatusResponse>("/api/mcp/status");
  },

  connectMcpServer(name: string): Promise<McpConnectResponse> {
    return requestJson<McpConnectResponse>("/api/mcp/connect", {
      method: "POST",
      body: JSON.stringify({ name })
    });
  },

  getTools(): Promise<ToolsResponse> {
    return requestJson<ToolsResponse>("/api/tools");
  },

  getMemory(): Promise<MemoryResponse> {
    return requestJson<MemoryResponse>("/api/memory");
  },

  appendMemory(content: string, sessionId?: string | null): Promise<MemoryAppendResponse> {
    const query = new URLSearchParams();
    if (sessionId) {
      query.set("session_id", sessionId);
    }
    const suffix = query.toString() ? `?${query.toString()}` : "";
    return requestJson<MemoryAppendResponse>(`/api/memory/append${suffix}`, {
      method: "POST",
      body: JSON.stringify({ content })
    });
  },

  listApprovals(params: {
    session_id?: string | null;
    run_id?: string | null;
    include_resolved?: boolean;
  } = {}): Promise<ApprovalListResponse> {
    const query = new URLSearchParams();
    if (params.session_id) {
      query.set("session_id", params.session_id);
    }
    if (params.run_id) {
      query.set("run_id", params.run_id);
    }
    if (typeof params.include_resolved === "boolean") {
      query.set("include_resolved", String(params.include_resolved));
    }
    const suffix = query.toString() ? `?${query.toString()}` : "";
    return requestJson<ApprovalListResponse>(`/api/approvals${suffix}`);
  },

  getApproval(approvalId: string): Promise<ApprovalDetailResponse> {
    return requestJson<ApprovalDetailResponse>(`/api/approvals/${encodeURIComponent(approvalId)}`);
  },

  resolveApproval(
    approvalId: string,
    body: { decision: ApprovalDecision; message?: string },
    params: { session_id?: string | null; run_id?: string | null } = {}
  ): Promise<ApprovalResolveResponse> {
    const query = new URLSearchParams();
    if (params.session_id) {
      query.set("session_id", params.session_id);
    }
    if (params.run_id) {
      query.set("run_id", params.run_id);
    }
    const suffix = query.toString() ? `?${query.toString()}` : "";
    return requestJson<ApprovalResolveResponse>(`/api/approvals/${encodeURIComponent(approvalId)}${suffix}`, {
      method: "POST",
      body: JSON.stringify({
        decision: body.decision,
        message: body.message || ""
      })
    });
  }
};
