import threading
import time

from .. import config
from ..config import PROMPT, terminal_print
from ..cron_scheduler import consume_cron_queue
from ..hooks import trigger_hooks
from ..memory.context import update_context
from ..message_utils import (
    internal_user_message,
    is_user_visible_message,
)
from ..teams import consume_lead_inbox
from .display import print_turn_assistants
from .events import log_user_prompt_submission
from .loop import agent_loop
from .session import create_session, save_session_snapshot


agent_lock = threading.Lock()


def _new_visible_assistants(
    history: list,
    existing_message_ids: set[int],
) -> list[dict]:
    return [
        message
        for message in history
        if (
            isinstance(message, dict)
            and id(message) not in existing_message_ids
            and message.get("role") == "assistant"
            and is_user_visible_message(message)
        )
    ]


def cron_autorun_loop(
    history: list,
    display_history: list,
    context: dict,
    session=None,
):
    while True:
        time.sleep(1)
        fired = consume_cron_queue()
        if not fired:
            continue
        with agent_lock:
            existing_message_ids = {
                id(message)
                for message in history
                if isinstance(message, dict)
            }
            for job in fired:
                history.append(internal_user_message(
                    f"[Scheduled] {job.prompt}"
                ))
                terminal_print(
                    f"  \033[35m[cron auto] {job.prompt[:60]}\033[0m")
            agent_loop(history, context)
            new_assistants = _new_visible_assistants(
                history,
                existing_message_ids,
            )
            display_history.extend(new_assistants)
            context.update(update_context(context, history))
            print_turn_assistants(new_assistants, 0)
            if session is not None:
                save_session_snapshot(
                    session,
                    history,
                    last_user_prompt=fired[-1].prompt,
                    display_messages=display_history,
                )


def run_cli():
    config.CLI_ACTIVE = True
    print("Coding Agent Harness")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []
    display_history = []
    context = update_context({}, [])
    session = create_session()
    threading.Thread(target=cron_autorun_loop,
                     args=(history, display_history, context, session),
                     daemon=True).start()
    while True:
        try:
            query = input(PROMPT).lstrip("\ufeff")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        log_user_prompt_submission(query)
        trigger_hooks("UserPromptSubmit", query)
        with agent_lock:
            existing_message_ids = {
                id(message)
                for message in history
                if isinstance(message, dict)
            }
            history.append({"role": "user", "content": query})
            display_history.append({"role": "user", "content": query})
            agent_loop(history, context)
            new_assistants = _new_visible_assistants(
                history,
                existing_message_ids,
            )
            display_history.extend(new_assistants)
            context = update_context(context, history)
            print_turn_assistants(new_assistants, 0)

        inbox = consume_lead_inbox(route_protocol=True)
        if inbox:
            def inbox_label(msg):
                req_id = msg.get("metadata", {}).get("request_id", "")
                suffix = f" req:{req_id}" if req_id else ""
                return f"{msg.get('type', 'message')}{suffix}"

            inbox_text = "\n".join(
                f"From {m['from']} [{inbox_label(m)}]: "
                f"{m['content'][:200]}" for m in inbox)
            history.append(internal_user_message(f"[Inbox]\n{inbox_text}"))
        save_session_snapshot(
            session,
            history,
            last_user_prompt=query,
            display_messages=display_history,
        )
        print()
