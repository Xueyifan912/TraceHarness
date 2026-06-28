"""Web-facing orchestration around the reusable runtime AgentLoop."""

from __future__ import annotations

import threading
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable

from ..background import abandon_background_tasks
from ..cron_scheduler import (
    cancel_session_jobs,
    consume_web_cron_queue,
    requeue_cron_job,
)
from ..memory.context import update_context
from ..message_utils import is_internal_message, public_chat_messages
from ..runtime.execution import execution_context
from ..runtime.cancellation import RunCancelled, cancellation_context
from ..runtime.events import (
    event_context,
    log_event,
    log_user_prompt_submission,
    scrub_sensitive_text,
)
from ..runtime.loop import AgentLoop
from ..runtime.session import (
    SessionRecord,
    archive_session,
    create_session,
    load_session,
    scan_recent_sessions,
    save_session_snapshot,
    session_file_path,
)
from .approvals import ApprovalRegistry, PendingApproval, web_permission_context
from .event_store import EventStore
from .run_registry import (
    RunState,
    SessionAlreadyRunning,
    SessionRunRegistry,
)

ASSISTANT_TEXT_PREVIEW_LIMIT = 4000
ASSISTANT_CONTENT_STRING_LIMIT = 4000
ASSISTANT_CONTENT_MAX_ITEMS = 50
ASSISTANT_CONTENT_MAX_KEYS = 50


class WebApiError(Exception):
    status_code = 500
    code = "internal_error"
    message = "Internal error."

    def __init__(self, message: str | None = None,
                 details: dict[str, Any] | None = None):
        super().__init__(message or self.message)
        self.message = message or self.message
        self.details = details or {}


class SessionNotFound(WebApiError):
    status_code = 404
    code = "session_not_found"
    message = "Session was not found."


class InvalidRequest(WebApiError):
    status_code = 400
    code = "invalid_request"
    message = "Invalid request."


class SessionRunning(WebApiError):
    status_code = 409
    code = "session_running"
    message = "This session already has a running agent loop."


class RunNotFound(WebApiError):
    status_code = 404
    code = "run_not_found"
    message = "Run was not found."


class PersistenceFailed(WebApiError):
    status_code = 500
    code = "persistence_failed"
    message = "Session state could not be persisted."


class AgentExecutionFailed(WebApiError):
    status_code = 502
    code = "agent_execution_failed"
    message = "The agent run failed."


class SessionCorrupt(WebApiError):
    status_code = 500
    code = "session_corrupt"
    message = "The session snapshot is corrupt or unreadable."


class RunStartFailed(WebApiError):
    status_code = 500
    code = "run_start_failed"
    message = "The background run could not be started."


class SessionArchiveFailed(WebApiError):
    status_code = 500
    code = "session_archive_failed"
    message = "The session could not be archived."


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        try:
            return _jsonable(value.model_dump())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return str(value)


def _text_preview(value: Any, limit: int = 2000) -> dict[str, Any]:
    original = str(value)
    text = scrub_sensitive_text(original)
    return {
        "preview": text[:limit],
        "length": len(original),
        "truncated": len(original) > limit,
    }


