import json
import re
import subprocess
import time
from pathlib import Path

from ..config import WORKDIR
from ..runtime.execution import current_execution_context, execution_workspace
from ..runtime.fileio import safe_runtime_path
from .tasks import list_tasks, load_task, save_task, unbind_worktree

# ── Worktree System ──

# Worktree names become filesystem paths, so the teaching version keeps the
# validation rules strict and reuses them for create/remove/keep.
WORKTREES_DIR = WORKDIR / ".worktrees"
WORKTREES_DIR.mkdir(exist_ok=True)

VALID_WT_NAME = re.compile(r'^[A-Za-z0-9._-]{1,64}$')


def workspace_root() -> Path:
    return execution_workspace(WORKDIR)


def worktrees_dir(workspace: str | Path | None = None) -> Path:
    if workspace is not None:
        path = Path(workspace).resolve() / ".worktrees"
    elif current_execution_context().get("workspace"):
        path = workspace_root() / ".worktrees"
    else:
        configured = Path(WORKTREES_DIR)
        default = Path(WORKDIR) / ".worktrees"
        if configured == default:
            return safe_runtime_path(
                WORKDIR,
                ".worktrees",
                create_directory=True,
            )
        path = configured.resolve()
    return safe_runtime_path(
        path.parent,
        path.name,
        create_directory=True,
    )


def validate_worktree_name(name: str) -> str | None:
    if not name:
        return "Worktree name cannot be empty"
    if name in (".", ".."):
        return f"'{name}' is not a valid worktree name"
    if not VALID_WT_NAME.match(name):
        return (f"Invalid worktree name '{name}': "
                "only letters, digits, dots, underscores, dashes (1-64 chars)")
    return None


def run_git(args: list[str]) -> tuple[bool, str]:
    try:
        r = subprocess.run(["git"] + args, cwd=workspace_root(),
                           capture_output=True, text=True, timeout=30)
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, out[:5000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return False, "Error: git timeout"


def log_event(event_type: str, worktree_name: str, task_id: str = ""):
    event = {"type": event_type, "worktree": worktree_name,
             "task_id": task_id, "ts": time.time()}
    events_file = worktrees_dir() / "events.jsonl"
    with open(events_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def create_worktree(name: str, task_id: str = "") -> str:
    # Tool-layer validation is part of the safety boundary; do it before git
    # sees the name, not only after git happens to reject something.
    err = validate_worktree_name(name)
    if err:
        return f"Error: {err}"
    if task_id:
        try:
            load_task(task_id)
        except (FileNotFoundError, ValueError) as exc:
            return f"Error: task {task_id} not found ({exc})"
    path = worktrees_dir() / name
    if path.exists():
        return f"Worktree '{name}' already exists at {path}"
    ok, result = run_git(["worktree", "add", str(path), "-b", f"wt/{name}", "HEAD"])
    if not ok:
        return f"Git error: {result}"
    if task_id:
        bind_task_to_worktree(task_id, name)
    log_event("create", name, task_id)
    print(f"  \033[33m[worktree] created: {name} at {path}\033[0m")
    task_text = f" | task_id={task_id}" if task_id else ""
    return f"Worktree '{name}' created at {path} | branch=wt/{name}{task_text}"


def bind_task_to_worktree(task_id: str, worktree_name: str):
    task = load_task(task_id)
    task.worktree = worktree_name
    save_task(task)


def _worktree_branch(path: Path, name: str) -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           cwd=path, capture_output=True, text=True, timeout=5)
        branch = r.stdout.strip()
        if r.returncode == 0 and branch:
            return branch
    except Exception:
        pass
    return f"wt/{name}"


def list_worktree_statuses() -> list[dict]:
    task_by_worktree = {
        task.worktree: task.id for task in list_tasks() if task.worktree
    }
    base = worktrees_dir()
    statuses = []
    for path in sorted(base.iterdir()):
        if not path.is_dir():
            continue
        name = path.name
        statuses.append({
            "name": name,
            "path": str(path),
            "branch": _worktree_branch(path, name),
            "task_id": task_by_worktree.get(name, ""),
        })
    return statuses


def format_worktree_status(status: dict) -> str:
    task = status.get("task_id") or "none"
    return (
        f"{status.get('name')}: path={status.get('path')} "
        f"| branch={status.get('branch')} | task_id={task}"
    )


def _count_worktree_changes(path: Path) -> tuple[bool, int, int, str]:
    try:
        r1 = subprocess.run(["git", "status", "--porcelain"],
                            cwd=path, capture_output=True, text=True, timeout=10)
        if r1.returncode != 0:
            output = (r1.stdout + r1.stderr).strip()
            return False, -1, -1, output or "git status failed"
        files = len([l for l in r1.stdout.strip().splitlines() if l.strip()])
        r2 = subprocess.run(["git", "log", "@{push}..HEAD", "--oneline"],
                            cwd=path, capture_output=True, text=True, timeout=10)
        if r2.returncode != 0:
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=workspace_root(),
                capture_output=True,
                text=True,
                timeout=10,
            )
            base_commit = base.stdout.strip()
            if base.returncode != 0 or not base_commit:
                output = (r2.stdout + r2.stderr).strip()
                return (
                    False,
                    files,
                    -1,
                    output or "git log @{push}..HEAD failed",
                )
            r2 = subprocess.run(
                ["git", "log", f"{base_commit}..HEAD", "--oneline"],
                cwd=path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r2.returncode != 0:
                output = (r2.stdout + r2.stderr).strip()
                return False, files, -1, output or "git log from base failed"
        commits = len([l for l in r2.stdout.strip().splitlines() if l.strip()])
        return True, files, commits, ""
    except Exception as exc:
        return False, -1, -1, f"{type(exc).__name__}: {exc}"


def remove_worktree(name: str, discard_changes: bool = False) -> str:
    err = validate_worktree_name(name)
    if err:
        return err
    path = worktrees_dir() / name
    if not path.exists():
        return f"Worktree '{name}' not found"
    if not discard_changes:
        verified, files, commits, reason = _count_worktree_changes(path)
        if not verified:
            detail = f" ({reason[:200]})" if reason else ""
            return (
                "Cannot verify worktree status"
                f"{detail}. Use discard_changes=true to force."
            )
        if files > 0 or commits > 0:
            return (f"Worktree '{name}' has {files} file(s), {commits} commit(s). "
                    "Use discard_changes=true or keep_worktree.")
    ok1, _ = run_git(["worktree", "remove", str(path), "--force"])
    if not ok1:
        return f"Failed to remove worktree '{name}'"
    run_git(["branch", "-D", f"wt/{name}"])
    unbind_worktree(name)
    log_event("remove", name)
    print(f"  \033[33m[worktree] removed: {name}\033[0m")
    return f"Worktree '{name}' removed"


def keep_worktree(name: str) -> str:
    err = validate_worktree_name(name)
    if err:
        return err
    log_event("keep", name)
    path = worktrees_dir() / name
    return f"Worktree '{name}' kept for review | path={path} | branch=wt/{name}"

