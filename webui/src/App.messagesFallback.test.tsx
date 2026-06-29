import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiClient } from "./api/client";
import App from "./App";


const mockState = vi.hoisted(() => {
  const session = {
    session_id: "session_fallback",
    created_at: "2026-01-01T00:00:00.000Z",
    updated_at: "2026-01-01T00:00:00.000Z",
    workspace_path: "C:\\workspace",
    message_count: 1,
    last_user_prompt_preview: {
      preview: "old question",
      length: 12,
      truncated: false
    },
    status: "idle",
    active_run_id: null
  };
  let rejectRefresh: ((reason?: unknown) => void) | null = null;
  return {
    session,
    getSessionCalls: 0,
    refreshRequested: false,
    refreshPromise: Promise.resolve<unknown>({}),
    reset() {
      this.getSessionCalls = 0;
      this.refreshRequested = false;
      this.refreshPromise = new Promise<unknown>((_resolve, reject) => {
        rejectRefresh = reject;
      });
    },
    rejectRefresh() {
      rejectRefresh?.(new Error("refresh failed"));
    }
  };
});


vi.mock("./api/client", async () => {
  const { ApiError } = await import("./api/types");
  return {
    apiClient: {
      health: vi.fn(async () => ({
        ok: true,
        app: "test",
        workspace_path: "C:\\workspace",
        version: "test"
      })),
      listSessions: vi.fn(async () => ({ sessions: [mockState.session] })),
      getSession: vi.fn(async () => {
        mockState.getSessionCalls += 1;
        if (mockState.getSessionCalls === 1) {
          return {
            session: mockState.session,
            messages: [{ role: "user", content: "old transcript" }],
            display_messages: [
              { role: "user", content: "old transcript" }
            ]
          };
        }
        mockState.refreshRequested = true;
        return mockState.refreshPromise;
      }),
      getSessionTimeline: vi.fn(async () => ({ items: [], warnings: [] })),
      getSessionEvents: vi.fn(async () => ({
        events: [],
        next_cursor: null,
        warnings: []
      })),
      listApprovals: vi.fn(async () => ({ approvals: [] })),
      getTeamStatus: vi.fn(async () => ({
        active_teammates: [],
        pending_requests: [],
        tasks: [],
        worktrees: [],
        raw_text: ""
      })),
      getTasks: vi.fn(async () => ({ tasks: [] })),
      getWorktrees: vi.fn(async () => ({ worktrees: [] })),
      getMcpStatus: vi.fn(async () => ({
        mock_servers: [],
        configured_servers: [],
        connected_servers: [],
        errors: []
      })),
      getTools: vi.fn(async () => ({ tools: [] })),
      getMemory: vi.fn(async () => ({
        path: "",
        exists: false,
        length: 0,
        size_bytes: 0,
        updated_at: null,
        content: "",
        truncated: false,
        limit: 0
      })),
      getRunStreamUrl: vi.fn(() => "/unused-stream"),
      startRun: vi.fn(async () => {
        throw new ApiError("not found", "not_found", 404);
      }),
      postMessage: vi.fn(async () => ({
        run: {
          run_id: "run_fallback",
          session_id: mockState.session.session_id,
          status: "completed",
          started_at: "2026-01-01T00:00:00.000Z",
          ended_at: "2026-01-01T00:00:01.000Z",
          error: null,
          pending_approval_id: null
        },
        session: mockState.session,
        messages: [
          { role: "assistant", content: "delta-only assistant" }
        ],
        timeline: []
      }))
    }
  };
});


describe("messages fallback", () => {
  beforeEach(() => {
    mockState.reset();
    vi.clearAllMocks();
  });

  it("keeps the transcript when messages fallback cannot refresh session detail", async () => {
    render(<App />);

    await screen.findByText("old transcript");
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "new fallback question" }
    });
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });

    await waitFor(() => expect(apiClient.postMessage).toHaveBeenCalledOnce());
    await waitFor(() => expect(mockState.refreshRequested).toBe(true));

    mockState.rejectRefresh();
    await screen.findByText("refresh failed");
    expect(screen.getByText("old transcript")).toBeInTheDocument();
    expect(screen.getByText("new fallback question")).toBeInTheDocument();
    expect(screen.queryByText("delta-only assistant")).not.toBeInTheDocument();
  });
});
