from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from lark_bot.modules.codex.codex_model import (
    CodexAuditEntry,
    CodexSession,
    NotificationOutboxItem,
    PendingInteraction,
)
from lark_bot.core.redaction import redact_text


SUMMARY_LIMIT = 2000


def session_from_row(row: sqlite3.Row) -> CodexSession:
    return CodexSession(
        id=row["id"],
        thread_id=row["thread_id"],
        turn_id=row["turn_id"],
        name=row["name"],
        cwd=row["cwd"],
        model=row["model"],
        sandbox=row["sandbox"],
        status=row["status"],
        summary=row["summary"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def interaction_from_row(row: sqlite3.Row) -> PendingInteraction:
    return PendingInteraction(
        id=row["id"],
        session_id=row["session_id"],
        request_id=row["request_id"],
        kind=row["kind"],
        status=row["status"],
        lark_message_id=row["lark_message_id"],
        payload_summary=row["payload_summary"],
        requested_at=datetime.fromisoformat(row["requested_at"]),
        resolved_at=(
            datetime.fromisoformat(row["resolved_at"])
            if row["resolved_at"] is not None
            else None
        ),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        actor_id=row["actor_id"],
        decision=row["decision"],
    )


def outbox_from_row(row: sqlite3.Row) -> NotificationOutboxItem:
    return NotificationOutboxItem(
        id=row["id"],
        session_id=row["session_id"],
        interaction_id=row["interaction_id"],
        notification_type=row["notification_type"],
        payload_summary=row["payload_summary"],
        attempt_count=row["attempt_count"],
        next_attempt_at=datetime.fromisoformat(row["next_attempt_at"]),
        sent_at=(
            datetime.fromisoformat(row["sent_at"])
            if row["sent_at"] is not None
            else None
        ),
        last_error=row["last_error"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def audit_from_row(row: sqlite3.Row) -> CodexAuditEntry:
    return CodexAuditEntry(
        id=row["id"],
        session_id=row["session_id"],
        interaction_id=row["interaction_id"],
        event_type=row["event_type"],
        actor_id=row["actor_id"],
        detail_summary=row["detail_summary"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def serialize_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


def serialize_optional_datetime(value: datetime | None) -> str | None:
    return serialize_datetime(value) if value is not None else None


def safe_summary(value: str) -> str:
    return redact_text(value)[:SUMMARY_LIMIT]


def interaction_values(interaction: PendingInteraction) -> tuple[object, ...]:
    return (
        interaction.id,
        interaction.session_id,
        interaction.request_id,
        interaction.kind.value,
        interaction.status.value,
        interaction.lark_message_id,
        safe_summary(interaction.payload_summary),
        serialize_datetime(interaction.requested_at),
        serialize_optional_datetime(interaction.resolved_at),
        serialize_datetime(interaction.expires_at),
        interaction.actor_id,
        interaction.decision.value if interaction.decision is not None else None,
    )
