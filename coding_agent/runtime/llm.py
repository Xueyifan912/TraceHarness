import time
from uuid import uuid4

from ..config import REQUEST_TIMEOUT_SECONDS
from ..memory.context import assemble_system_prompt
from ..providers.router import get_model_provider
from ..recovery import with_retry
from .events import (
    log_llm_call_ended,
    log_llm_call_failed,
    log_llm_call_started,
)


def call_llm(messages: list, context: dict, tools: list,
             state, max_tokens: int):
    system = assemble_system_prompt(context)
    provider = get_model_provider()
    print(
        f"  \033[90m[llm] calling {provider.name}:{state.current_model} "
        f"({len(messages)} message(s), {len(tools)} tool(s), "
        f"timeout={REQUEST_TIMEOUT_SECONDS:g}s)\033[0m",
        flush=True,
    )
    started = time.time()
    call_id = f"llm_{uuid4().hex}"
    log_llm_call_started(
        state.current_model, len(messages), len(tools),
        max_tokens, REQUEST_TIMEOUT_SECONDS, provider.name, call_id)
    try:
        response = with_retry(
            lambda: provider.complete(
                model=state.current_model,
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens,
                timeout=REQUEST_TIMEOUT_SECONDS),
            state)
    except Exception as e:
        log_llm_call_failed(
            e, time.time() - started, state.current_model, provider.name,
            call_id)
        raise
    elapsed = time.time() - started
    log_llm_call_ended(
        response, elapsed, state.current_model, provider.name, call_id)
    print(f"  \033[90m[llm] response in {elapsed:.1f}s\033[0m")
    return response
