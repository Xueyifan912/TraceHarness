import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockState = vi.hoisted(() => {
  const session = {
    session_id: "session_approval",
    created_at: "2026-01-01T00:00:00.000Z",
    updated_at: "2026-01-01T00:00:00.000Z",
    workspace_path: "C:\\workspace",
    message_count: 1,
    last_user_prompt_preview: null,
    status: "waiting_approval",
    active_run_id: "run_approval"
  };
  const approval = {
    approval_id: "appr_test",
    session_id: session.session_id,
    run_id: "run_approval",
    tool_name: "bash",
    tool_use_id: "toolu_test",
    input_preview: { preview: "Remove-Item file.txt" },
    reason: "Destructive-looking bash command",
    rule: "remove-item",
    created_at: "2026-01-01T00:00:00.000Z",
    expires_at: "2099-01-01T00:00:00.000Z",
    timeout_seconds: 300,
    status: "pending",
    decision: null,
    message: null,
    resolved_at: null
  };
  const idleSession = {
    session_id: "session_idle",
    created_at: "2026-01-01T00:00:00.000Z",
    updated_at: "2026-01-01T00:00:00.000Z",
    workspace_path: "C:\\workspace",
    message_count: 0,
    last_user_prompt_preview: {
      preview: "第二会话",
      length: 4,
      truncated: false
    },
    status: "idle",
    active_run_id: null
  };
  return {
    approvalResolved: false,
    session,
    idleSession,
    approval
  };
});

vi.mock("./api/client", () => ({
  apiClient: {
    health: vi.fn(async () => ({
      ok: true,
      app: "test",
      workspace_path: "C:\\workspace",
      version: "test"
    })),
    listSessions: vi.fn(async () => ({
      sessions: [mockState.session, mockState.idleSession]
    })),
    getSession: vi.fn(async (sessionId: string) => ({
      session:
        sessionId === mockState.idleSession.session_id
          ? mockState.idleSession
          : mockState.session,
      messages: [],
      display_messages: []
    })),
    getRun: vi.fn(async () => ({
      run: {
        run_id: "run_approval",
        session_id: mockState.session.session_id,
        status: "waiting_approval",
        started_at: "2026-01-01T00:00:00.000Z",
        ended_at: null,
        error: null,
        pending_approval_id: mockState.approval.approval_id
      }
    })),
    getSessionTimeline: vi.fn(async () => ({ items: [], warnings: [] })),
    getSessionEvents: vi.fn(async () => ({
      events: [],
      next_cursor: null,
      warnings: []
    })),
    listApprovals: vi.fn(async () => ({
      approvals: mockState.approvalResolved ? [] : [mockState.approval]
    })),
    resolveApproval: vi.fn(async (
      _approvalId: string,
      body: { decision: "allow" | "deny" }
    ) => {
      mockState.approvalResolved = true;
      return {
        approval: {
          ...mockState.approval,
          status: body.decision === "allow" ? "allowed" : "denied",
          decision: body.decision,
          resolved_at: "2026-01-01T00:00:01.000Z"
        }
      };
    }),
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
    getRunStreamUrl: vi.fn(() => "/unused-stream")
  }
}));

import { apiClient } from "./api/client";
import App from "./App";


describe("approval run state", () => {
  beforeEach(() => {
    mockState.approvalResolved = false;
    vi.clearAllMocks();
  });

  it.each([
    ["允许", "allow"],
    ["拒绝", "deny"]
  ] as const)(
    "keeps the run visibly active after approval action %s",
    async (buttonLabel, decision) => {
      render(<App />);

      await screen.findByRole("dialog", {
        name: "本地工具权限审批"
      });
      expect(
        screen.getByText("Agent 正在运行，等待后端同步返回...")
      ).toBeInTheDocument();
      expect(screen.getAllByText("待审批").length).toBeGreaterThan(0);

      fireEvent.click(
        screen.getByRole("button", { name: buttonLabel })
      );

      await waitFor(() =>
        expect(apiClient.resolveApproval).toHaveBeenCalledOnce()
      );
      expect(apiClient.resolveApproval).toHaveBeenCalledWith(
        mockState.approval.approval_id,
        expect.objectContaining({ decision }),
        expect.objectContaining({
          session_id: mockState.session.session_id,
          run_id: mockState.approval.run_id
        })
      );
      await waitFor(() =>
        expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
      );
      expect(
        screen.getByText("Agent 正在运行，等待后端同步返回...")
      ).toBeInTheDocument();
      expect(screen.getByText("运行正在进行")).toBeInTheDocument();
      expect(screen.getAllByText("运行中").length).toBeGreaterThan(0);
    }
  );

  it("does not leak a pending approval into another session", async () => {
    render(<App />);

    await screen.findByRole("dialog", {
      name: "本地工具权限审批"
    });
    fireEvent.click(
      screen.getByRole("button", { name: /第二会话/ })
    );

    await screen.findByRole("heading", { name: "第二会话" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(
      screen.getByPlaceholderText(
        "输入你的任务或问题，Enter 发送，Shift+Enter 换行"
      )
    ).not.toBeDisabled();

    fireEvent.click(
      screen.getByRole("button", { name: /新会话.*待审批/ })
    );
    await screen.findByRole("dialog", {
      name: "本地工具权限审批"
    });
  });
});
