import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { RunEvent } from "../api/types";
import { useRunEvents } from "./useRunEvents";


class MockEventSource {
  static instances: MockEventSource[] = [];

  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  private listeners = new Map<
    string,
    Set<(event: MessageEvent<string>) => void>
  >();

  constructor(_url: string) {
    MockEventSource.instances.push(this);
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
    this.closed = true;
  }

  callbacks(type: string) {
    return [...(this.listeners.get(type) ?? [])];
  }

  emit(event: RunEvent) {
    const message = {
      data: JSON.stringify(event)
    } as MessageEvent<string>;
    this.callbacks(event.type).forEach((listener) => listener(message));
  }
}


function event(
  type: string,
  sessionId: string,
  runId: string,
  payload: Record<string, unknown> = {}
): RunEvent {
  return {
    event_id: `${type}_${sessionId}_${runId}`,
    ts: "2026-01-01T00:00:00.000Z",
    type,
    session_id: sessionId,
    run_id: runId,
    payload
  };
}


describe("useRunEvents", () => {
  beforeEach(() => {
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource);
  });

  it("keeps tool events non-terminal and ignores a queued old-stream callback", () => {
    const onEvent = vi.fn();
    const onCompleted = vi.fn();
    const onFailed = vi.fn();
    const { result, rerender } = renderHook(
      ({ sessionId, runId }) =>
        useRunEvents({
          sessionId,
          runId,
          enabled: true,
          onEvent,
          onCompleted,
          onFailed
        }),
      {
        initialProps: {
          sessionId: "session_a",
          runId: "run_a"
        }
      }
    );
    const first = MockEventSource.instances[0];

    act(() => {
      first.onopen?.();
      first.emit(
        event("tool_call_ended", "session_a", "run_a", {
          status: "completed"
        })
      );
    });

    expect(result.current.status).toBe("connected");
    expect(first.closed).toBe(false);
    expect(onCompleted).not.toHaveBeenCalled();
    const queuedOldTerminal = first.callbacks("run_completed")[0];

    rerender({ sessionId: "session_b", runId: "run_b" });
    const second = MockEventSource.instances[1];
    act(() => {
      queuedOldTerminal?.({
        data: JSON.stringify(
          event("run_completed", "session_a", "run_a")
        )
      } as MessageEvent<string>);
    });

    expect(onCompleted).not.toHaveBeenCalled();
    expect(second.closed).toBe(false);

    act(() => {
      second.emit(event("run_completed", "session_b", "run_b"));
    });
    expect(onCompleted).toHaveBeenCalledTimes(1);
    expect(result.current.status).toBe("completed");
    expect(second.closed).toBe(true);
  });
});
