from datetime import datetime, timedelta, timezone
from lark_bot.storage.sqlite import SQLiteNotificationStore


def test_sqlite_store_suppresses_duplicate_within_cooldown():
    store = SQLiteNotificationStore(":memory:")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    assert store.should_send("same-key", cooldown_seconds=300, now=now)
    store.record_sent("same-key", status="succeeded", now=now)

    assert not store.should_send(
        "same-key",
        cooldown_seconds=300,
        now=now + timedelta(seconds=60),
    )


def test_sqlite_store_allows_duplicate_after_cooldown():
    store = SQLiteNotificationStore(":memory:")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    store.record_sent("same-key", status="failed", now=now)

    assert store.should_send(
        "same-key",
        cooldown_seconds=300,
        now=now + timedelta(seconds=301),
    )
