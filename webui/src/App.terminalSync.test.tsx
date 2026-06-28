import { act, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { RunEvent } from "./api/types";


const mockState = vi.hoisted(() => {
  const session = {
    session_id: "session_running",
    created_at: "2026-01-01T00:00:00.000Z",
    updated_at: "2026-01-01T00:00:00.000Z",
    workspace_path: "C:\\workspace",
    message_count: 1,
    last_user_prompt_preview: {
      preview: "测试问题",
      length: 4,
      truncated: false
    },
    status: "running",
    active_run_id: "run_running"
  };
  let resolveFinalDetail: ((value: unknown) => void) | null = null;
  return {
    session,
    getSessionCalls: 0,
    finalDetailPromise: Promise.resolve<unknown>({}),
    reset() {
      this.getSessionCalls = 0;
      this.finalDetailPromise = new Promise<unknown>((resolve) => {
        resolveFinalDetail = resolve;
      });
    },
    resolveFinal() {
      resolveFinalDetail?.({
        session: {
          ...session,
          message_count: 2,
          status: "idle",
          active_run_id: null
        },
        messages: [
          { role: "user", content: "测试问题" },
          { role: "assistant", content: "最终回答" }
        ],
        display_messages: [
          { role: "user", content: "测试问题" },
          { role: "assistant", content: "最终回答" }
        ]
      });
    }
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
    listSessions: vi.fn(async () => ({ sessions: [mockState.session] })),
    getSession: vi.fn(async () => {
      mockState.getSessionCalls += 1;
      if (mockState.getSessionCalls === 1) {
        return {
          session: mockState.session,
          messages: [{ role: "user", content: "测试问题" }],
          display_messages: [{ role: "user", content: "测试问题" }]
        };
      }
      return mockState.finalDetailPromise;
    }),
    getRun: vi.fn(async () => ({
      run: {
        run_id: "run_running",
        session_id: mockState.session.session_id,
        status: "running",
        started_at: "2026-01-01T00:00:00.000Z",
        ended_at: null,
        error: null,
        pending_approval_id: null
      }
    })),
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
    getRunStreamUrl: vi.fn(() => "/test-stream")
  }
}));


class MockEventSource {
  static latest: MockEventSource | null = null;

  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  private listeners = new Map<string, Set<(event: MessageEvent<string>) => void>>();

  constructor(_url: string) {
    MockEventSource.latest = this;
  }

  addEventListener(type: string, listener: EventListener) {
    const listeners = this.listeners.get(type) ?? new Set();
    listeners.add(listener as (event: MessageEvent<string>) => void);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: EventListener) {
    this.listeners.get(type)?.delete(
      listener as (event: MessageEvent<string>) => void
    );
  }

  close() {
    // The hook owns lifecycle cleanup; no browser resources exist in this test.
  }

  emit(event: RunEvent) {
    const message = { data: JSON.stringify(event) } as MessageEvent<string>;
    this.listeners.get(event.type)?.forEach((listener) => listener(message));
  }
}


import App from "./App";


describe("terminal run synchronization", () => {
  beforeEach(() => {
    mockState.reset();
    MockEventSource.latest = null;
    vi.stubGlobal("EventSource", MockEventSource);
  });

  it("keeps the session running until the final transcript is loaded", async () => {
    render(<App />);

    await screen.findByText("测试问题", { selector: ".message-text" });
    await waitFor(() => expect(MockEventSource.latest).not.toBeNull());

    const terminalEvent: RunEvent = {
      event_id: "event_terminal",
      ts: "2026-01-01T00:00:10.000Z",
      type: "run_completed",
      session_id: "session_running",
      run_id: "run_running",
      payload: {
        status: "completed",
        run: {
          run_id: "run_running",
          session_id: "session_running",
          status: "completed",
          started_at: "2026-01-01T00:00:00.000Z",
          ended_at: "2026-01-01T00:00:10.000Z",
          error: null,
          pending_approval_id: null
        }
      }
    };

    act(() => {
      MockEventSource.latest?.emit(terminalEvent);
    });

    expect(screen.getByText(/Agent 正在运行/)).toBeInTheDocument();
    expect(screen.queryByText("最终回答")).not.toBeInTheDocument();
    expect(screen.getAllByText("运行中").length).toBeGreaterThan(0);

    await act(async () => {
      mockState.resolveFinal();
      await mockState.finalDetailPromise;
    });

    await screen.findByText("最终回答");
    expect(screen.queryByText(/Agent 正在运行/)).not.toBeInTheDocument();
    expect(screen.getAllByText("空闲").length).toBeGreaterThan(0);
  });

  it.each(["completed", "denied"])(
    "does not treat tool status %s as a run terminal state",
    async (toolStatus) => {
      render(<App />);

      await screen.findByText("测试问题", { selector: ".message-text" });
      await waitFor(() => expect(MockEventSource.latest).not.toBeNull());
      const source = MockEventSource.latest;

      act(() => {
        source?.emit({
          event_id: "event_approval_resolved",
          ts: "2026-01-01T00:00:05.000Z",
          type: "approval_resolved",
          session_id: "session_running",
          run_id: "run_running",
          payload: {
            approval_id: "approval_test",
            status: toolStatus === "denied" ? "denied" : "allowed"
          }
        });
        source?.emit({
          event_id: `event_tool_${toolStatus}`,
          ts: "2026-01-01T00:00:06.000Z",
          type: "tool_call_ended",
          session_id: "session_running",
          run_id: "run_running",
          payload: {
            tool: "write_file",
            tool_use_id: "toolu_test",
            status: toolStatus
          }
        });
      });

      expect(
        screen.getByPlaceholderText(
          "输入你的任务或问题，Enter 发送，Shift+Enter 换行"
        )
      ).toBeDisabled();
      expect(screen.getByText(/Agent 正在运行/)).toBeInTheDocument();
      expect(screen.getByText("运行正在进行")).toBeInTheDocument();
      expect(screen.getAllByText("运行中").length).toBeGreaterThan(0);
    }
  );
});
