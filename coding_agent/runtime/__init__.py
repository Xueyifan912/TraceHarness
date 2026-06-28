"""Runtime building blocks for the coding agent harness."""

__all__ = ["AgentLoop", "agent_loop"]


def __getattr__(name: str):
    if name in __all__:
        from .loop import AgentLoop, agent_loop

        values = {
            "AgentLoop": AgentLoop,
            "agent_loop": agent_loop,
        }
        return values[name]
    raise AttributeError(name)
