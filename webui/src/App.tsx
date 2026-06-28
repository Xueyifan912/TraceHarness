import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiClient } from "./api/client";
import {
  ApiError,
  type Approval,
  type ApprovalDecision,
  type AuditEvent,
  type ChatMessage,
  type HealthResponse,
  type RunEvent,
  type RunSummary,
  type SessionSummary,
  type SseConnectionStatus,
  type TimelineItem
} from "./api/types";
import { AppShell } from "./components/layout/AppShell";
import { SessionSidebar } from "./components/session/SessionSidebar";
import { ChatThread } from "./components/chat/ChatThread";
import { Composer } from "./components/chat/Composer";
import { InspectorPanel } from "./components/inspector/InspectorPanel";
import { ApprovalModal } from "./components/approval/ApprovalModal";
import { useApprovals } from "./hooks/useApprovals";
import { useRunEvents } from "./hooks/useRunEvents";

function apiErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.message}（${error.code}）`;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "未知错误";
}

function makePendingRun(sessionId: string): RunSummary {
  return {
    run_id: "pending",
    session_id: sessionId,
    status: "running",
    started_at: new Date().toISOString(),
    ended_at: null,
    error: null,
    pending_approval_id: null
  };
}

const ACTIVE_RUN_STATUSES = new Set(["queued", "running", "waiting_approval", "cancelling"]);
const TERMINAL_RUN_STATUSES = new Set(["completed", "failed", "cancelled"]);

function isActiveRunStatus(status: string | null | undefined): boolean {
  return ACTIVE_RUN_STATUSES.has(String(status ?? ""));
}

function isTerminalRunStatus(status: string | null | undefined): boolean {
  return TERMINAL_RUN_STATUSES.has(String(status ?? ""));
}

function reconcileSessionsWithRun(
  sessions: SessionSummary[],
  knownRun: RunSummary | null
): SessionSummary[] {
  if (!knownRun || !isActiveRunStatus(knownRun.status)) {
    return sessions;
  }
  return sessions.map((session) =>
    knownRun.session_id === session.session_id
      ? {
          ...session,
          status: knownRun.status,
          active_run_id:
            knownRun.run_id === "pending"
              ? session.active_run_id
              : knownRun.run_id
        }
      : session
  );
}

function runEventKey(event: RunEvent): string {
  if (event.event_id) {
    return event.event_id;
  }
  return `${event.type}:${event.ts ?? ""}:${event.session_id ?? ""}:${event.run_id ?? ""}`;
}

function previewFromPayload(value: unknown): string {
  if (value && typeof value === "object" && "preview" in value) {
    return String((value as { preview?: unknown }).preview ?? "");
  }
  if (typeof value === "string") {
    return value;
  }
  if (value === undefined || value === null) {
    return "";
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function runFromEvent(event: RunEvent): RunSummary | null {
  const run = event.payload.run;
  if (!run || typeof run !== "object") {
    return null;
  }
  const candidate = run as Partial<RunSummary>;
  if (!candidate.run_id || !candidate.session_id || !candidate.status) {
    return null;
  }
  return {
    run_id: String(candidate.run_id),
    session_id: String(candidate.session_id),
    status: String(candidate.status),
    started_at: String(candidate.started_at ?? event.ts ?? new Date().toISOString()),
    ended_at: candidate.ended_at ? String(candidate.ended_at) : null,
    error: candidate.error ? String(candidate.error) : null,
    pending_approval_id: candidate.pending_approval_id ? String(candidate.pending_approval_id) : null
  };
}

function messageFromRunEvent(event: RunEvent): ChatMessage | null {
  if (event.type !== "user_message" && event.type !== "assistant_message") {
    return null;
  }
  const role = event.type === "user_message" ? "user" : "assistant";
  const payloadRole = typeof event.payload.role === "string" ? event.payload.role : role;
  if (event.type === "assistant_message") {
    const content = event.payload.content;
    if (Array.isArray(content) || (content !== null && typeof content === "object")) {
      return {
        role: payloadRole,
        content: content as ChatMessage["content"]
      };
    }
    if (typeof content === "string") {
      return {
        role: payloadRole,
        content
      };
    }
    if (typeof event.payload.text_preview === "string") {
      return {
        role: payloadRole,
        content: event.payload.text_preview
      };
    }
  }
  return {
    role: payloadRole,
    content: previewFromPayload(event.payload.content)
  };
}

function messageText(message: ChatMessage): string {
  if (typeof message.content === "string") {
    return message.content;
  }
  try {
    return JSON.stringify(message.content);
  } catch {
    return String(message.content ?? "");
  }
}

const OPTIMISTIC_RUN_ID_KEY = "__optimistic_run_id";
const RUN_MESSAGE_EVENT_ID_KEY = "__run_message_event_id";
const RUN_MESSAGE_RUN_ID_KEY = "__run_message_run_id";

function makeOptimisticUserMessage(content: string, runId: string): ChatMessage {
  return {
    role: "user",
    content,
    [OPTIMISTIC_RUN_ID_KEY]: runId
  };
}

export function appendRunMessage(messages: ChatMessage[], next: ChatMessage, event: RunEvent): ChatMessage[] {
  const eventKey = runEventKey(event);
  const nextWithIdentity: ChatMessage = {
    ...next,
    [RUN_MESSAGE_EVENT_ID_KEY]: eventKey,
    [RUN_MESSAGE_RUN_ID_KEY]: event.run_id
  };
  if (messages.some((message) => message[RUN_MESSAGE_EVENT_ID_KEY] === eventKey)) {
    return messages;
  }

  if (event.type === "user_message") {
    const nextText = messageText(next);
    const pendingIndex = messages.findIndex(
      (message) =>
        message.role === "user" &&
        (
          message[OPTIMISTIC_RUN_ID_KEY] === event.run_id ||
          String(message[OPTIMISTIC_RUN_ID_KEY] ?? "").startsWith("pending:")
        ) &&
        messageText(message) === nextText
    );
    if (pendingIndex >= 0) {
      return messages.map((message, index) =>
        index === pendingIndex
          ? {
              ...nextWithIdentity,
              role: next.role || "user",
              [OPTIMISTIC_RUN_ID_KEY]: event.run_id
            }
          : message
      );
    }
  }

  const lastMessage = messages[messages.length - 1];
  if (
    lastMessage &&
    lastMessage.role === next.role &&
    messageText(lastMessage) === messageText(next)
  ) {
    return messages;
  }

  return [
    ...messages,
    event.type === "user_message"
      ? {
          ...nextWithIdentity,
          [OPTIMISTIC_RUN_ID_KEY]: event.run_id
        }
      : nextWithIdentity
  ];
}

function auditEventFromRunEvent(event: RunEvent): AuditEvent {
  return {
    event_id: event.event_id,
    ts: event.ts,
    type: event.type,
    session_id: event.session_id,
    run_id: event.run_id,
    source: event.source ?? null,
    payload: event.payload
  };
}

function timelineItemFromRunEvent(event: RunEvent): TimelineItem | null {
  const payload = event.payload;
  if (event.type === "tool_call_started" || event.type === "tool_call_ended") {
    const toolUseId = String(payload.tool_use_id ?? event.event_id);
    return {
      id: `tool_${toolUseId}`,
      type: "tool_call",
      title: String(payload.tool ?? "tool"),
      status: event.type === "tool_call_started" ? "running" : String(payload.status ?? "completed"),
      started_at: event.type === "tool_call_started" ? event.ts ?? undefined : undefined,
      ended_at: event.type === "tool_call_ended" ? event.ts ?? undefined : undefined,
      tool_use_id: typeof payload.tool_use_id === "string" ? payload.tool_use_id : undefined,
      input_preview: payload.input,
      output_preview: typeof payload.output_preview === "string" ? payload.output_preview : undefined
    };
  }

  if (event.type === "background_completion") {
    const toolUseId = String(
      payload.tool_use_id ?? payload.background_id ?? event.event_id
    );
    return {
      id: `tool_${toolUseId}`,
      type: "tool_call",
      title: String(payload.tool ?? "background tool"),
      status: String(payload.status ?? "completed"),
      ended_at: event.ts ?? undefined,
      tool_use_id:
        typeof payload.tool_use_id === "string"
          ? payload.tool_use_id
          : undefined,
      background_id: payload.background_id
    };
  }

  if (event.type === "llm_call_started" || event.type === "llm_call_ended" || event.type === "llm_call_failed") {
    const callId = String(payload.llm_call_id ?? event.event_id);
    return {
      id: `llm_${callId}`,
      type: "llm_call",
      title: "LLM call",
      status: event.type === "llm_call_started" ? "running" : event.type === "llm_call_failed" ? "failed" : "completed",
      started_at: event.type === "llm_call_started" ? event.ts ?? undefined : undefined,
      ended_at: event.type !== "llm_call_started" ? event.ts ?? undefined : undefined,
      model: payload.model,
      provider: payload.provider,
      error_type: payload.error_type,
      message: payload.message
    };
  }

  if (event.type === "permission_decision" || event.type === "approval_requested" || event.type === "approval_resolved") {
    return {
      id: `${event.type}_${event.event_id}`,
      type: "permission",
      title: event.type === "approval_requested" ? "Approval requested" : event.type === "approval_resolved" ? "Approval resolved" : "Permission decision",
      status: String(payload.status ?? payload.action ?? event.type),
      timestamp: event.ts ?? undefined,
      tool_use_id: typeof payload.tool_use_id === "string" ? payload.tool_use_id : undefined,
      input_preview: payload.input_preview ?? payload.subject,
      output_preview: typeof payload.message === "string" ? payload.message : undefined
    };
  }

  if (event.type === "final_stop") {
    return {
      id: `final_${event.event_id}`,
      type: "final_stop",
      title: "Final stop",
      status: "completed",
      timestamp: event.ts ?? undefined,
      reason: payload.reason
    };
  }

  if (event.type === "run_failed" || event.type === "run_cancelled") {
    return {
      id: `${event.type}_${event.event_id}`,
      type: "error",
      title: event.type === "run_failed" ? "Run failed" : "Run cancelled",
      status: event.type === "run_failed" ? "failed" : "cancelled",
      timestamp: event.ts ?? undefined,
      output_preview: previewFromPayload(payload.error ?? payload.run)
    };
  }

  return null;
}

function mergeTimelineItem(items: TimelineItem[], next: TimelineItem): TimelineItem[] {
  const index = items.findIndex((item) => item.id === next.id);
  if (index < 0) {
    return [...items, next];
  }
  const merged = [...items];
  merged[index] = {
    ...merged[index],
    ...Object.fromEntries(Object.entries(next).filter(([, value]) => value !== undefined))
  };
  return merged;
}

export default function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [timelineItems, setTimelineItems] = useState<TimelineItem[]>([]);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [currentRun, setCurrentRun] = useState<RunSummary | null>(null);
  const [timelineNotice, setTimelineNotice] = useState<string | null>("Timeline / Events API 待 WUI-02 审查后再稳定接入。");
  const [isLoadingSidecar, setIsLoadingSidecar] = useState(false);
  const [isLoadingSessions, setIsLoadingSessions] = useState(false);
  const [isLoadingSession, setIsLoadingSession] = useState(false);
  const [isCreating, setIsCreating] = useState(false);
  const [isArchiving, setIsArchiving] = useState(false);
  const [sendingSessionId, setSendingSessionId] = useState<string | null>(null);
  const [terminalSync, setTerminalSync] = useState<{
    sessionId: string;
    runId: string;
  } | null>(null);
  const [isCancelling, setIsCancelling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [dismissedApprovalId, setDismissedApprovalId] = useState<string | null>(null);
  const selectedSessionIdRef = useRef<string | null>(null);
  const currentRunRef = useRef<RunSummary | null>(null);
  const processedRunEventKeysRef = useRef<Set<string>>(new Set());
  const sessionsRequestRef = useRef(0);
  const sessionDetailRequestRef = useRef(0);
  const sidecarRequestRef = useRef(0);

  const selectSessionId = useCallback((sessionId: string | null) => {
    selectedSessionIdRef.current = sessionId;
    setSelectedSessionId(sessionId);
  }, []);

  const selectedSession = useMemo(
    () =>
      sessions.find((session) => session.session_id === selectedSessionId) ??
      null,
    [selectedSessionId, sessions]
  );

  const {
    approvals,
    pendingApproval,
    lastResolvedApproval,
    isLoading: approvalsLoading,
    isResolving: approvalResolving,
    error: approvalsError,
    refresh: refreshApprovals,
    resolveApproval
  } = useApprovals(selectedSessionId);

  const currentRunBelongsToSelected = Boolean(
    currentRun?.session_id === selectedSessionId
  );
  const approvalsForSelected = useMemo(
    () =>
      approvals.filter(
        (approval) => approval.session_id === selectedSessionId
      ),
    [approvals, selectedSessionId]
  );
  const pendingApprovalForSelected =
    pendingApproval &&
    pendingApproval.session_id === selectedSessionId &&
    (
      !currentRunBelongsToSelected ||
      currentRun?.run_id === "pending" ||
      currentRun?.run_id === pendingApproval.run_id
    )
      ? pendingApproval
      : null;
  const lastResolvedApprovalForSelected =
    lastResolvedApproval?.session_id === selectedSessionId
      ? lastResolvedApproval
      : null;
  const isWaitingApproval =
    selectedSession?.status === "waiting_approval" ||
    (currentRunBelongsToSelected && currentRun?.status === "waiting_approval") ||
    Boolean(pendingApprovalForSelected);
  const isSendingCurrentSession = Boolean(
    sendingSessionId && selectedSessionId && sendingSessionId === selectedSessionId
  );
  const isTerminalSyncCurrent = Boolean(
    terminalSync && terminalSync.sessionId === selectedSessionId
  );
  const isRunActive =
    selectedSession?.status === "running" ||
    selectedSession?.status === "queued" ||
    (currentRunBelongsToSelected && isActiveRunStatus(currentRun?.status));
  const isRunning =
    isSendingCurrentSession ||
    isRunActive ||
    isWaitingApproval ||
    isTerminalSyncCurrent;
  const hasActiveSession = sessions.some((session) =>
    isActiveRunStatus(session.status)
  );

  const loadSidecarData = useCallback(async (sessionId: string) => {
    const requestSessionId = sessionId;
    const requestId = ++sidecarRequestRef.current;
    setIsLoadingSidecar(true);
    try {
      const [timelineResult, eventsResult] = await Promise.allSettled([
        apiClient.getSessionTimeline(requestSessionId),
        apiClient.getSessionEvents(requestSessionId)
      ]);

      if (
        selectedSessionIdRef.current !== requestSessionId ||
        sidecarRequestRef.current !== requestId
      ) {
        return;
      }

      let notice: string | null = null;
      if (timelineResult.status === "fulfilled") {
        setTimelineItems(timelineResult.value.items);
        if (timelineResult.value.warnings.length > 0) {
          notice = timelineResult.value.warnings.join("；");
        }
      } else {
        setTimelineItems([]);
        notice = "Timeline API 暂不可用，基础会话功能不受影响。";
      }

      if (eventsResult.status === "fulfilled") {
        setEvents(eventsResult.value.events);
        if (eventsResult.value.warnings.length > 0) {
          notice = [notice, ...eventsResult.value.warnings].filter(Boolean).join("；");
        }
      } else {
        setEvents([]);
        notice = notice ?? "Events API 暂不可用，基础会话功能不受影响。";
      }

      setTimelineNotice(notice);
    } finally {
      if (
        selectedSessionIdRef.current === requestSessionId &&
        sidecarRequestRef.current === requestId
      ) {
        setIsLoadingSidecar(false);
      }
    }
  }, []);

  const loadSession = useCallback(
    async (sessionId: string) => {
      const requestSessionId = sessionId;
      const requestId = ++sessionDetailRequestRef.current;
      const sessionChanged =
        selectedSessionIdRef.current !== requestSessionId;
      selectSessionId(requestSessionId);
      if (sessionChanged) {
        sidecarRequestRef.current += 1;
        setMessages([]);
        setTimelineItems([]);
        setEvents([]);
        setTimelineNotice(null);
        setIsLoadingSidecar(false);
        currentRunRef.current = null;
        setCurrentRun(null);
      }
      setIsLoadingSession(true);
      setError(null);
      try {
        const detail = await apiClient.getSession(requestSessionId);
        if (
          selectedSessionIdRef.current !== requestSessionId ||
          sessionDetailRequestRef.current !== requestId
        ) {
          return;
        }
        let nextSession = detail.session;
        let nextMessages = detail.display_messages ?? detail.messages;
        let nextRun: RunSummary | null = null;

        if (detail.session.active_run_id) {
          try {
            const runDetail = await apiClient.getRun(requestSessionId, detail.session.active_run_id);
            if (
              selectedSessionIdRef.current !== requestSessionId ||
              sessionDetailRequestRef.current !== requestId
            ) {
              return;
            }
            nextRun = runDetail.run;
            if (isTerminalRunStatus(runDetail.run.status)) {
              const refreshed = await apiClient.getSession(requestSessionId);
              if (
                selectedSessionIdRef.current !== requestSessionId ||
                sessionDetailRequestRef.current !== requestId
              ) {
                return;
              }
              const runStillMarkedActive = refreshed.session.active_run_id === runDetail.run.run_id;
              const staleActiveStatus =
                runStillMarkedActive ||
                (!refreshed.session.active_run_id && isActiveRunStatus(refreshed.session.status));
              nextSession = {
                ...refreshed.session,
                status: staleActiveStatus ? runDetail.run.status : refreshed.session.status,
                active_run_id: runStillMarkedActive ? null : refreshed.session.active_run_id
              };
              nextMessages = refreshed.display_messages ?? refreshed.messages;
            } else {
              nextSession = {
                ...detail.session,
                status: runDetail.run.status,
                active_run_id: runDetail.run.run_id
              };
            }
          } catch {
            if (
              selectedSessionIdRef.current !== requestSessionId ||
              sessionDetailRequestRef.current !== requestId
            ) {
              return;
            }
            nextRun = null;
          }
        }

        setSessions((current) => {
          const exists = current.some(
            (session) => session.session_id === nextSession.session_id
          );
          if (!exists) {
            return [nextSession, ...current];
          }
          return current.map((session) =>
            session.session_id === nextSession.session_id
              ? nextSession
              : session
          );
        });
        setMessages(nextMessages);
        currentRunRef.current = nextRun;
        setCurrentRun(nextRun);
        await loadSidecarData(requestSessionId);
      } catch (err) {
        if (
          selectedSessionIdRef.current === requestSessionId &&
          sessionDetailRequestRef.current === requestId
        ) {
          setError(apiErrorMessage(err));
        }
      } finally {
        if (
          selectedSessionIdRef.current === requestSessionId &&
          sessionDetailRequestRef.current === requestId
        ) {
          setIsLoadingSession(false);
        }
      }
    },
    [loadSidecarData, selectSessionId]
  );

  const loadSessions = useCallback(
    async (preferredSessionId?: string) => {
      const requestId = ++sessionsRequestRef.current;
      setIsLoadingSessions(true);
      setError(null);
      try {
        const [healthResult, sessionResult] = await Promise.all([
          apiClient.health(),
          apiClient.listSessions()
        ]);
        if (sessionsRequestRef.current !== requestId) {
          return;
        }
        setHealth(healthResult);
        if (sessionResult.warnings?.length) {
          setError(sessionResult.warnings.join("；"));
        }
        const reconciledSessions = reconcileSessionsWithRun(
          sessionResult.sessions,
          currentRunRef.current
        );
        setSessions(reconciledSessions);

        const currentSelection = selectedSessionIdRef.current;
        const nextId =
          preferredSessionId ??
          currentSelection ??
          reconciledSessions[0]?.session_id ??
          null;
        if (nextId && nextId !== currentSelection) {
          await loadSession(nextId);
        } else if (!nextId) {
          selectSessionId(null);
          setMessages([]);
          setTimelineItems([]);
          setEvents([]);
          setCurrentRun(null);
          currentRunRef.current = null;
          setIsLoadingSidecar(false);
        }
      } catch (err) {
        if (sessionsRequestRef.current === requestId) {
          setHealth(null);
          setError(`无法连接 Web API：${apiErrorMessage(err)}`);
        }
      } finally {
        if (sessionsRequestRef.current === requestId) {
          setIsLoadingSessions(false);
        }
      }
    },
    [loadSession, selectSessionId]
  );

  const refreshCurrentSessionData = useCallback(
    async (sessionId: string) => {
      if (selectedSessionIdRef.current !== sessionId) {
        await loadSessions();
        return;
      }
      await Promise.allSettled([
        loadSessions(),
        loadSession(sessionId),
        refreshApprovals()
      ]);
    },
    [loadSession, loadSessions, refreshApprovals]
  );

  const syncTerminalSession = useCallback(
    async (sessionId: string, runId: string) => {
      const requestId = ++sessionDetailRequestRef.current;
      try {
        const detail = await apiClient.getSession(sessionId);
        if (
          selectedSessionIdRef.current !== sessionId ||
          sessionDetailRequestRef.current !== requestId
        ) {
          return;
        }
        const terminalRun = currentRunRef.current;
        if (terminalRun && terminalRun.run_id !== runId) {
          return;
        }
        const summary = {
          ...detail.session,
          status: detail.session.status,
          active_run_id: detail.session.active_run_id
        };
        // Commit the final transcript and terminal summary together. Until
        // this point the UI deliberately remains in its running/syncing state.
        setMessages(detail.display_messages ?? detail.messages);
        setSessions((current) =>
          current.map((session) =>
            session.session_id === sessionId ? summary : session
          )
        );
        await Promise.allSettled([
          loadSidecarData(sessionId),
          refreshApprovals()
        ]);
      } catch (err) {
        if (selectedSessionIdRef.current === sessionId) {
          setError(apiErrorMessage(err));
        }
      } finally {
        setTerminalSync((current) =>
          current?.sessionId === sessionId && current.runId === runId
            ? null
            : current
        );
      }
    },
    [loadSidecarData, refreshApprovals]
  );

  useEffect(() => {
    currentRunRef.current = currentRun;
  }, [currentRun]);

  const streamRunId =
    currentRun &&
    currentRunBelongsToSelected &&
    currentRun.run_id !== "pending" &&
    currentRun.session_id === selectedSessionId &&
    isActiveRunStatus(currentRun.status)
      ? currentRun.run_id
      : null;

  useEffect(() => {
    processedRunEventKeysRef.current.clear();
  }, [selectedSessionId, streamRunId]);

  const handleRunEvent = useCallback(
    (event: RunEvent) => {
      if (!event.session_id || !event.run_id) {
        return;
      }
      if (selectedSessionIdRef.current !== event.session_id) {
        return;
      }

      const current = currentRunRef.current;
      if (current && current.run_id !== "pending" && current.run_id !== event.run_id) {
        return;
      }

      const key = runEventKey(event);
      if (processedRunEventKeysRef.current.has(key)) {
        return;
      }
      processedRunEventKeysRef.current.add(key);

      const terminalStatus =
        event.type === "run_completed"
          ? "completed"
          : event.type === "run_failed"
            ? "failed"
            : event.type === "run_cancelled"
              ? "cancelled"
              : null;
      if (terminalStatus) {
        setTerminalSync({
          sessionId: event.session_id,
          runId: event.run_id
        });
      }
      const payloadStatus =
        event.type === "run_status" &&
        typeof event.payload.status === "string"
          ? event.payload.status
          : null;
      const approvalId =
        typeof event.payload.approval_id === "string"
          ? event.payload.approval_id
          : typeof event.payload.pending_approval_id === "string"
            ? event.payload.pending_approval_id
            : null;

      let nextRun = runFromEvent(event);
      if (!nextRun && current && (current.run_id === "pending" || current.run_id === event.run_id)) {
        if (terminalStatus) {
          nextRun = {
            ...current,
            run_id: event.run_id,
            session_id: event.session_id,
            status: terminalStatus,
            ended_at: event.ts ?? new Date().toISOString(),
            error:
              terminalStatus === "failed"
                ? previewFromPayload(event.payload.error ?? event.payload.message ?? event.payload.run)
                : current.error,
            pending_approval_id: null
          };
        } else if (event.type === "approval_requested") {
          nextRun = {
            ...current,
            run_id: event.run_id,
            session_id: event.session_id,
            status: "waiting_approval",
            pending_approval_id: approvalId ?? current.pending_approval_id
          };
        } else if (event.type === "approval_resolved" || event.type === "permission_decision") {
          nextRun = {
            ...current,
            run_id: event.run_id,
            session_id: event.session_id,
            status: payloadStatus && isActiveRunStatus(payloadStatus) ? payloadStatus : "running",
            pending_approval_id: null
          };
        } else if (payloadStatus) {
          nextRun = {
            ...current,
            run_id: event.run_id,
            session_id: event.session_id,
            status: payloadStatus,
            pending_approval_id: approvalId ?? current.pending_approval_id
          };
        }
      }

      if (nextRun) {
        currentRunRef.current = nextRun;
        setCurrentRun(nextRun);
      }

      const nextStatus = nextRun?.status ?? terminalStatus ?? payloadStatus ?? undefined;
      const nextActiveRunId =
        nextStatus && isActiveRunStatus(nextStatus) ? event.run_id : terminalStatus ? null : undefined;
      if (!terminalStatus && (nextStatus || nextActiveRunId !== undefined)) {
        const patchSession = (session: SessionSummary): SessionSummary =>
          session.session_id === event.session_id
            ? {
                ...session,
                status: nextStatus ?? session.status,
                active_run_id: nextActiveRunId === undefined ? session.active_run_id : nextActiveRunId
              }
            : session;
        setSessions((currentSessions) => currentSessions.map(patchSession));
      }

      const nextMessage = messageFromRunEvent(event);
      if (nextMessage && messageText(nextMessage).trim().length > 0) {
        setMessages((currentMessages) => appendRunMessage(currentMessages, nextMessage, event));
      }

      const nextTimelineItem = timelineItemFromRunEvent(event);
      if (nextTimelineItem) {
        setTimelineItems((currentItems) => mergeTimelineItem(currentItems, nextTimelineItem));
      }

      const auditEvent = auditEventFromRunEvent(event);
      setEvents((currentEvents) =>
        currentEvents.some((item) => item.event_id === auditEvent.event_id)
          ? currentEvents
          : [...currentEvents, auditEvent]
      );

      if (
        event.type === "approval_requested" ||
        event.type === "approval_resolved" ||
        event.type === "permission_decision"
      ) {
        void refreshApprovals();
      }
    },
    [refreshApprovals]
  );

  const handleRunTerminal = useCallback(
    async (event: RunEvent) => {
      if (event.session_id && selectedSessionIdRef.current === event.session_id) {
        await syncTerminalSession(event.session_id, event.run_id ?? "");
        setSendingSessionId((current) => (current === event.session_id ? null : current));
      } else {
        await loadSessions();
      }
    },
    [loadSessions, syncTerminalSession]
  );

  const recoverRunState = useCallback(
    async (sessionId: string, runId: string) => {
      if (selectedSessionIdRef.current !== sessionId) {
        return;
      }
      try {
        const response = await apiClient.getRun(sessionId, runId);
        if (selectedSessionIdRef.current !== sessionId) {
          return;
        }

        const recoveredRun = response.run;
        currentRunRef.current = recoveredRun;
        setCurrentRun(recoveredRun);

        if (isTerminalRunStatus(recoveredRun.status)) {
          setTerminalSync({
            sessionId,
            runId: recoveredRun.run_id
          });
          await syncTerminalSession(sessionId, recoveredRun.run_id);
          setSendingSessionId((current) =>
            current === sessionId ? null : current
          );
          return;
        }

        const activeRunId = isActiveRunStatus(recoveredRun.status) ? recoveredRun.run_id : null;
        const patchSession = (session: SessionSummary): SessionSummary =>
          session.session_id === sessionId
            ? {
                ...session,
                status: recoveredRun.status,
                active_run_id: activeRunId
              }
            : session;
        setSessions((currentSessions) => currentSessions.map(patchSession));

      } catch {
        if (selectedSessionIdRef.current === sessionId) {
          await refreshCurrentSessionData(sessionId);
        }
      }
    },
    [refreshCurrentSessionData, syncTerminalSession]
  );

  const {
    status: runStreamStatus,
    error: runStreamError,
    lastSeenAt: runStreamLastSeenAt
  }: {
    status: SseConnectionStatus;
    error: string | null;
    lastSeenAt: string | null;
  } = useRunEvents({
    sessionId: selectedSessionId,
    runId: streamRunId,
    enabled: Boolean(selectedSessionId && streamRunId),
    onEvent: handleRunEvent,
    onCompleted: handleRunTerminal,
    onFailed: handleRunTerminal,
    onConnectionError: ({ sessionId, runId }) => {
      void recoverRunState(sessionId, runId);
    }
  });

  useEffect(() => {
    void loadSessions();
  }, []);

  useEffect(() => {
    setDismissedApprovalId(null);
  }, [selectedSessionId]);

  useEffect(() => {
    if (!hasActiveSession) {
      return undefined;
    }
    let disposed = false;
    let inFlight = false;
    const refreshActiveSessions = async () => {
      if (inFlight) {
        return;
      }
      inFlight = true;
      try {
        const response = await apiClient.listSessions();
        if (!disposed) {
          setSessions(
            reconcileSessionsWithRun(
              response.sessions,
              currentRunRef.current
            )
          );
        }
      } catch {
        // The primary request and SSE paths surface connection failures. This
        // quiet poll only keeps background session badges fresh.
      } finally {
        inFlight = false;
      }
    };
    const timer = window.setInterval(() => {
      void refreshActiveSessions();
    }, 1500);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [hasActiveSession]);

  useEffect(() => {
    if (!pendingApprovalForSelected) {
      return;
    }
    if (
      selectedSessionIdRef.current !==
      pendingApprovalForSelected.session_id
    ) {
      return;
    }
    const existingRun = currentRunRef.current;
    const waitingRun: RunSummary = {
      run_id: pendingApprovalForSelected.run_id,
      session_id: pendingApprovalForSelected.session_id,
      status: "waiting_approval",
      started_at:
        existingRun?.run_id === pendingApprovalForSelected.run_id
          ? existingRun.started_at
          : pendingApprovalForSelected.created_at,
      ended_at: null,
      error: null,
      pending_approval_id: pendingApprovalForSelected.approval_id
    };
    currentRunRef.current = waitingRun;
    setCurrentRun(waitingRun);
    setSessions((currentSessions) =>
      currentSessions.map((session) =>
        session.session_id === pendingApprovalForSelected.session_id
          ? {
              ...session,
              status: "waiting_approval",
              active_run_id: pendingApprovalForSelected.run_id
            }
          : session
      )
    );
    if (dismissedApprovalId !== pendingApprovalForSelected.approval_id) {
      setDismissedApprovalId(null);
    }
  }, [dismissedApprovalId, pendingApprovalForSelected]);

  async function handleCreateSession() {
    setIsCreating(true);
    setError(null);
    try {
      const created = await apiClient.createSession();
      await loadSessions(created.session.session_id);
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setIsCreating(false);
    }
  }

  async function sendViaMessagesFallback(targetSessionId: string, content: string) {
    const response = await apiClient.postMessage(targetSessionId, content);
    if (selectedSessionIdRef.current === targetSessionId) {
      setCurrentRun(response.run);
      currentRunRef.current = response.run;
      setMessages(response.messages);
      setTimelineItems(response.timeline ?? []);
      await refreshCurrentSessionData(targetSessionId);
    } else {
      await loadSessions();
    }
  }

  async function handleSend(content: string): Promise<boolean> {
    setError(null);

    let targetSessionId = selectedSessionIdRef.current;
    try {
      if (!targetSessionId) {
        const created = await apiClient.createSession();
        targetSessionId = created.session.session_id;
        selectSessionId(targetSessionId);
        setSessions((current) => [created.session, ...current]);
      }

      setSendingSessionId(targetSessionId);
      processedRunEventKeysRef.current.clear();
      const optimisticRunMarker = `pending:${targetSessionId}:${Date.now()}:${Math.random().toString(36).slice(2)}`;
      if (selectedSessionIdRef.current === targetSessionId) {
        const pendingRun = makePendingRun(targetSessionId);
        setCurrentRun(pendingRun);
        currentRunRef.current = pendingRun;
        setMessages((current) => [...current, makeOptimisticUserMessage(content, optimisticRunMarker)]);
      }

      let response;
      try {
        response = await apiClient.startRun(targetSessionId, content);
      } catch (err) {
        if (err instanceof ApiError && (err.status === 404 || err.status === 405)) {
          await sendViaMessagesFallback(targetSessionId, content);
          return true;
        }
        throw err;
      }

      if (selectedSessionIdRef.current === targetSessionId) {
        setCurrentRun(response.run);
        currentRunRef.current = response.run;
        setMessages((current) =>
          current.map((message) =>
            message[OPTIMISTIC_RUN_ID_KEY] === optimisticRunMarker
              ? { ...message, [OPTIMISTIC_RUN_ID_KEY]: response.run.run_id }
              : message
          )
        );
        const runningSession = {
          ...response.session,
          message_count: response.session.message_count + 1,
          last_user_prompt_preview: {
            preview: content.slice(0, 2000),
            length: content.length,
            truncated: content.length > 2000
          }
        };
        setSessions((current) => [
          runningSession,
          ...current.filter((session) => session.session_id !== runningSession.session_id)
        ]);
        await refreshApprovals();
        if (!isActiveRunStatus(response.run.status)) {
          await refreshCurrentSessionData(targetSessionId);
        }
      } else {
        await loadSessions();
      }
    } catch (err) {
      const activeRunId =
        err instanceof ApiError && typeof err.details.active_run_id === "string"
          ? err.details.active_run_id
          : null;
      if (
        targetSessionId &&
        activeRunId &&
        err instanceof ApiError &&
        err.status === 409 &&
        err.code === "session_running" &&
        selectedSessionIdRef.current === targetSessionId
      ) {
        setError(apiErrorMessage(err));
        await recoverRunState(targetSessionId, activeRunId);
        return false;
      }
      if (!targetSessionId || selectedSessionIdRef.current === targetSessionId) {
        setError(apiErrorMessage(err));
      }
      if (targetSessionId) {
        if (selectedSessionIdRef.current === targetSessionId) {
          await refreshCurrentSessionData(targetSessionId);
        } else {
          await loadSessions();
        }
      }
      return false;
    } finally {
      setSendingSessionId((current) => (current === targetSessionId ? null : current));
      if (targetSessionId && selectedSessionIdRef.current === targetSessionId) {
        await refreshApprovals();
      }
    }
    return true;
  }

  async function handleResolveApproval(approval: Approval, decision: ApprovalDecision, message: string) {
    await resolveApproval(approval, decision, message);
    if (selectedSessionIdRef.current === approval.session_id) {
      const current = currentRunRef.current;
      if (
        current &&
        current.run_id === approval.run_id &&
        isActiveRunStatus(current.status) &&
        current.status !== "cancelling"
      ) {
        const resumedRun = {
          ...current,
          status: "running",
          pending_approval_id: null
        };
        currentRunRef.current = resumedRun;
        setCurrentRun(resumedRun);
        const resumeSession = (session: SessionSummary): SessionSummary =>
          session.session_id === approval.session_id
            ? {
                ...session,
                status: "running",
                active_run_id: approval.run_id
              }
            : session;
        setSessions((sessions) => sessions.map(resumeSession));
      }
      setDismissedApprovalId(approval.approval_id);
    } else {
      await loadSessions();
    }
  }

  async function handleArchiveSession(sessionId: string) {
    if (!window.confirm("归档后该会话将从活动列表移除。继续吗？")) {
      return;
    }
    setIsArchiving(true);
    setError(null);
    try {
      await apiClient.archiveSession(sessionId);
      if (selectedSessionIdRef.current === sessionId) {
        selectSessionId(null);
        setMessages([]);
        setTimelineItems([]);
        setEvents([]);
        setCurrentRun(null);
        currentRunRef.current = null;
      }
      await loadSessions();
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setIsArchiving(false);
    }
  }

  async function handleCancelRun() {
    const run = currentRunRef.current;
    const sessionId = selectedSessionIdRef.current;
    if (
      !run ||
      !sessionId ||
      run.session_id !== sessionId ||
      run.run_id === "pending" ||
      !isActiveRunStatus(run.status)
    ) {
      return;
    }
    setIsCancelling(true);
    setError(null);
    try {
      await apiClient.cancelRun(sessionId, run.run_id);
    } catch (err) {
      if (selectedSessionIdRef.current === sessionId) {
        setError(apiErrorMessage(err));
      }
    } finally {
      setIsCancelling(false);
    }
  }

  const composerDisabledReason = pendingApprovalForSelected
    ? "等待权限审批"
    : isSendingCurrentSession
      ? "消息正在发送"
      : isWaitingApproval
        ? "等待权限审批"
        : isTerminalSyncCurrent
          ? "正在同步最终回答"
          : isRunActive
            ? currentRunBelongsToSelected && currentRun?.status === "cancelling"
              ? "正在停止运行"
              : "运行正在进行"
            : isLoadingSession
              ? "会话正在加载"
              : null;

  const shellTitle = useMemo(() => {
    if (!selectedSession) {
      return "Coding Agent Workbench";
    }
    const preview = selectedSession.last_user_prompt_preview?.preview;
    return selectedSession.title || preview || "新会话";
  }, [selectedSession]);

  const shellSubtitle = selectedSession
    ? `${selectedSession.session_id} · ${selectedSession.message_count} 条消息`
    : "本地 Codex-like 对话工作台";

  return (
    <AppShell
      title={shellTitle}
      subtitle={shellSubtitle}
      error={error}
      inspectorOpen={inspectorOpen}
      onToggleInspector={() => setInspectorOpen((value) => !value)}
      sidebar={
        <SessionSidebar
          sessions={sessions}
          selectedSessionId={selectedSessionId}
          health={health}
          isLoading={isLoadingSessions}
          isCreating={isCreating}
          isArchiving={isArchiving}
          onCreate={handleCreateSession}
          onRefresh={() => void loadSessions()}
          onSelect={(sessionId) => void loadSession(sessionId)}
          onArchive={(sessionId) => void handleArchiveSession(sessionId)}
        />
      }
      chat={<ChatThread session={selectedSession} messages={messages} isLoading={isLoadingSession} isRunning={isRunning} />}
      composer={
        <Composer
          disabled={
            isSendingCurrentSession ||
            isLoadingSession ||
            isRunActive ||
            isWaitingApproval ||
            isTerminalSyncCurrent
          }
          isRunning={isRunning}
          disabledReason={composerDisabledReason}
          onSend={handleSend}
          onCancel={handleCancelRun}
          isCancelling={isCancelling}
        />
      }
      inspector={
        <InspectorPanel
          session={selectedSession}
          currentRun={currentRunBelongsToSelected ? currentRun : null}
          isRunning={isRunning}
          timelineItems={timelineItems}
          events={events}
          timelineNotice={timelineNotice}
          isLoadingTimeline={isLoadingSidecar}
          approvals={approvalsForSelected}
          pendingApproval={pendingApprovalForSelected}
          lastResolvedApproval={lastResolvedApprovalForSelected}
          approvalsLoading={approvalsLoading}
          approvalsError={approvalsError}
          sseStatus={runStreamStatus}
          sseError={runStreamError}
          sseLastSeenAt={runStreamLastSeenAt}
          onOpenApproval={(approval) => setDismissedApprovalId((current) => current === approval.approval_id ? null : current)}
          onRefreshSessionActivity={(sessionId) => loadSidecarData(sessionId)}
        />
      }
    >
      <ApprovalModal
        approval={
          pendingApprovalForSelected &&
          dismissedApprovalId !== pendingApprovalForSelected.approval_id
            ? pendingApprovalForSelected
            : null
        }
        isSubmitting={approvalResolving}
        error={approvalsError}
        onResolve={handleResolveApproval}
        onDismiss={() =>
          pendingApprovalForSelected
            ? setDismissedApprovalId(
                pendingApprovalForSelected.approval_id
              )
            : undefined
        }
      />
    </AppShell>
  );
}
