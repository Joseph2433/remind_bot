from importlib import import_module
from pathlib import Path


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
    assert import_module("lark_bot.lark.messages").RenderedMessage
    assert callable(import_module("lark_bot.lark.render").render_task_notification)


def test_codex_storage_package_exports_store_and_schema_tables() -> None:
    module = import_module("lark_bot.storage.codex")
    store_type = module.SQLiteCodexStore
    assert store_type.__module__ == "lark_bot.storage.codex.store"

    with store_type(":memory:") as store:
        with store._connection() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
    assert tables >= {
        "codex_sessions",
        "codex_interactions",
        "codex_event_dedupe",
        "notification_outbox",
        "codex_audit",
    }


def test_codex_orchestrator_has_focused_modules() -> None:
    assert import_module("lark_bot.codex.orchestration.service").CodexOrchestrator
    assert import_module("lark_bot.codex.orchestration.events").OrchestratorEventType
    assert import_module("lark_bot.codex.orchestration.interactions").resolution
    assert import_module("lark_bot.codex.orchestration.summaries").request_summary


def test_daemon_separates_auth_runtime_and_routes() -> None:
    assert import_module("lark_bot.server.daemon.auth").ensure_daemon_token
    assert import_module("lark_bot.server.daemon.runtime").DaemonRuntime
    assert import_module("lark_bot.server.daemon.app").create_daemon_app


def test_cli_is_a_thin_stable_composition_root() -> None:
    cli = import_module("lark_bot.cli")
    assert cli.app
    assert import_module("lark_bot.commands.app").app is cli.app
    assert import_module("lark_bot.commands.codex_args").uses_remote_resume_picker
    assert import_module("lark_bot.commands.common").build_codex_notification_from_json


def test_package_root_contains_only_entrypoints_and_shared_contracts() -> None:
    root = Path(__file__).parents[1] / "src" / "lark_bot"
    actual = {path.name for path in root.glob("*.py")}
    assert actual == {
        "__init__.py",
        "__main__.py",
        "cli.py",
        "config.py",
        "models.py",
        "redaction.py",
    }
