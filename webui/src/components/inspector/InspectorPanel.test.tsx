import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiClient } from "../../api/client";
import type { MemoryResponse } from "../../api/types";
import { InspectorPanel } from "./InspectorPanel";

const emptyMemory: MemoryResponse = {
  path: "C:\\workspace\\.memory\\MEMORY.md",
  exists: false,
  length: 0,
  size_bytes: 0,
  updated_at: null,
  content: "",
  truncated: false,
  limit: 51200
};

describe("InspectorPanel memory append", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(apiClient, "getTeamStatus").mockResolvedValue({
      active_teammates: [],
      pending_requests: [],
      tasks: [],
      worktrees: [],
      raw_text: ""
    });
    vi.spyOn(apiClient, "getTasks").mockResolvedValue({ tasks: [] });
    vi.spyOn(apiClient, "getWorktrees").mockResolvedValue({ worktrees: [] });
    vi.spyOn(apiClient, "getMcpStatus").mockResolvedValue({
      mock_servers: [],
      configured_servers: [],
      connected_servers: [],
      errors: []
    });
    vi.spyOn(apiClient, "getTools").mockResolvedValue({ tools: [] });
    vi.spyOn(apiClient, "getMemory").mockResolvedValue(emptyMemory);
  });

  it("keeps the draft and reports a legacy ok=false response as an error", async () => {
    const appendMemory = vi.spyOn(apiClient, "appendMemory").mockResolvedValue({
      ok: false,
      message: "Memory append failed",
      length: 12,
      max_length: 20480,
      memory: emptyMemory
    });

    render(
      <InspectorPanel
        session={null}
        currentRun={null}
        isRunning={false}
        timelineItems={[]}
        events={[]}
        timelineNotice={null}
      />
    );
    await waitFor(() => expect(apiClient.getMemory).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("tab", { name: "Memory" }));
    const textarea = screen.getByLabelText("追加 memory");
    fireEvent.change(textarea, { target: { value: "durable fact" } });
    fireEvent.click(screen.getByRole("button", { name: "追加" }));

    await waitFor(() => {
      expect(screen.getByText("Memory append failed")).toBeInTheDocument();
    });
    expect(appendMemory).toHaveBeenCalledWith("durable fact", undefined);
    expect(textarea).toHaveValue("durable fact");
    expect(apiClient.getMemory).toHaveBeenCalledTimes(1);
  });
});
