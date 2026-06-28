import json
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .config import WORKDIR
from .runtime.execution import current_execution_context, execution_workspace
from .runtime.fileio import (
    atomic_write_text,
    exclusive_file_lock,
    safe_runtime_path,
)

# ── Cron Scheduler ──

# Cron jobs are stored separately from conversation history. When a job fires,
# it becomes a scheduled prompt that is injected back into the same agent loop.
DURABLE_PATH = WORKDIR / ".scheduled_tasks.json"
_DEFAULT_DURABLE_PATH = DURABLE_PATH


@dataclass
class CronJob:
    id: str
    cron: str
    prompt: str
    recurring: bool
    durable: bool
    session_id: str | None = None


scheduled_jobs: dict[str, CronJob] = {}
cron_queue: list[CronJob] = []
cron_lock = threading.RLock()
_last_fired: dict[str, str] = {}
_DEFAULT_WORKSPACE_KEY = str(WORKDIR.resolve())
_scheduled_jobs_by_workspace: dict[str, dict[str, CronJob]] = {
    _DEFAULT_WORKSPACE_KEY: scheduled_jobs,
}
_cron_queue_by_workspace: dict[str, list[CronJob]] = {
    _DEFAULT_WORKSPACE_KEY: cron_queue,
}
_last_fired_by_workspace: dict[str, dict[str, str]] = {
    _DEFAULT_WORKSPACE_KEY: _last_fired,
}
_loaded_workspaces: set[str] = set()


def _workspace(workspace: str | Path | None = None) -> Path:
    if workspace is not None:
        return Path(workspace).resolve()
    return execution_workspace(WORKDIR)


def durable_path(workspace: str | Path | None = None) -> Path:
    root = _workspace(workspace)
    if root == WORKDIR.resolve() and DURABLE_PATH is not None:
        if Path(DURABLE_PATH) != Path(_DEFAULT_DURABLE_PATH):
            return Path(DURABLE_PATH).resolve()
        configured = Path(DURABLE_PATH).resolve()
        if not configured.is_relative_to(root):
            raise ValueError("Durable cron path escapes workspace")
        return configured
    return safe_runtime_path(root, ".scheduled_tasks.json")


def _workspace_state(
    workspace: str | Path | None = None,
) -> tuple[Path, dict[str, CronJob], list[CronJob], dict[str, str]]:
    root = _workspace(workspace)
    key = str(root)
    with cron_lock:
        jobs = _scheduled_jobs_by_workspace.setdefault(key, {})
        queue = _cron_queue_by_workspace.setdefault(key, [])
        fired = _last_fired_by_workspace.setdefault(key, {})
        if key not in _loaded_workspaces:
            _load_durable_jobs(root, jobs)
            _loaded_workspaces.add(key)
    return root, jobs, queue, fired


def _cron_field_matches(field: str, value: int) -> bool:
    if field == "*":
        return True
    if field.startswith("*/"):
        step = int(field[2:])
        return step > 0 and value % step == 0
    if "," in field:
        return any(_cron_field_matches(part.strip(), value)
                   for part in field.split(","))
    if "-" in field:
        lo, hi = field.split("-", 1)
        return int(lo) <= value <= int(hi)
    return value == int(field)


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    dow_val = (dt.weekday() + 1) % 7
    m = _cron_field_matches(minute, dt.minute)
    h = _cron_field_matches(hour, dt.hour)
    dom_ok = _cron_field_matches(dom, dt.day)
    month_ok = _cron_field_matches(month, dt.month)
    dow_ok = _cron_field_matches(dow, dow_val)
    if not (m and h and month_ok):
        return False
    if dom == "*" and dow == "*":
        return True
    if dom == "*":
        return dow_ok
    if dow == "*":
        return dom_ok
    return dom_ok or dow_ok


def _validate_cron_field(field: str, lo: int, hi: int) -> str | None:
    if field == "*":
        return None
    if field.startswith("*/"):
        step = field[2:]
        if not step.isdigit() or int(step) <= 0:
            return f"Invalid step: {field}"
        return None
    if "," in field:
        for part in field.split(","):
            err = _validate_cron_field(part.strip(), lo, hi)
            if err:
                return err
        return None
    if "-" in field:
        left, right = field.split("-", 1)
        if not left.isdigit() or not right.isdigit():
            return f"Invalid range: {field}"
        a, b = int(left), int(right)
        if a < lo or a > hi or b < lo or b > hi:
            return f"Range {field} out of bounds [{lo}-{hi}]"
        if a > b:
            return f"Range start > end: {field}"
        return None
    if not field.isdigit():
        return f"Invalid field: {field}"
    value = int(field)
    if value < lo or value > hi:
        return f"Value {value} out of bounds [{lo}-{hi}]"
    return None


