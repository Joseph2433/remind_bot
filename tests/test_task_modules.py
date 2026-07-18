from importlib import import_module


def test_task_module_owns_detection_and_execution() -> None:
    task = import_module("lark_bot.modules.task")
    assert task.detect_output
    assert task.run_command
