import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiClient } from "../api/client";
import { ApiError, type Approval, type ApprovalDecision } from "../api/types";

function formatApprovalError(error: unknown): string {
  if (error instanceof ApiError) {
    const errors = error.details.errors;
    if (Array.isArray(errors) && errors.length > 0) {
      const first = errors[0] as Record<string, unknown>;
      const loc = Array.isArray(first.loc) ? first.loc.join(".") : "";
      const msg = typeof first.msg === "string" ? first.msg : JSON.stringify(first);
      return `${error.message}（${error.code}：${loc ? `${loc} ` : ""}${msg}）`;
    }
    return `${error.message}（${error.code}）`;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "未知审批错误";
}

export function useApprovals(sessionId: string | null, intervalMs = 1500) {
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [lastResolvedApproval, setLastResolvedApproval] = useState<Approval | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isResolving, setIsResolving] = useState(false);
  const [resolvingApprovalId, setResolvingApprovalId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const sessionRef = useRef(sessionId);
  const revisionRef = useRef(0);
  const mutationInFlightRef = useRef(false);
  const refreshRef = useRef<{
    sessionId: string;
    revision: number;
    promise: Promise<void>;
  } | null>(null);

  useEffect(() => {
    sessionRef.current = sessionId;
    revisionRef.current += 1;
    mutationInFlightRef.current = false;
    setApprovals([]);
    setLastResolvedApproval(null);
    setIsResolving(false);
    setResolvingApprovalId(null);
    setError(null);
  }, [sessionId]);

  const refresh = useCallback(async () => {
    if (!sessionId) {
      setApprovals([]);
      setIsLoading(false);
      return;
    }
    const requestSessionId = sessionId;
    const requestRevision = revisionRef.current;
    if (
      refreshRef.current?.sessionId === requestSessionId &&
      refreshRef.current.revision === requestRevision
    ) {
      return refreshRef.current.promise;
    }
    const token = {
      sessionId: requestSessionId,
      revision: requestRevision,
      promise: Promise.resolve()
    };
    const request = (async () => {
      setIsLoading(true);
      try {
        const response = await apiClient.listApprovals({
          session_id: requestSessionId,
          include_resolved: false
        });
        if (
          sessionRef.current === requestSessionId &&
          revisionRef.current === requestRevision &&
          !mutationInFlightRef.current
        ) {
          setApprovals(response.approvals);
          setError(null);
        }
      } catch (err) {
        if (
          sessionRef.current === requestSessionId &&
          revisionRef.current === requestRevision
        ) {
          setError(formatApprovalError(err));
        }
      } finally {
        if (refreshRef.current === token) {
          refreshRef.current = null;
          if (sessionRef.current === requestSessionId) {
            setIsLoading(false);
          }
        }
      }
    })();
    token.promise = request;
    refreshRef.current = token;
    return request;
  }, [sessionId]);

  useEffect(() => {
    if (!sessionId) {
      return undefined;
    }

    void refresh();
    const timer = window.setInterval(() => {
      void refresh();
    }, intervalMs);

    return () => {
      window.clearInterval(timer);
    };
  }, [intervalMs, refresh, sessionId]);

  const resolveApproval = useCallback(
    async (approval: Approval, decision: ApprovalDecision, message = "") => {
      const requestSessionId = approval.session_id;
      mutationInFlightRef.current = true;
      revisionRef.current += 1;
      setIsResolving(true);
      setResolvingApprovalId(approval.approval_id);
      setError(null);
      try {
        const response = await apiClient.resolveApproval(
          approval.approval_id,
          { decision, message },
            { session_id: approval.session_id, run_id: approval.run_id }
        );
        if (sessionRef.current === requestSessionId) {
          revisionRef.current += 1;
          setLastResolvedApproval(response.approval);
          setApprovals((current) =>
            current.filter((item) => item.approval_id !== approval.approval_id)
          );
          mutationInFlightRef.current = false;
          void refresh();
        }
        return response.approval;
      } catch (err) {
        if (sessionRef.current === requestSessionId) {
          setError(formatApprovalError(err));
        }
        throw err;
      } finally {
        if (sessionRef.current === requestSessionId) {
          mutationInFlightRef.current = false;
          setIsResolving(false);
          setResolvingApprovalId(null);
        }
      }
    },
    [refresh]
  );

  const pendingApproval = useMemo(
    () => approvals.find((approval) => approval.status === "pending") ?? null,
    [approvals]
  );

  return {
    approvals,
    pendingApproval,
    lastResolvedApproval,
    isLoading,
    isResolving,
    resolvingApprovalId,
    error,
    refresh,
    resolveApproval
  };
}
