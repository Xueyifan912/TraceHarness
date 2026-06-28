import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { SessionSummary } from "../../api/types";
import { SessionSidebar } from "./SessionSidebar";

function makeSession(status: string): SessionSummary {
  return {
    session_id: "session_cancel",
    title: null,
    created_at: "2026-06-28T00:00:00Z",
    updated_at: "2026-06-28T00:00:00Z",
    workspace_path: "C:\\workspace",
    message_count: 1,
    last_user_prompt_preview: {
      preview: "cancel me",
      length: 9,
      truncated: false
    },
    status,
    active_run_id: "run_cancel"
  };
}

describe("SessionSidebar archive action", () => {
  it("disables archiving while a run is cancelling", () => {
    const onArchive = vi.fn();
    render(
      <SessionSidebar
        sessions={[makeSession("cancelling")]}
        selectedSessionId="session_cancel"
        health={{ ok: true, app: "test", workspace_path: "C:\\workspace", version: "0.1" }}
        isLoading={false}
        isCreating={false}
        isArchiving={false}
        onCreate={vi.fn()}
        onRefresh={vi.fn()}
        onSelect={vi.fn()}
        onArchive={onArchive}
      />
    );

    const archiveButton = screen.getByRole("button", { name: "归档当前会话" });
    expect(archiveButton).toBeDisabled();
    expect(screen.getByText("当前：停止中")).toBeInTheDocument();

    fireEvent.click(archiveButton);
    expect(onArchive).not.toHaveBeenCalled();
  });
});
