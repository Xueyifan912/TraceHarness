import { useEffect, useRef, useState } from "react";
import { apiClient } from "../api/client";
import type { RunEvent, SseConnectionStatus } from "../api/types";

const RUN_EVENT_TYPES = [
  "run_started",
  "run_status",
  "user_message",
  "assistant_message",
  "llm_call_started",
  "llm_call_ended",
  "llm_call_failed",
  "tool_call_started",
  "tool_call_ended",
  "background_completion",
  "permission_decision",
  "approval_requested",
  "approval_resolved",
  "final_stop",
  "run_completed",
  "run_failed",
  "run_cancelled",
  "run_cancel_requested",
  "stream_gap",
  "heartbeat"
];

interface UseRunEventsOptions {
  sessionId: string | null;
  runId: string | null;
  enabled: boolean;
  onEvent: (event: RunEvent) => void;
  onCompleted?: (event: RunEvent) => void;
  onFailed?: (event: RunEvent) => void;
  onConnectionError?: (context: { sessionId: string; runId: string }) => void;
}

export function useRunEvents({
  sessionId,
  runId,
  enabled,
  onEvent,
  onCompleted,
  onFailed,
  onConnectionError
}: UseRunEventsOptions) {
  const [status, setStatus] = useState<SseConnectionStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [lastSeenAt, setLastSeenAt] = useState<string | null>(null);
  const sourceRef = useRef<EventSource | null>(null);
  const latestRef = useRef({ sessionId, runId, enabled });
  const callbacksRef = useRef({ onEvent, onCompleted, onFailed, onConnectionError });

  useEffect(() => {
    latestRef.current = { sessionId, runId, enabled };
  }, [enabled, runId, sessionId]);

  useEffect(() => {
    callbacksRef.current = { onEvent, onCompleted, onFailed, onConnectionError };
  }, [onCompleted, onConnectionError, onEvent, onFailed]);

  useEffect(() => {
    if (!enabled || !sessionId || !runId) {
      sourceRef.current?.close();
      sourceRef.current = null;
      setStatus("idle");
      setError(null);
      return undefined;
    }

    if (typeof EventSource === "undefined") {
      setStatus("error");
      setError("当前浏览器不支持 EventSource，无法连接 SSE。");
      return undefined;
    }

    const expectedSessionId = sessionId;
    const expectedRunId = runId;
    const source = new EventSource(apiClient.getRunStreamUrl(sessionId, runId));
    sourceRef.current = source;
    let connectionErrorNotified = false;
    setStatus("connecting");
    setError(null);

    source.onopen = () => {
      if (latestRef.current.sessionId !== expectedSessionId || latestRef.current.runId !== expectedRunId) {
        return;
      }
      setStatus("connected");
      setError(null);
    };

    const handleEvent = (message: MessageEvent<string>) => {
      if (latestRef.current.sessionId !== expectedSessionId || latestRef.current.runId !== expectedRunId) {
        return;
      }

      let parsed: RunEvent;
      try {
        parsed = JSON.parse(message.data) as RunEvent;
      } catch {
        setStatus("error");
        setError("SSE 事件 JSON 解析失败。");
        return;
      }

      if (parsed.session_id !== expectedSessionId || parsed.run_id !== expectedRunId) {
        return;
      }

      setLastSeenAt(new Date().toISOString());
      if (parsed.type === "heartbeat") {
        setStatus("connected");
        return;
      }

      callbacksRef.current.onEvent(parsed);

      if (parsed.type === "stream_gap") {
        setError("SSE 事件缓冲区出现缺口，正在重新同步会话状态。");
        if (!connectionErrorNotified) {
          connectionErrorNotified = true;
          callbacksRef.current.onConnectionError?.({
            sessionId: expectedSessionId,
            runId: expectedRunId
          });
        }
        return;
      }

      if (parsed.type === "run_completed") {
        setStatus("completed");
        callbacksRef.current.onCompleted?.(parsed);
        source.close();
        if (sourceRef.current === source) {
          sourceRef.current = null;
        }
        return;
      }

      if (parsed.type === "run_failed" || parsed.type === "run_cancelled") {
        setStatus(parsed.type === "run_failed" ? "failed" : "cancelled");
        callbacksRef.current.onFailed?.(parsed);
        source.close();
        if (sourceRef.current === source) {
          sourceRef.current = null;
        }
      }
    };

    RUN_EVENT_TYPES.forEach((eventType) => {
      source.addEventListener(eventType, handleEvent as EventListener);
    });
    source.onmessage = handleEvent;

    source.onerror = () => {
      if (latestRef.current.sessionId !== expectedSessionId || latestRef.current.runId !== expectedRunId) {
        return;
      }
      if (sourceRef.current !== source) {
        return;
      }
      setStatus("error");
      setError("SSE 连接异常或已断开。");
      if (!connectionErrorNotified) {
        connectionErrorNotified = true;
        callbacksRef.current.onConnectionError?.({
          sessionId: expectedSessionId,
          runId: expectedRunId
        });
      }
    };

    return () => {
      RUN_EVENT_TYPES.forEach((eventType) => {
        source.removeEventListener(eventType, handleEvent as EventListener);
      });
      source.onmessage = null;
      source.close();
      if (sourceRef.current === source) {
        sourceRef.current = null;
      }
      setStatus((current) =>
        current === "completed" || current === "failed" || current === "cancelled" || current === "error"
          ? current
          : "disconnected"
      );
    };
  }, [enabled, runId, sessionId]);

  return {
    status,
    error,
    lastSeenAt
  };
}