def _scrub_jsonable(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_sensitive_text(value)
    if isinstance(value, list):
        return [_scrub_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _scrub_jsonable(item)
            for key, item in value.items()
        }
    return value


def _truncate_jsonable(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        truncated = len(value) > ASSISTANT_CONTENT_STRING_LIMIT
        return value[:ASSISTANT_CONTENT_STRING_LIMIT], truncated
    if value is None or isinstance(value, (int, float, bool)):
        return value, False
    if isinstance(value, list):
        items = value[:ASSISTANT_CONTENT_MAX_ITEMS]
        truncated = len(value) > ASSISTANT_CONTENT_MAX_ITEMS
        output = []
        for item in items:
            bounded, item_truncated = _truncate_jsonable(item)
            output.append(bounded)
            truncated = truncated or item_truncated
        return output, truncated
    if isinstance(value, dict):
        items = list(value.items())
        truncated = len(items) > ASSISTANT_CONTENT_MAX_KEYS
        output = {}
        for key, item in items[:ASSISTANT_CONTENT_MAX_KEYS]:
            bounded, item_truncated = _truncate_jsonable(item)
            output[str(key)] = bounded
            truncated = truncated or item_truncated
        return output, truncated
    return _truncate_jsonable(_jsonable(value))


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_extract_text(item) for item in value)
    if isinstance(value, dict):
        if value.get("type") == "text" and "text" in value:
            return str(value.get("text") or "")
        content = value.get("content")
        if content is not None:
            return _extract_text(content)
    return ""


def _assistant_message_payload(message: dict[str, Any]) -> dict[str, Any]:
    content = _jsonable(message.get("content"))
    bounded_content, content_truncated = _truncate_jsonable(content)
    bounded_content = _scrub_jsonable(bounded_content)
    text = _extract_text(content)
    safe_text = scrub_sensitive_text(text)
    text_truncated = len(text) > ASSISTANT_TEXT_PREVIEW_LIMIT
    return {
        "role": message.get("role"),
        "content": bounded_content,
        "text_preview": safe_text[:ASSISTANT_TEXT_PREVIEW_LIMIT],
        "truncated": content_truncated or text_truncated,
    }


def _display_messages(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    stored = snapshot.get("display_messages")
    if isinstance(stored, list):
        return [
            _jsonable(message)
            for message in public_chat_messages(stored)
        ]
    runtime_messages = snapshot.get("messages")
    if not isinstance(runtime_messages, list):
        return []
    visible = [
        _jsonable(message)
        for message in public_chat_messages(runtime_messages)
    ]
    if not any(message.get("role") == "user" for message in visible):
        preview = snapshot.get("last_user_prompt_preview")
        if isinstance(preview, dict):
            prompt = str(preview.get("preview") or "").strip()
            if prompt:
                visible.insert(0, {"role": "user", "content": prompt})
    return visible


def _new_visible_assistant_messages(
    messages: list,
    existing_message_ids: set[int],
) -> list[dict[str, Any]]:
    return [
        _jsonable(message)
        for message in messages
        if (
            isinstance(message, dict)
            and id(message) not in existing_message_ids
            and message.get("role") == "assistant"
            and public_chat_messages([message])
        )
    ]


class AgentService:
    def __init__(
        self,
        workspace: str | Path | None = None,
        *,
        registry: SessionRunRegistry | None = None,
        event_store: EventStore | None = None,
        approval_registry: ApprovalRegistry | None = None,
        loop_factory: Callable[[], AgentLoop] = AgentLoop,
    ):
        self.workspace = (Path.cwd() if workspace is None else Path(workspace)).resolve()
        self.registry = registry or SessionRunRegistry(self.workspace)
        self.event_store = event_store or EventStore(self.workspace)
        self.approval_registry = approval_registry or ApprovalRegistry(
            workspace=self.workspace)
        self.approval_registry.workspace = self.workspace
        self.loop_factory = loop_factory
        self._service_guard = threading.Lock()
        self._service_stop = threading.Event()
        self._cron_thread: threading.Thread | None = None
        for interrupted in self.registry.take_recovered_interrupts():
            self._log_run_event("run_status", interrupted)
            self._log_run_event("run_failed", interrupted)

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "app": "coding-agent-harness-web",
            "workspace_path": str(self.workspace),
            "version": "0.1",
        }

    def start_background_services(self) -> None:
        with self._service_guard:
            if self._cron_thread and self._cron_thread.is_alive():
                return
            self._service_stop.clear()
            self._cron_thread = threading.Thread(
                target=self._cron_dispatch_loop,
                name=f"web-cron-{id(self):x}",
                daemon=True,
            )
            self._cron_thread.start()

    def shutdown(self) -> None:
        self._service_stop.set()
        with self._service_guard:
            thread = self._cron_thread
            self._cron_thread = None
        if thread and thread.is_alive():
            thread.join(timeout=2)

    def _cron_dispatch_loop(self) -> None:
        while not self._service_stop.wait(0.25):
            self._dispatch_due_cron_jobs_once()

    def _dispatch_due_cron_jobs_once(self) -> None:
        for job in consume_web_cron_queue(self.workspace):
            if not job.session_id:
                continue
            try:
                self.start_run(
                    job.session_id,
                    f"[Scheduled] {job.prompt}",
                )
            except SessionRunning:
                requeue_cron_job(job, self.workspace)
            except (SessionNotFound, SessionCorrupt) as exc:
                cancel_session_jobs(job.session_id, self.workspace)
                with event_context(
                    session_id=job.session_id,
                    source="web_cron",
                    workspace=self.workspace,
                    clear_run_id=True,
                ):
                    log_event("cron_dispatch_failed", {
                        "job_id": job.id,
                        "error_type": type(exc).__name__,
                    })
            except Exception as exc:
                requeue_cron_job(job, self.workspace)
                with event_context(
                    session_id=job.session_id,
                    source="web_cron",
                    workspace=self.workspace,
                    clear_run_id=True,
                ):
                    log_event("cron_dispatch_failed", {
                        "job_id": job.id,
                        "error_type": type(exc).__name__,
                    })

    def create_session(self, title: str | None = None,
                       initial_message: str | None = None) -> dict[str, Any]:
        normalized_title = str(title or "").strip() or None
        record = create_session(self.workspace, title=normalized_title)
        if not record.path.exists():
            raise PersistenceFailed(details={
                "session_id": record.session_id,
            })
        messages: list[dict[str, Any]] = []
        prompt = (initial_message or "").strip()
        if prompt:
            messages.append({"role": "user", "content": prompt})
            if not save_session_snapshot(
                record,
                messages,
                last_user_prompt=prompt,
                display_messages=messages,
            ):
                raise PersistenceFailed(details={
                    "session_id": record.session_id,
                })
        snapshot = self._load_snapshot(record.session_id)
        return {"session": self._summary(snapshot)}

    def list_sessions(self, limit: int = 20) -> dict[str, Any]:
        sessions = []
        recent, warnings = scan_recent_sessions(
            self.workspace,
            limit=limit,
        )
        warnings.extend(self.registry.restore_warnings())
        for item in recent:
            session_id = item.get("session_id")
            if not session_id:
                continue
            snapshot = load_session(str(session_id), self.workspace) or item
            sessions.append(self._summary(snapshot))
        return {
            "sessions": sessions,
            "warnings": warnings,
        }

    def get_session(self, session_id: str) -> dict[str, Any]:
        snapshot = self._load_snapshot(session_id)
        runtime_messages = snapshot.get("messages", [])
        return {
            "session": self._summary(snapshot),
            "messages": [
                _jsonable(message)
                for message in runtime_messages
                if (
                    isinstance(message, dict)
                    and not is_internal_message(message)
                )
            ],
            "display_messages": _display_messages(snapshot),
        }

    def archive_session(self, session_id: str) -> dict[str, Any]:
        self._load_snapshot(session_id)
        status, active_run_id = self.registry.session_status(session_id)
        if active_run_id is not None:
            raise SessionRunning(details={"active_run_id": active_run_id})
        try:
            archived_path = archive_session(session_id, self.workspace)
        except Exception as exc:
            raise SessionArchiveFailed(details={
                "session_id": session_id,
                "error_type": type(exc).__name__,
            }) from exc
        if archived_path is None:
            raise SessionNotFound(details={"session_id": session_id})
        cancel_session_jobs(session_id, self.workspace)
        self.registry.forget_session(session_id)
        self.approval_registry.forget_session(session_id)
        return {
            "ok": True,
            "session_id": session_id,
            "archived": True,
        }

    def post_message(self, session_id: str, content: str,
                     save: bool = True) -> dict[str, Any]:
        prompt = self._normalise_prompt(content)
        snapshot = self._load_snapshot(session_id)
        run = self._start_run(session_id)
        return self._execute_run(
            session_id=session_id,
            snapshot=snapshot,
            run=run,
            prompt=prompt,
            save=save,
            raise_errors=True,
        )

    def start_run(self, session_id: str, content: str,
                  save: bool = True) -> dict[str, Any]:
        prompt = self._normalise_prompt(content)
        snapshot = self._load_snapshot(session_id)
        run = self._start_run(session_id)
        response = {
            "run": run.to_dict(),
            "session": self._summary(snapshot),
        }
        thread = threading.Thread(
            target=self._background_run,
            args=(session_id, snapshot, run, prompt, save),
            name=f"web-run-{run.run_id}",
            daemon=True,
        )
        try:
            thread.start()
        except Exception as exc:
            failed = self.registry.fail(
                run.run_id,
                scrub_sensitive_text(f"{type(exc).__name__}: {exc}"),
            )
            with event_context(
                session_id=session_id,
                run_id=run.run_id,
                source="web",
                workspace=self.workspace,
            ):
                self._log_run_event("run_failed", failed)
            raise RunStartFailed(details={
                "session_id": session_id,
                "run_id": run.run_id,
                "error_type": type(exc).__name__,
            }) from exc
        return response

    def get_run(self, session_id: str, run_id: str) -> dict[str, Any]:
        self._load_snapshot(session_id)
        run = self.registry.get_session_run(session_id, run_id)
        if run is None:
            raise RunNotFound(details={
                "session_id": session_id,
                "run_id": run_id,
            })
        return {"run": run.to_dict()}

    def cancel_run(self, session_id: str, run_id: str) -> dict[str, Any]:
        run = self.registry.request_cancel(session_id, run_id)
        if run is None:
            self._load_snapshot(session_id)
            raise RunNotFound(details={
                "session_id": session_id,
                "run_id": run_id,
            })
        if run.status not in {"completed", "failed", "cancelled"}:
            self.approval_registry.cancel_run(run_id)
            with event_context(
                session_id=session_id,
                run_id=run_id,
                source="web",
                workspace=self.workspace,
            ):
                self._log_run_event("run_status", run)
                log_event("run_cancel_requested", {"run": run.to_dict()})
        return {"run": run.to_dict()}

    def resolve_approval(
        self,
        approval_id: str,
        decision: str,
        *,
        message: str = "",
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        approval = self.approval_registry.resolve(
            approval_id,
            decision,
            message=message,
            session_id=session_id,
            run_id=run_id,
        )
        # Resume the authoritative run state before returning the HTTP
        # response. The waiting worker calls the same idempotent transition
        # after it wakes, so clients never observe a resolved approval paired
        # with a still-waiting run.
        self._run_resolved_approval(str(approval["run_id"]), None)
        return approval

    def _background_run(
        self,
        session_id: str,
        snapshot: dict[str, Any],
        run: RunState,
        prompt: str,
        save: bool,
    ) -> None:
        self._execute_run(
            session_id=session_id,
            snapshot=snapshot,
            run=run,
            prompt=prompt,
            save=save,
            raise_errors=False,
        )

    def _execute_run(
        self,
        *,
        session_id: str,
        snapshot: dict[str, Any],
        run: RunState,
        prompt: str,
        save: bool,
        raise_errors: bool,
    ) -> dict[str, Any] | None:
        messages = list(snapshot.get("messages", []))
        existing_message_ids = {
            id(message)
            for message in messages
            if isinstance(message, dict)
        }
        display_messages = _display_messages(snapshot)
        messages.append({"role": "user", "content": prompt})
        display_messages.append({"role": "user", "content": prompt})
        assistant_messages: list[dict[str, Any]] = []
        assistant_messages_captured = False

        def capture_assistant_messages() -> None:
            nonlocal assistant_messages, assistant_messages_captured
            if assistant_messages_captured:
                return
            assistant_messages = _new_visible_assistant_messages(
                messages,
                existing_message_ids,
            )
            display_messages.extend(assistant_messages)
            for message in assistant_messages:
                log_event(
                    "assistant_message",
                    _assistant_message_payload(message),
                )
            assistant_messages_captured = True

        def persist_transcript() -> bool:
            if not save:
                return True
            record = self._record_from_snapshot(session_id, snapshot)
            return save_session_snapshot(
                record,
                messages,
                last_user_prompt=prompt,
                display_messages=display_messages,
            )

        with execution_context(
            session_id=session_id,
            run_id=run.run_id,
            source="web",
            workspace=self.workspace,
        ), event_context(
            session_id=session_id,
            run_id=run.run_id,
            source="web",
            workspace=self.workspace,
        ):
            try:
                self._log_run_event("run_started", run)
                self._log_run_event("run_status", run)
                log_event("user_message", {
                    "role": "user",
                    "content": _text_preview(prompt),
                })
                log_user_prompt_submission(prompt)
                context = update_context({}, messages)
                with cancellation_context(
                    self.registry.cancellation_event(run.run_id)
                ), web_permission_context(
                    self.approval_registry,
                    on_waiting=lambda approval: self._run_waiting_for_approval(
                        run.run_id, approval),
                    on_resolved=lambda approval: self._run_resolved_approval(
                        run.run_id, approval),
                ):
                    outcome = self.loop_factory().run(messages, context)
                if getattr(outcome, "status", None) == "failed":
                    raise AgentExecutionFailed(
                        getattr(outcome, "error", None)
                        or AgentExecutionFailed.message,
                        details={
                            "session_id": session_id,
                            "run_id": run.run_id,
                            "reason": getattr(outcome, "reason", "failed"),
                        },
                    )
                capture_assistant_messages()
                if not persist_transcript():
                    raise PersistenceFailed(details={
                        "session_id": session_id,
                        "run_id": run.run_id,
                    })
                completed = self.registry.complete(run.run_id)
                self._log_run_event("run_status", completed)
                self._log_run_event("run_completed", completed)
            except RunCancelled as exc:
                self.approval_registry.cancel_run(run.run_id)
                abandon_background_tasks()
                capture_assistant_messages()
                if not persist_transcript():
                    failed = self.registry.fail(
                        run.run_id,
                        PersistenceFailed.message,
                    )
                    self._log_run_event("run_status", failed)
                    self._log_run_event("run_failed", failed)
                    if raise_errors:
                        raise PersistenceFailed(details={
                            "session_id": session_id,
                            "run_id": run.run_id,
                        })
                    return None
                cancelled = self.registry.cancel(run.run_id, str(exc))
                self._log_run_event("run_status", cancelled)
                self._log_run_event("run_cancelled", cancelled)
            except Exception as exc:
                self.approval_registry.cancel_run(run.run_id)
                abandon_background_tasks()
                capture_assistant_messages()
                persistence_ok = False
                try:
                    persistence_ok = persist_transcript()
                except Exception:
                    persistence_ok = False
                error_text = scrub_sensitive_text(
                    f"{type(exc).__name__}: {exc}"
                )
                if not persistence_ok:
                    error_text = (
                        f"{error_text}; {PersistenceFailed.message}"
                    )
                failed = self.registry.fail(
                    run.run_id,
                    error_text,
                )
                self._log_run_event("run_status", failed)
                self._log_run_event("run_failed", failed)
                if raise_errors:
                    raise
                return None

        updated = self._load_snapshot(session_id)
        return {
            "run": run.to_dict(),
            "session": self._summary(updated),
            "messages": assistant_messages,
            "timeline": self.event_store.timeline(
                session_id=session_id,
                run_id=run.run_id,
                limit=100,
            )["items"],
        }

    def _normalise_prompt(self, content: str) -> str:
        prompt = content.strip()
        if not prompt:
            raise InvalidRequest("Message content must not be empty.")
        return prompt

    def _start_run(self, session_id: str) -> RunState:
        try:
            return self.registry.try_start(session_id)
        except SessionAlreadyRunning as exc:
            raise SessionRunning(details={"active_run_id": exc.active_run_id}) from exc

    def _run_waiting_for_approval(
        self,
        run_id: str,
        approval: PendingApproval,
    ) -> None:
        run = self.registry.wait_for_approval(run_id, approval.approval_id)
        if run is not None:
            self._log_run_event("run_status", run)

    def _run_resolved_approval(
        self,
        run_id: str,
        _approval: PendingApproval | None,
    ) -> None:
        run = self.registry.resume_after_approval(run_id)
        if run is not None:
            self._log_run_event("run_status", run)

    def _log_run_event(self, event_type: str, run: RunState) -> None:
        # Run lifecycle transitions can originate from the background agent
        # thread or an approval/cancel HTTP worker. Bind identity explicitly
        # instead of relying on whichever ContextVar happens to be active.
        with event_context(
            session_id=run.session_id,
            run_id=run.run_id,
            source="web",
            workspace=self.workspace,
        ):
            log_event(event_type, {
                "status": run.status,
                "run": run.to_dict(),
            })

    def _load_snapshot(self, session_id: str) -> dict[str, Any]:
        snapshot = load_session(session_id, self.workspace)
        if not snapshot:
            try:
                path = session_file_path(session_id, self.workspace)
            except ValueError:
                path = None
            if path is not None and path.exists():
                raise SessionCorrupt(details={"session_id": session_id})
            raise SessionNotFound(details={"session_id": session_id})
        return snapshot

    def _record_from_snapshot(
        self,
        session_id: str,
        snapshot: dict[str, Any],
    ) -> SessionRecord:
        if snapshot.get("session_id") != session_id:
            raise SessionCorrupt(details={"session_id": session_id})
        return SessionRecord(
            session_id=session_id,
            created_at=str(snapshot.get("created_at") or ""),
            workspace_path=str(self.workspace),
            path=session_file_path(session_id, self.workspace),
            title=(
                str(snapshot["title"])
                if snapshot.get("title") is not None
                else None
            ),
        )

    def _summary(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        session_id = str(snapshot.get("session_id") or "")
        status, active_run_id = self.registry.session_status(session_id)
        return {
            "session_id": session_id,
            "created_at": snapshot.get("created_at"),
            "updated_at": snapshot.get("updated_at"),
            "workspace_path": snapshot.get("workspace_path") or str(self.workspace),
            "title": snapshot.get("title"),
            "message_count": len(_display_messages(snapshot)),
            "last_user_prompt_preview": snapshot.get("last_user_prompt_preview"),
            "status": status,
            "active_run_id": active_run_id,
        }
