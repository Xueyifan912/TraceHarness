import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Approval } from "../api/types";


const mockState = vi.hoisted(() => {
  let resolveStale: ((value: { approvals: Approval[] }) => void) | null = null;
  return {
    listCalls: 0,
    stalePromise: Promise.resolve({ approvals: [] as Approval[] }),
    reset() {
      this.listCalls = 0;
      this.stalePromise = new Promise<{ approvals: Approval[] }>((resolve) => {
        resolveStale = resolve;
      });
    },
    resolveStale(approval: Approval) {
      resolveStale?.({ approvals: [approval] });
    }
  };
});


vi.mock("../api/client", () => ({
  apiClient: {
    listApprovals: vi.fn(async () => {
      mockState.listCalls += 1;
      if (mockState.listCalls === 1) {
        return mockState.stalePromise;
      }
      return { approvals: [] };
    }),
    resolveApproval: vi.fn(async (
      _approvalId: string,
      body: { decision: "allow" | "deny"; message?: string }
    ) => ({
      approval: {
        ...approval,
        status: body.decision === "allow" ? "allowed" : "denied",
        decision: body.decision,
        resolved_at: "2026-01-01T00:00:01.000Z"
      }
    }))
  }
}));


const approval: Approval = {
  approval_id: "approval_test",
  session_id: "session_test",
  run_id: "run_test",
  tool_name: "bash",
  tool_use_id: "toolu_test",
  input_preview: { preview: "Remove-Item file.txt" },
  reason: "test",
  rule: "test",
  created_at: "2026-01-01T00:00:00.000Z",
  expires_at: "2099-01-01T00:00:00.000Z",
  timeout_seconds: 300,
  status: "pending",
  decision: null,
  message: null,
  resolved_at: null
};


import { useApprovals } from "./useApprovals";


describe("useApprovals", () => {
  beforeEach(() => {
    mockState.reset();
  });

  it("ignores a stale poll that resolves after an approval decision", async () => {
    const { result } = renderHook(() =>
      useApprovals("session_test", 60_000)
    );
    await waitFor(() => expect(mockState.listCalls).toBe(1));

    await act(async () => {
      await result.current.resolveApproval(approval, "deny");
    });
    expect(result.current.approvals).toEqual([]);

    await act(async () => {
      mockState.resolveStale(approval);
      await mockState.stalePromise;
    });

    expect(result.current.approvals).toEqual([]);
    expect(result.current.pendingApproval).toBeNull();
  });
});
