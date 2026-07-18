from importlib import import_module


def test_notification_module_owns_request_and_sender_contract() -> None:
    notification = import_module("lark_bot.modules.notification")
    assert notification.NotificationRequest
    assert notification.Notifier
