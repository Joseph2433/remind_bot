from lark_bot.modules.codex.orchestration.orchestration_event import (
    OrchestratorEvent,
    OrchestratorEventType,
)

__all__ = ["CodexOrchestrator", "OrchestratorEvent", "OrchestratorEventType"]


def __getattr__(name: str) -> object:
    if name == "CodexOrchestrator":
        from lark_bot.modules.codex.codex_orchestrator import CodexOrchestrator

        return CodexOrchestrator
    raise AttributeError(name)
