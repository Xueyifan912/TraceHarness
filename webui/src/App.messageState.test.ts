import { describe, expect, it } from "vitest";

import type { ChatMessage, RunEvent } from "./api/types";
import { appendRunMessage } from "./App";


function event(
  eventId: string,
  type: "user_message" | "assistant_message",
  content: string
): RunEvent {
  return {
    event_id: eventId,
    ts: "2026-01-01T00:00:00.000Z",
    type,
    session_id: "session_test",
    run_id: "run_test",
    payload: {
      role: type === "user_message" ? "user" : "assistant",
      content
    }
  };
}


describe("appendRunMessage", () => {
  it("replaces an optimistic user message when SSE wins the start-run race", () => {
    const optimistic: ChatMessage = {
      role: "user",
      content: "同一个问题",
      __optimistic_run_id: "pending:session_test:1"
    };
    const incoming: ChatMessage = {
      role: "user",
      content: "同一个问题"
    };

    const once = appendRunMessage(
      [optimistic],
      incoming,
      event("event_user_1", "user_message", "同一个问题")
    );
    const replayed = appendRunMessage(
      once,
      incoming,
      event("event_user_replay", "user_message", "同一个问题")
    );

    expect(once).toHaveLength(1);
    expect(replayed).toHaveLength(1);
    expect(replayed[0].content).toBe("同一个问题");
    expect(replayed[0].__optimistic_run_id).toBe("run_test");
  });

  it("does not append the same assistant event twice", () => {
    const answer: ChatMessage = {
      role: "assistant",
      content: "最终回答"
    };
    const runEvent = event(
      "event_assistant_1",
      "assistant_message",
      "最终回答"
    );

    const once = appendRunMessage([], answer, runEvent);
    const replayed = appendRunMessage(once, answer, runEvent);

    expect(replayed).toHaveLength(1);
  });
});
