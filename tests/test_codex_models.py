from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from lark_bot.codex_models import (
    CodexAuditEntry,
    CodexSession,
    InteractionKind,
    InteractionStatus,
    NotificationOutboxItem,
    PendingInteraction,
    SessionStatus,
)


def test_codex_domain_models_normalize_timestamps_to_utc():
    local_tz = timezone(timedelta(hours=8))
    session = CodexSession(
        id="session-1",
        thread_id=None,
        turn_id=None,
        name="test session",
        cwd="C:/workspace",
        model="gpt-5",
        sandbox="workspace-write",
        status=SessionStatus.STARTING,
        summary="redacted summary",
        created_at=datetime(2026, 7, 12, 10, 0, tzinfo=local_tz),
        updated_at=datetime(2026, 7, 12, 10, 1),
    )
    interaction = PendingInteraction(
        id="interaction-1",
        session_id=session.id,
        request_id="request-1",
        kind=InteractionKind.EXEC_APPROVAL,
        status=InteractionStatus.PENDING,
        payload_summary="run redacted command",
        requested_at=datetime(2026, 7, 12, 10, 2, tzinfo=local_tz),
        expires_at=datetime(2026, 7, 12, 10, 32, tzinfo=local_tz),
    )

    assert session.created_at == datetime(2026, 7, 12, 2, 0, tzinfo=timezone.utc)
    assert session.updated_at == datetime(2026, 7, 12, 10, 1, tzinfo=timezone.utc)
    assert interaction.requested_at.tzinfo is timezone.utc
    assert interaction.resolved_at is None


def test_codex_domain_enums_cover_required_states():
    assert {status.value for status in SessionStatus} == {
        "starting",
        "running",
        "waiting_for_approval",
        "waiting_for_input",
        "succeeded",
        "failed",
        "interrupted",
        "cancelled",
    }
    assert {kind.value for kind in InteractionKind} == {
        "exec_approval",
        "file_change_approval",
        "permission_request",
        "user_input",
    }
    assert {status.value for status in InteractionStatus} == {
        "pending",
        "resolved",
        "expired",
        "cancelled",
    }
    assert "prompt" not in CodexSession.model_fields
    assert "output" not in CodexSession.model_fields
    assert "prompt" not in PendingInteraction.model_fields
    assert "output" not in PendingInteraction.model_fields


def test_notification_outbox_item_contains_only_redacted_payload_summary():
    item = NotificationOutboxItem(
        id=1,
        session_id="session-1",
        interaction_id=None,
        notification_type="session_started",
        payload_summary="redacted notification summary",
        attempt_count=0,
        next_attempt_at=datetime(2026, 7, 12, 12, 0),
        sent_at=None,
        last_error=None,
        created_at=datetime(2026, 7, 12, 12, 0),
    )

    assert item.next_attempt_at.tzinfo is timezone.utc
    assert "payload" not in NotificationOutboxItem.model_fields


def test_codex_audit_entry_normalizes_timestamp_and_has_no_raw_payload_fields():
    entry = CodexAuditEntry(
        id=1,
        session_id="session-1",
        interaction_id="interaction-1",
        event_type="interaction_resolved",
        actor_id="user-1",
        detail_summary="redacted audit detail",
        created_at=datetime(2026, 7, 12, 12, 0),
    )

    assert entry.created_at == datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    assert "payload" not in CodexAuditEntry.model_fields
    assert "prompt" not in CodexAuditEntry.model_fields
    assert "output" not in CodexAuditEntry.model_fields


@pytest.mark.parametrize("field", ["decision", "actor_id", "resolved_at"])
def test_pending_interaction_rejects_initial_resolution_metadata(field):
    values = {
        "id": "interaction-1",
        "session_id": "session-1",
        "request_id": "request-1",
        "kind": InteractionKind.EXEC_APPROVAL,
        "status": InteractionStatus.PENDING,
        "requested_at": datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc),
        "expires_at": datetime(2026, 7, 12, 12, 30, tzinfo=timezone.utc),
        field: (
            datetime(2026, 7, 12, 12, 1, tzinfo=timezone.utc)
            if field == "resolved_at"
            else "unsafe-initial-value"
        ),
    }

    with pytest.raises(ValidationError):
        PendingInteraction(**values)


def test_pending_interaction_rejects_resolution_metadata_assignment():
    interaction = PendingInteraction(
        id="interaction-1",
        session_id="session-1",
        request_id="request-1",
        kind=InteractionKind.EXEC_APPROVAL,
        requested_at=datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc),
        expires_at=datetime(2026, 7, 12, 12, 30, tzinfo=timezone.utc),
    )

    with pytest.raises(ValidationError):
        interaction.decision = "approved"

    assert interaction.status is InteractionStatus.PENDING
    assert interaction.decision is None
    assert interaction.actor_id is None
    assert interaction.resolved_at is None
