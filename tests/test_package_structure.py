from importlib import import_module


def test_canonical_task_and_notification_modules_are_importable() -> None:
    detector = import_module("lark_bot.tasks.detector")
    runner = import_module("lark_bot.tasks.runner")
    notifier_base = import_module("lark_bot.notifications.base")
    codex_adapter = import_module("lark_bot.notifications.adapters.codex")

    assert callable(detector.detect_output)
    assert callable(runner.run_command)
    assert hasattr(notifier_base, "Notifier")
    assert hasattr(codex_adapter, "CodexEvent")


def test_codex_app_server_is_split_by_wire_and_lifecycle_responsibility() -> None:
    messages = import_module("lark_bot.codex.app_server.messages")
    responses = import_module("lark_bot.codex.app_server.responses")
    client = import_module("lark_bot.codex.app_server.client")

    for name in (
        "ServerRequest",
        "ServerNotification",
        "_Reader",
        "_Writer",
        "_Process",
    ):
        assert hasattr(messages, name)

    response_builders = (
        "command_approval_response",
        "file_approval_response",
        "permission_response",
        "user_input_response",
    )
    for name in response_builders:
        assert callable(getattr(responses, name))
        assert not hasattr(client, name)

    for name in (
        "ProtocolError",
        "ServerRpcError",
        "ProcessExitedError",
        "ProcessFactory",
        "_Lifecycle",
        "CodexAppServerClient",
        "_default_process_factory",
    ):
        assert hasattr(client, name)


def test_codex_runtime_modules_live_under_codex_package() -> None:
    modules = {
        name: import_module(f"lark_bot.codex.{name}")
        for name in (
            "models",
            "gateway",
            "interactive",
            "tui",
            "hooks",
            "hook_adapter",
            "probe",
        )
    }

    assert hasattr(modules["models"], "CodexSession")
    assert hasattr(modules["gateway"], "CodexGateway")
    assert hasattr(modules["interactive"], "InteractiveSessionManager")
    assert hasattr(modules["tui"], "CodexTuiLauncher")
    assert hasattr(modules["hooks"], "build_notify_override")
    assert hasattr(modules["hook_adapter"], "handle_callback")
    assert hasattr(modules["probe"], "run_local_probe")


def test_lark_modules_separate_client_routing_and_connection() -> None:
    assert import_module("lark_bot.lark.client").LarkBotClient
    assert import_module("lark_bot.lark.events").LarkMessageEvent
    assert import_module("lark_bot.lark.router").LarkControlRouter
    assert import_module("lark_bot.lark.connection").LarkLongConnection
