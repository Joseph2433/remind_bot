from importlib import import_module

from lark_bot.modules.agent.agent_model import AgentKind
from lark_bot.modules.task.task_model import TaskStatus


def test_notification_module_owns_request_and_sender_contract() -> None:
    notification = import_module("lark_bot.modules.notification")
    assert notification.NotificationRequest
    assert notification.Notifier


def test_shared_builder_preserves_session_identity_and_explicit_waiting() -> None:
    model = import_module("lark_bot.modules.notification.notification_model")
    builder = import_module("lark_bot.modules.notification.notification_builder")

    value = model.AgentNotificationInput(
        agent=AgentKind.CLAUDE,
        task_name="review",
        session_id="session-1",
        session_name="review",
        event_name="PermissionRequest",
        status=TaskStatus.WAITING_FOR_INPUT,
        command=["claude"],
        summary="allow command",
    )

    request = builder.build_agent_notification(value)

    assert request.context is not None
    assert request.context.session_id == "session-1"
    assert request.task.stdout_tail == ["allow command"]
    assert request.detection.status is TaskStatus.WAITING_FOR_INPUT
    assert request.detection.tags == [
        "claude",
        "PermissionRequest",
        "waiting_for_input",
    ]


def test_shared_builder_elevates_output_based_waiting() -> None:
    model = import_module("lark_bot.modules.notification.notification_model")
    builder = import_module("lark_bot.modules.notification.notification_builder")

    value = model.AgentNotificationInput(
        agent=AgentKind.CODEX,
        task_name="approval",
        status=TaskStatus.SUCCEEDED,
        command=["codex"],
        output_tail=["Approval required before continuing"],
        tags=["custom"],
    )

    request = builder.build_agent_notification(value)

    assert request.context is None
    assert request.detection.status is TaskStatus.WAITING_FOR_INPUT
    assert request.detection.tags == ["codex", "custom", "approval"]
    assert request.detection.matched_phrases == ["Approval"]


def test_shared_builder_changes_nonzero_success_to_failure() -> None:
    model = import_module("lark_bot.modules.notification.notification_model")
    builder = import_module("lark_bot.modules.notification.notification_builder")

    value = model.AgentNotificationInput(
        agent=AgentKind.CODEX,
        task_name="tests",
        status=TaskStatus.SUCCEEDED,
        command=["codex"],
        exit_code=7,
    )

    request = builder.build_agent_notification(value)

    assert request.task.exit_code == 7
    assert request.detection.status is TaskStatus.FAILED
    assert request.detection.tags == ["codex", "failed"]
