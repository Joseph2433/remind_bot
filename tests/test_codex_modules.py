from importlib import import_module


def test_codex_is_a_provider_module() -> None:
    assert import_module("lark_bot.modules.codex.codex_model").CodexSession
    assert import_module("lark_bot.modules.codex.codex_adapter").CodexEvent
    assert import_module("lark_bot.modules.codex.codex_orchestrator").CodexOrchestrator
    assert import_module(
        "lark_bot.modules.codex.app_server.app_server_client"
    ).CodexAppServerClient
    assert import_module(
        "lark_bot.modules.codex.orchestration.orchestration_event"
    ).OrchestratorEvent
