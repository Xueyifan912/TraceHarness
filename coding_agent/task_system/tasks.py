import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from ..config import WORKDIR
from ..runtime.execution import current_execution_context, execution_workspace
from ..runtime.events import log_event
from ..runtime.fileio import safe_runtime_path

# Tasks are tiny durable records. Later systems add ownership, dependencies,
# worktrees, and teammates on top of this same file-backed state.
TASKS_DIR = WORKDIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)
VALID_TASK_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_TASK_PROCESS_LOCK = threading.RLock()

@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    blockedBy: list[str]
    worktree: str | None = None


def tasks_dir(workspace: str | Path | None = None) -> Path:
    if workspace is not None:
        path = Path(workspace).resolve() / ".tasks"
    elif current_execution_context().get("workspace"):
        path = execution_workspace(WORKDIR) / ".tasks"
    else:
        configured = Path(TASKS_DIR)
        default = Path(WORKDIR) / ".tasks"
        if configured == default:
            return safe_runtime_path(
                WORKDIR,
                ".tasks",
                create_directory=True,
            )
        path = configured.resolve()
    return safe_runtime_path(
        path.parent,
        path.name,
        create_directory=True,
    )


def _task_path(task_id: str) -> Path:
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("Invalid task_id: empty")
    if task_id in (".", "..") or not VALID_TASK_ID.fullmatch(task_id):
        raise ValueError(f"Invalid task_id: {task_id!r}")
    base = tasks_dir().resolve()
    path = (base / f"{task_id}.json").resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"Task path escapes task directory: {task_id!r}")
    return path


@contextmanager
def _task_store_lock():
    lock_path = tasks_dir() / ".lock"
    with _TASK_PROCESS_LOCK:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_task_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _save_task_unlocked(task: Task) -> None:
    _write_task_atomic(_task_path(task.id), asdict(task))


def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    task = Task(
        id=f"task_{int(time.time())}_{uuid4().hex[:12]}",
        subject=subject, description=description,
        status="pending", owner=None,
        blockedBy=blockedBy or [],
    )
    with _task_store_lock():
        _save_task_unlocked(task)
    return task


def save_task(task: Task):
    with _task_store_lock():
        _save_task_unlocked(task)


def load_task(task_id: str) -> Task:
    path = _task_path(task_id)
    try:
        return Task(**json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        raise
    except Exception as exc:
        log_event("task_file_corrupt", {
            "task_id": task_id,
            "file": path.name,
            "error_type": type(exc).__name__,
        }, workspace=execution_workspace(WORKDIR))
        raise ValueError(f"Corrupt task file: {task_id}") from exc


def list_tasks() -> list[Task]:
    tasks: list[Task] = []
    for path in sorted(tasks_dir().glob("task_*.json")):
        try:
            tasks.append(
                Task(**json.loads(path.read_text(encoding="utf-8")))
            )
        except Exception as exc:
            log_event("task_file_corrupt", {
                "task_id": path.stem,
                "file": path.name,
                "error_type": type(exc).__name__,
            }, workspace=execution_workspace(WORKDIR))
    return tasks


def format_task_line(task: Task) -> str:
    owner = task.owner or "unassigned"
    worktree = task.worktree or "none"
    blockers = ", ".join(task.blockedBy) if task.blockedBy else "none"
    return (
        f"{task.id}: {task.subject} | status={task.status} "
        f"| owner={owner} | worktree={worktree} | blockedBy={blockers}"
    )


def get_task_json(task_id: str) -> str:
    return json.dumps(asdict(load_task(task_id)), indent=2)


def can_start(task_id: str) -> bool:
    # Dependencies are intentionally simple: every blocker must exist and be
    # completed before the task can be claimed.
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        try:
            dep_path = _task_path(dep_id)
        except ValueError:
            return False
        if not dep_path.exists():
            return False
        try:
            dependency = load_task(dep_id)
        except Exception:
            return False
        if dependency.status != "completed":
            return False
    return True


def claim_task(task_id: str, owner: str = "agent") -> str:
    with _task_store_lock():
        task = load_task(task_id)
        if task.status != "pending":
            return f"Task {task_id} is {task.status}, cannot claim"
        if task.owner:
            return f"Task {task_id} already owned by {task.owner}"
        if not can_start(task_id):
            deps = []
            missing = []
            for dep_id in task.blockedBy:
                try:
                    dep_path = _task_path(dep_id)
                except ValueError:
                    missing.append(dep_id)
                    continue
                if dep_path.exists() and load_task(dep_id).status != "completed":
                    deps.append(dep_id)
                elif not dep_path.exists():
                    missing.append(dep_id)
            parts = []
            if deps: parts.append(f"blocked by: {deps}")
            if missing: parts.append(f"missing deps: {missing}")
            return "Cannot start - " + ", ".join(parts)
        task.owner = owner
        task.status = "in_progress"
        _save_task_unlocked(task)
    print(f"  \033[36m[claim] {task.subject} -> in_progress\033[0m")
    worktree = f" | worktree={task.worktree}" if task.worktree else ""
    return f"Claimed {task.id} ({task.subject}) | owner={owner}{worktree}"


def complete_task(task_id: str, owner: str | None = None) -> str:
    with _task_store_lock():
        task = load_task(task_id)
        if task.status != "in_progress":
            return f"Task {task_id} is {task.status}, cannot complete"
        if owner is not None and task.owner != owner:
            return (
                f"Task {task_id} is owned by {task.owner or 'nobody'}, "
                f"cannot complete as {owner}"
            )
        task.status = "completed"
        _save_task_unlocked(task)
    unblocked = [t.subject for t in list_tasks()
                 if t.status == "pending" and t.blockedBy and can_start(t.id)]
    print(f"  \033[32m[complete] {task.subject} done\033[0m")
    worktree = f" | worktree={task.worktree}" if task.worktree else ""
    msg = f"Completed {task.id} ({task.subject}) | owner={task.owner}{worktree}"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
    return msg


def unbind_worktree(worktree_name: str) -> list[str]:
    updated: list[str] = []
    with _task_store_lock():
        for path in sorted(tasks_dir().glob("task_*.json")):
            try:
                task = Task(**json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
            if task.worktree != worktree_name:
                continue
            task.worktree = None
            _save_task_unlocked(task)
            updated.append(task.id)
    return updated
