"""In-process Web approval registry and permission resolver."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator
from uuid import uuid4

from ..hooks import use_permission_resolver
from ..runtime.execution import current_execution_context
from ..runtime.events import (
    current_event_context,
    event_context,
    log_event,
    safe_text_preview,
)
from ..security.policy import PolicyDecision, audit_policy_decision

WEB_APPROVAL_DENIED_REASON = "Permission denied by Web approval."
WEB_APPROVAL_EXPIRED_REASON = "Permission approval timed out; denied."
WEB_APPROVAL_CANCELLED_REASON = "Permission approval cancelled; denied."
WEB_APPROVAL_CONTEXT_MISSING_REASON = (
    "Permission approval context missing; denied."
)
# Kept for import compatibility with WUI-01 tests/extensions.
WEB_AUTO_DENY_REASON = "Permission requires Web approval; denied in MVP."
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 300.0
MAX_RESOLVED_APPROVALS = 500


class ApprovalError(Exception):
    status_code = 500
    code = "approval_error"
    message = "Approval error."

    def __init__(self, message: str | None = None,
                 details: dict | None = None):
        super().__init__(message or self.message)
        self.message = message or self.message
        self.details = details or {}


class ApprovalNotFound(ApprovalError):
    status_code = 404
    code = "approval_not_found"
    message = "Approval was not found."


class ApprovalAlreadyResolved(ApprovalError):
    status_code = 409
    code = "approval_already_resolved"
    message = "Approval has already been resolved."


class ApprovalMismatch(ApprovalError):
    status_code = 409
    code = "approval_mismatch"
    message = "Approval does not match the requested session or run."


@dataclass
class PendingApproval:
    approval_id: str
    session_id: str
    run_id: str
    tool_name: str
    tool_use_id: str | None
    input_preview: dict
    reason: str
    rule: str
    created_at: str
    expires_at: str
    timeout_seconds: float
    status: str = "pending"
    decision: str | None = None
    message: str | None = None
    resolved_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_timestamp(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _subject_preview(decision: PolicyDecision) -> dict:
    return safe_text_preview(decision.subject or "", limit=500)


def _approval_payload(approval: PendingApproval) -> dict:
    data = approval.to_dict()
    data["source"] = "web"
    return data


class ApprovalRegistry:
    def __init__(self, timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
                 workspace: str | Path | None = None):
        self.timeout_seconds = float(timeout_seconds)
        self.workspace = (Path.cwd() if workspace is None else Path(workspace)).resolve()
        self._condition = threading.Condition()
        self._approvals: dict[str, PendingApproval] = {}

    def create(self, decision: PolicyDecision, *,
               session_id: str, run_id: str,
               on_pending: Callable[[PendingApproval], None] | None = None,
               ) -> PendingApproval:
        now = _utc_now()
        expires_at = now + timedelta(seconds=self.timeout_seconds)
        approval = PendingApproval(
            approval_id=f"appr_{uuid4().hex[:10]}",
            session_id=session_id,
            run_id=run_id,
            tool_name=decision.tool,
            tool_use_id=decision.tool_use_id,
            input_preview=_subject_preview(decision),
            reason=decision.reason,
            rule=decision.rule,
            created_at=_utc_timestamp(now),
            expires_at=_utc_timestamp(expires_at),
            timeout_seconds=self.timeout_seconds,
        )
        with self._condition:
            self._approvals[approval.approval_id] = approval
            try:
                if on_pending:
                    on_pending(approval)
            except Exception:
                self._approvals.pop(approval.approval_id, None)
                raise
            self._condition.notify_all()
        self._log_requested(approval)
        return approval

    def list(self, *, session_id: str | None = None,
             run_id: str | None = None,
             include_resolved: bool = True) -> list[dict]:
        self.expire_due()
        with self._condition:
            approvals = list(self._approvals.values())
        filtered = []
        for approval in approvals:
            if session_id is not None and approval.session_id != session_id:
                continue
            if run_id is not None and approval.run_id != run_id:
                continue
            if not include_resolved and approval.status != "pending":
                continue
            filtered.append(approval)
        filtered.sort(
            key=lambda item: (
                item.status != "pending",
                item.created_at,
            )
        )
        return [approval.to_dict() for approval in filtered]

    def get(self, approval_id: str) -> dict:
        self.expire_due()
        approval = self._get_approval(approval_id)
        return approval.to_dict()

    def resolve(self, approval_id: str, decision: str, *,
                message: str = "",
                session_id: str | None = None,
                run_id: str | None = None) -> dict:
        if decision not in ("allow", "deny"):
            raise ValueError("decision must be allow or deny")
        with self._condition:
            approval = self._approvals.get(approval_id)
            if approval is None:
                raise ApprovalNotFound(details={"approval_id": approval_id})
            if session_id is not None and approval.session_id != session_id:
                raise ApprovalMismatch(details={"approval_id": approval_id})
            if run_id is not None and approval.run_id != run_id:
                raise ApprovalMismatch(details={"approval_id": approval_id})
            if approval.status != "pending":
                raise ApprovalAlreadyResolved(details={
                    "approval_id": approval_id,
                    "status": approval.status,
                })
            status = "allowed" if decision == "allow" else "denied"
            self._finish_locked(approval, status, decision, message)
            self._prune_resolved_locked()
            # Persist/publish the approval terminal event before waking the
            # agent thread. Otherwise a fast tool + final turn can publish
            # run_completed before approval_resolved.
            self._log_resolved(approval)
            self._condition.notify_all()
            payload = approval.to_dict()
        return payload

    def wait(self, approval_id: str) -> PendingApproval:
        deadline = time.monotonic() + self.timeout_seconds
        with self._condition:
            approval = self._approvals.get(approval_id)
            if approval is None:
                raise ApprovalNotFound(details={"approval_id": approval_id})
            while approval.status == "pending":
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._finish_locked(
                        approval,
                        "expired",
                        "deny",
                        WEB_APPROVAL_EXPIRED_REASON,
                    )
                    self._prune_resolved_locked()
                    self._log_resolved(approval)
                    self._condition.notify_all()
                    break
                self._condition.wait(timeout=remaining)
            result = approval
        return result

    def cancel_run(self, run_id: str,
                   message: str = WEB_APPROVAL_CANCELLED_REASON) -> list[dict]:
        cancelled: list[PendingApproval] = []
        with self._condition:
            for approval in self._approvals.values():
                if approval.run_id == run_id and approval.status == "pending":
                    self._finish_locked(approval, "cancelled", "deny", message)
                    cancelled.append(approval)
            if cancelled:
                self._prune_resolved_locked()
                for approval in cancelled:
                    self._log_resolved(approval)
                self._condition.notify_all()
        return [approval.to_dict() for approval in cancelled]

    def expire_due(self) -> None:
        now = _utc_now()
        expired: list[PendingApproval] = []
        with self._condition:
            for approval in self._approvals.values():
                if approval.status != "pending":
                    continue
                try:
                    expires_at = datetime.fromisoformat(
                        approval.expires_at.replace("Z", "+00:00"))
                except Exception:
                    continue
                if expires_at <= now:
                    self._finish_locked(
                        approval,
                        "expired",
                        "deny",
                        WEB_APPROVAL_EXPIRED_REASON,
                    )
                    expired.append(approval)
            if expired:
                self._prune_resolved_locked()
                for approval in expired:
                    self._log_resolved(approval)
                self._condition.notify_all()

    def resolve_policy_decision(
        self,
        decision: PolicyDecision,
        *,
        on_waiting: Callable[[PendingApproval], None] | None = None,
        on_resolved: Callable[[PendingApproval], None] | None = None,
    ) -> str | None:
        if decision.action == "deny":
            return decision.reason
        if decision.action != "ask":
            return None

        execution = current_execution_context()
        audit = current_event_context()
        session_id = execution.get("session_id") or audit.get("session_id")
        run_id = execution.get("run_id") or audit.get("run_id")
        if not session_id or not run_id:
            audit_policy_decision(
                decision.with_outcome(
                    "deny",
                    WEB_APPROVAL_CONTEXT_MISSING_REASON,
                    "web_approval_context_missing",
                )
            )
            return WEB_APPROVAL_CONTEXT_MISSING_REASON

        approval = self.create(
            decision,
            session_id=session_id,
            run_id=run_id,
            on_pending=on_waiting,
        )
        result = self.wait(approval.approval_id)
        if on_resolved:
            on_resolved(result)

        if result.status == "allowed":
            audit_policy_decision(
                decision.with_outcome(
                    "allow",
                    result.message or "Permission allowed by Web approval",
                    "web_approval",
                )
            )
            return None

        source = {
            "denied": "web_approval",
            "expired": "web_approval_timeout",
            "cancelled": "web_approval_cancelled",
        }.get(result.status, "web_approval")
        reason = result.message or WEB_APPROVAL_DENIED_REASON
        audit_policy_decision(decision.with_outcome("deny", reason, source))
        return reason

    def _get_approval(self, approval_id: str) -> PendingApproval:
        with self._condition:
            approval = self._approvals.get(approval_id)
            if approval is None:
                raise ApprovalNotFound(details={"approval_id": approval_id})
            return approval

    def forget_session(self, session_id: str) -> None:
        with self._condition:
            pending = [
                approval
                for approval in self._approvals.values()
                if approval.session_id == session_id
                and approval.status == "pending"
            ]
            if pending:
                raise RuntimeError("Cannot forget a session with pending approvals.")
            self._approvals = {
                approval_id: approval
                for approval_id, approval in self._approvals.items()
                if approval.session_id != session_id
            }

    def _prune_resolved_locked(self) -> None:
        resolved = sorted(
            (
                approval
                for approval in self._approvals.values()
                if approval.status != "pending"
            ),
            key=lambda approval: approval.resolved_at or approval.created_at,
            reverse=True,
        )
        for approval in resolved[MAX_RESOLVED_APPROVALS:]:
            self._approvals.pop(approval.approval_id, None)

    @staticmethod
    def _finish_locked(approval: PendingApproval, status: str,
                       decision: str, message: str) -> None:
        approval.status = status
        approval.decision = decision
        approval.message = message
        approval.resolved_at = _utc_timestamp()

    def _log_requested(self, approval: PendingApproval) -> None:
        self._log_approval_event("approval_requested", approval)

    def _log_resolved(self, approval: PendingApproval) -> None:
        self._log_approval_event("approval_resolved", approval)

    def _log_approval_event(self, event_type: str,
                            approval: PendingApproval) -> None:
        with event_context(
            session_id=approval.session_id,
            run_id=approval.run_id,
            source="web",
            workspace=self.workspace,
        ):
            log_event(event_type, _approval_payload(approval))


@contextmanager
def web_permission_context(
    approval_registry: ApprovalRegistry,
    *,
    on_waiting: Callable[[PendingApproval], None] | None = None,
    on_resolved: Callable[[PendingApproval], None] | None = None,
) -> Iterator[None]:
    def resolver(decision: PolicyDecision) -> str | None:
        return approval_registry.resolve_policy_decision(
            decision,
            on_waiting=on_waiting,
            on_resolved=on_resolved,
        )

    with use_permission_resolver(resolver):
        yield
