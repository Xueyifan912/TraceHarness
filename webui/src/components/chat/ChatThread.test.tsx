import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { SessionSummary } from "../../api/types";
import { ChatThread } from "./ChatThread";


const session: SessionSummary = {
  session_id: "session_test",
  created_at: null,
  updated_at: null,
  workspace_path: null,
  message_count: 2,
  last_user_prompt_preview: null,
  status: "idle",
  active_run_id: null
};


describe("ChatThread", () => {
  it("never renders internal compaction messages", () => {
    render(
      <ChatThread
        session={session}
        isLoading={false}
        isRunning={false}
        messages={[
          {
            role: "user",
            content: "[Compacted]\n\nPRIVATE INTERNAL SUMMARY"
          },
          {
            role: "user",
            content: "[Compacted. Continue with summarized context.]"
          },
          {
            role: "user",
            content: "用户真正的问题"
          },
          {
            role: "assistant",
            content: "对用户的最终回答"
          }
        ]}
      />
    );

    expect(screen.queryByText("PRIVATE INTERNAL SUMMARY")).not.toBeInTheDocument();
    expect(screen.queryByText("[Compacted. Continue with summarized context.]")).not.toBeInTheDocument();
    expect(screen.getByText("用户真正的问题")).toBeInTheDocument();
    expect(screen.getByText("对用户的最终回答")).toBeInTheDocument();
  });

  it("does not render assistant progress text from a tool-use turn", () => {
    render(
      <ChatThread
        session={session}
        isLoading={false}
        isRunning={false}
        messages={[
          {
            role: "assistant",
            content: [
              {
                type: "text",
                text: "我先检查一下工作区。"
              },
              {
                type: "tool_use",
                id: "toolu_test",
                name: "read_file",
                input: { path: "README.md" }
              }
            ]
          },
          {
            role: "assistant",
            content: [{ type: "text", text: "这是最终回答。" }]
          }
        ]}
      />
    );

    expect(screen.queryByText("我先检查一下工作区。")).not.toBeInTheDocument();
    expect(screen.getByText("这是最终回答。")).toBeInTheDocument();
  });
});
