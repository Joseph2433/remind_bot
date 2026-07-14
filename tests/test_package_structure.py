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
