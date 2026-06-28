"""In-process run state and per-session locking for the Web backend."""

from __future__ import annotations

import json
import re
import threading
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ..runtime.fileio import atomic_write_text, safe_runtime_path


MAX_TERMINAL_RUNS = 500
RUN_STATE_DIR = ".agent_runs"
INTERRUPTED_RUN_ERROR = "Run interrupted by backend restart."
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
_SAFE_STATE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"run_{stamp}_{uuid4().hex[:6]}"


@dataclass
class RunState:
    run_id: str
    session_id: str
    status: str
    started_at: str
    ended_at: str | None = None
    error: str | None = None
    pending_approval_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class SessionAlreadyRunning(Exception):
    def __init__(self, active_run_id: str | None):
        super().__init__("Session is already running")
        self.active_run_id = active_run_id


class SessionRunRegistry:
    def __init__(self, workspace: str | Path | None = None):
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._runs: dict[str, RunState] = {}
        self._active_runs: dict[str, str] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._terminal_run_ids: deque[str] = deque()
        self._interrupted_sessions: dict[str, str] = {}
        self._recovered_interrupts: list[RunState] = []
        self._restore_warnings: list[str] = []
        self.workspace = (
            Path(workspace).resolve()
            if workspace is not None
            else None
        )
        self._restore_runs()

    def try_start(self, session_id: str) -> RunState:
        with self._guard:
            lock = self._locks.setdefault(session_id, threading.Lock())
            if not lock.acquire(blocking=False):
                raise SessionAlreadyRunning(self._active_runs.get(session_id))

            run = RunState(
                run_id=new_run_id(),
                session_id=session_id,
                status="running",
                started_at=utc_timestamp(),
            )
            self._runs[run.run_id] = run
            self._active_runs[session_id] = run.run_id
            self._cancel_events[run.run_id] = threading.Event()
            self._interrupted_sessions.pop(session_id, None)
            self._persist_run_locked(run)
            return run

    def cancellation_event(self, run_id: str) -> threading.Event:
        with self._guard:
            return self._cancel_events[run_id]

    def request_cancel(self, session_id: str, run_id: str) -> RunState | None:
        with self._guard:
            run = self._runs.get(run_id)
            if run is None or run.session_id != session_id:
                return None
            if run.status not in TERMINAL_STATUSES:
                event = self._cancel_events.get(run_id)
                if event is not None:
                    event.set()
                run.status = "cancelling"
                self._persist_run_locked(run)
            return run

    def complete(self, run_id: str) -> RunState:
        return self._finish(run_id, "completed")

    def fail(self, run_id: str, error: str) -> RunState:
        return self._finish(run_id, "failed", error=error)

    def cancel(self, run_id: str, error: str | None = None) -> RunState:
        return self._finish(run_id, "cancelled", error=error)

    def wait_for_approval(self, run_id: str, approval_id: str) -> RunState | None:
        with self._guard:
            run = self._runs.get(run_id)
            if not run or run.status in TERMINAL_STATUSES:
                return None
            run.status = "waiting_approval"
            run.pending_approval_id = approval_id
            self._persist_run_locked(run)
            return run

    def resume_after_approval(self, run_id: str) -> RunState | None:
        with self._guard:
            run = self._runs.get(run_id)
            if not run or run.status in TERMINAL_STATUSES:
                return None
            if run.status != "waiting_approval":
                return None
            run.status = "running"
            run.pending_approval_id = None
            self._persist_run_locked(run)
            return run

    def session_status(self, session_id: str) -> tuple[str, str | None]:
        with self._guard:
            active_run_id = self._active_runs.get(session_id)
            if not active_run_id:
                if session_id in self._interrupted_sessions:
                    return "failed", None
                return "idle", None
            run = self._runs.get(active_run_id)
            return (run.status if run else "running"), active_run_id

    def get_run(self, run_id: str) -> RunState | None:
        with self._guard:
            return self._runs.get(run_id)

    def get_session_run(self, session_id: str, run_id: str) -> RunState | None:
        with self._guard:
            run = self._runs.get(run_id)
            if run is None or run.session_id != session_id:
                return None
            return run

    def take_recovered_interrupts(self) -> list[RunState]:
        with self._guard:
            recovered = list(self._recovered_interrupts)
            self._recovered_interrupts.clear()
            return recovered

    def restore_warnings(self) -> list[str]:
        with self._guard:
            return list(self._restore_warnings)

    def forget_session(self, session_id: str) -> None:
        with self._guard:
            if session_id in self._active_runs:
                raise SessionAlreadyRunning(self._active_runs.get(session_id))
            removed = {
                run_id
                for run_id, run in self._runs.items()
                if run.session_id == session_id
            }
            for run_id in removed:
                self._runs.pop(run_id, None)
                self._cancel_events.pop(run_id, None)
                self._delete_run_file_locked(run_id)
            self._terminal_run_ids = deque(
                run_id
                for run_id in self._terminal_run_ids
                if run_id not in removed
            )
            self._locks.pop(session_id, None)
            self._interrupted_sessions.pop(session_id, None)

    def _finish(self, run_id: str, status: str,
                error: str | None = None) -> RunState:
        with self._guard:
            run = self._runs[run_id]
            if run.status in TERMINAL_STATUSES:
                return run
            run.status = status
            run.ended_at = utc_timestamp()
            run.error = error
            run.pending_approval_id = None
            self._persist_run_locked(run)
            active_run_id = self._active_runs.pop(run.session_id, None)
            release_lock = self._locks.get(run.session_id)
            if release_lock and active_run_id == run_id and release_lock.locked():
                release_lock.release()
            self._locks.pop(run.session_id, None)
            self._terminal_run_ids.append(run_id)
            while len(self._terminal_run_ids) > MAX_TERMINAL_RUNS:
                expired_run_id = self._terminal_run_ids.popleft()
                self._runs.pop(expired_run_id, None)
                self._cancel_events.pop(expired_run_id, None)
                self._delete_run_file_locked(expired_run_id)
            return run

    def _run_state_dir(self) -> Path | None:
        if self.workspace is None:
            return None
        return safe_runtime_path(
            self.workspace,
            RUN_STATE_DIR,
            create_directory=True,
        )

    def _run_file(self, run_id: str) -> Path | None:
        directory = self._run_state_dir()
        if directory is None:
            return None
        return safe_runtime_path(directory, f"{run_id}.json")

    def _persist_run_locked(self, run: RunState) -> None:
        path = self._run_file(run.run_id)
        if path is None:
            return
        try:
            atomic_write_text(
                path,
                json.dumps(
                    run.to_dict(),
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        except Exception:
            # Runtime state remains authoritative in-process. A later
            # transition retries persistence.
            return

    def _delete_run_file_locked(self, run_id: str) -> None:
        path = self._run_file(run_id)
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return

    def _restore_runs(self) -> None:
        directory = self._run_state_dir()
        if directory is None:
            return
        try:
            paths = sorted(
                directory.glob("run_*.json"),
                key=lambda path: path.stat().st_mtime,
            )[-MAX_TERMINAL_RUNS:]
        except Exception as exc:
            self._restore_warnings.append(
                f"Run state directory could not be read: "
                f"{type(exc).__name__}"
            )
            return

        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                run_id = str(data["run_id"])
                session_id = str(data["session_id"])
                if run_id != path.stem or not _SAFE_STATE_ID.fullmatch(run_id):
                    raise ValueError("run id does not match its file name")
                if not session_id or not _SAFE_STATE_ID.fullmatch(session_id):
                    raise ValueError("invalid session id")
                run = RunState(
                    run_id=run_id,
                    session_id=session_id,
                    status=str(data["status"]),
                    started_at=str(data["started_at"]),
                    ended_at=(
                        str(data["ended_at"])
                        if data.get("ended_at")
                        else None
                    ),
                    error=(
                        str(data["error"])
                        if data.get("error")
                        else None
                    ),
                    pending_approval_id=(
                        str(data["pending_approval_id"])
                        if data.get("pending_approval_id")
                        else None
                    ),
                )
            except Exception as exc:
                self._restore_warnings.append(
                    f"Unreadable run state {path.name}: "
                    f"{type(exc).__name__}"
                )
                continue

            if run.status not in TERMINAL_STATUSES:
                run.status = "failed"
                run.ended_at = utc_timestamp()
                run.error = INTERRUPTED_RUN_ERROR
                run.pending_approval_id = None
                self._interrupted_sessions[run.session_id] = run.run_id
                self._recovered_interrupts.append(run)
                self._persist_run_locked(run)

            self._runs[run.run_id] = run
            self._terminal_run_ids.append(run.run_id)
