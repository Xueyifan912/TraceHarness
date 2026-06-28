import ast
import json
import locale
import os
import signal
import subprocess
import time
from pathlib import Path

from ..config import WORKDIR
from ..runtime.cancellation import cancellation_requested
from ..runtime.execution import execution_workspace
from ..runtime.events import current_event_context
from ..runtime.fileio import (
    atomic_write_text,
    exclusive_file_lock,
    safe_runtime_path,
)

BASH_TIMEOUT_SECONDS = 120.0
TODO_DIR_NAME = ".agent_todos"

# ── Basic Tools ──

def _tool_base(cwd: Path = None) -> Path:
    if cwd is not None:
        return Path(cwd).resolve()
    return execution_workspace(WORKDIR)


def safe_path(p: str, cwd: Path = None) -> Path:
    # File tools stay inside the workspace or teammate worktree. Bash remains
    # powerful on purpose and is controlled by the permission hook instead.
    base = _tool_base(cwd)
    path = (base / p).resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def _decode_process_output(data: bytes) -> str:
    if not data:
        return ""
    encodings = ["utf-8-sig", locale.getpreferredencoding(False)]
    if os.name == "nt":
        encodings.append("mbcs")
    tried = set()
    for encoding in encodings:
        key = encoding.casefold()
        if key in tried:
            continue
        tried.add(key)
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
                check=False,
            )
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except Exception:
        try:
            process.terminate()
        except Exception:
            pass
    try:
        process.wait(timeout=2)
    except Exception:
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            pass


def run_bash(command: str, cwd: Path = None,
             run_in_background: bool = False) -> str:
    # run_in_background is consumed by the dispatcher; direct execution ignores it.
    process: subprocess.Popen | None = None
    try:
        kwargs = {
            "shell": True,
            "cwd": _tool_base(cwd),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)
        deadline = time.monotonic() + BASH_TIMEOUT_SECONDS
        while True:
            if cancellation_requested():
                _terminate_process_tree(process)
                stdout, stderr = process.communicate()
                output = _decode_process_output(stdout + stderr).strip()
                return f"Error: Cancelled{f': {output}' if output else ''}"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process_tree(process)
                process.communicate()
                return f"Error: Timeout ({BASH_TIMEOUT_SECONDS:g}s)"
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        out = _decode_process_output(stdout + stderr).strip()
        return out[:50000] if out else "(no output)"
    except Exception as exc:
        if process is not None:
            _terminate_process_tree(process)
        return f"Error: {type(exc).__name__}: {exc}"


def run_read(path: str, limit: int | None = None,
             offset: int = 0, cwd: Path = None) -> str:
    try:
        lines = safe_path(path, cwd).read_text(encoding="utf-8").splitlines()
        offset = max(int(offset or 0), 0)
        limit = int(limit) if limit is not None else None
        lines = lines[offset:]
        if limit is not None and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str, cwd: Path = None) -> str:
    try:
        fp = safe_path(path, cwd)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str,
             cwd: Path = None) -> str:
    try:
        fp = safe_path(path, cwd)
        text = fp.read_text(encoding="utf-8")
        if old_text not in text:
            return f"Error: text not found in {path}"
        fp.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str, cwd: Path = None) -> str:
    import glob as g
    try:
        base = _tool_base(cwd)
        results = []
        for match in g.glob(pattern, root_dir=base):
            if (base / match).resolve().is_relative_to(base):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


def call_tool_handler(handler, args: dict, name: str) -> str:
    if not handler:
        return f"Unknown: {name}"
    try:
        return handler(**(args or {}))
    except TypeError as e:
        return f"Error: {e}"


def _normalize_todos(todos):
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    for i, todo in enumerate(todos):
        if not isinstance(todo, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in todo or "status" not in todo:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if todo["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{todo['status']}'"
    return todos, None


def _todo_scope() -> tuple[Path, str]:
    workspace = execution_workspace(WORKDIR)
    context = current_event_context()
    session_id = str(context.get("session_id") or "cli")
    safe_session = "".join(
        character
        if character.isalnum() or character in "._-"
        else "_"
        for character in session_id
    ).strip("._-") or "cli"
    directory = safe_runtime_path(
        workspace,
        TODO_DIR_NAME,
        create_directory=True,
    )
    return directory / f"{safe_session}.json", session_id


def run_todo_write(todos: list) -> str:
    todos, error = _normalize_todos(todos)
    if error:
        return error
    path, session_id = _todo_scope()
    try:
        with exclusive_file_lock(path.with_name(f".{path.name}.lock")):
            atomic_write_text(
                path,
                json.dumps(
                    {
                        "session_id": session_id,
                        "todos": todos,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
    except Exception as exc:
        return f"Error: could not persist todos: {exc}"
    print(f"  \033[33m[todo] updated {len(todos)} item(s)\033[0m")
    return f"Updated {len(todos)} todos"


def run_todo_read() -> str:
    path, session_id = _todo_scope()
    try:
        if not path.exists():
            return "No todos."
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("session_id") != session_id:
            return "Error: todo state does not match the current session"
        todos, error = _normalize_todos(payload.get("todos"))
        if error:
            return error
        if not todos:
            return "No todos."
        return json.dumps(todos, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Error: could not read todos: {exc}"

