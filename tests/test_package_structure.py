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