def validate_cron(cron_expr: str) -> str | None:
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
    for field, (lo, hi), name in zip(fields, bounds, names):
        err = _validate_cron_field(field, lo, hi)
        if err:
            return f"{name}: {err}"
    return None


def save_durable_jobs(workspace: str | Path | None = None):
    root, jobs, _, _ = _workspace_state(workspace)
    with cron_lock:
        durable = [asdict(job) for job in jobs.values() if job.durable]
    path = durable_path(root)
    with exclusive_file_lock(path.with_name(f".{path.name}.lock")):
        atomic_write_text(
            path,
            json.dumps(durable, ensure_ascii=False, indent=2),
        )


def _load_durable_jobs(root: Path, jobs: dict[str, CronJob]):
    path = durable_path(root)
    if not path.exists():
        return
    try:
        for item in json.loads(path.read_text(encoding="utf-8")):
            job = CronJob(**item)
            if not validate_cron(job.cron):
                jobs[job.id] = job
    except Exception:
        pass


def load_durable_jobs(workspace: str | Path | None = None):
    root = _workspace(workspace)
    key = str(root)
    with cron_lock:
        jobs = _scheduled_jobs_by_workspace.setdefault(key, {})
        _cron_queue_by_workspace.setdefault(key, [])
        _last_fired_by_workspace.setdefault(key, {})
        _load_durable_jobs(root, jobs)
        _loaded_workspaces.add(key)


def schedule_job(cron: str, prompt: str,
                 recurring: bool = True, durable: bool = True) -> CronJob | str:
    err = validate_cron(cron)
    if err:
        return err
    job = CronJob(
        id=f"cron_{uuid4().hex[:12]}",
        cron=cron, prompt=prompt,
        recurring=recurring, durable=durable,
        session_id=current_execution_context().get("session_id"))
    root, jobs, _, _ = _workspace_state()
    with cron_lock:
        jobs[job.id] = job
    if durable:
        save_durable_jobs(root)
    return job


def cancel_job(job_id: str) -> str:
    root, jobs, _, _ = _workspace_state()
    with cron_lock:
        job = jobs.pop(job_id, None)
    if not job:
        return f"Job {job_id} not found"
    if job.durable:
        save_durable_jobs(root)
    return f"Cancelled {job_id}"


def cron_scheduler_loop():
    while True:
        time.sleep(1)
        now = datetime.now()
        marker = now.strftime("%Y-%m-%d %H:%M")
        with cron_lock:
            states = [
                (Path(key), jobs,
                 _cron_queue_by_workspace.setdefault(key, []),
                 _last_fired_by_workspace.setdefault(key, {}))
                for key, jobs in list(_scheduled_jobs_by_workspace.items())
            ]
            for root, jobs, queue, last_fired in states:
                for job in list(jobs.values()):
                    try:
                        if (cron_matches(job.cron, now)
                                and last_fired.get(job.id) != marker):
                            queue.append(job)
                            last_fired[job.id] = marker
                            if not job.recurring:
                                jobs.pop(job.id, None)
                                if job.durable:
                                    save_durable_jobs(root)
                    except Exception as e:
                        print(f"  \033[31m[cron error] {job.id}: {e}\033[0m")


def consume_cron_queue() -> list[CronJob]:
    _, _, queue, _ = _workspace_state()
    active_session_id = current_execution_context().get("session_id")
    with cron_lock:
        fired = [
            job for job in queue
            if job.session_id == active_session_id
        ]
        queue[:] = [
            job for job in queue
            if job.session_id != active_session_id
        ]
    return fired


def run_schedule_cron(cron: str, prompt: str,
                      recurring: bool = True, durable: bool = True) -> str:
    result = schedule_job(cron, prompt, recurring, durable)
    if isinstance(result, str):
        return f"Error: {result}"
    return f"Scheduled {result.id}: '{cron}' -> {prompt}"


def run_list_crons() -> str:
    _, jobs_by_id, _, _ = _workspace_state()
    with cron_lock:
        jobs = list(jobs_by_id.values())
    if not jobs:
        return "No cron jobs."
    return "\n".join(
        f"  {job.id}: '{job.cron}' -> {job.prompt[:40]} "
        f"[{'recurring' if job.recurring else 'one-shot'}, "
        f"{'durable' if job.durable else 'session'}]"
        for job in jobs)


def run_cancel_cron(job_id: str) -> str:
    return cancel_job(job_id)


load_durable_jobs()
threading.Thread(target=cron_scheduler_loop, daemon=True).start()

