import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from lark_bot.codex.models import (
    CodexAuditEntry,
    CodexSession,
    InteractionDecision,
    InteractionKind,
    InteractionStatus,
    PendingInteraction,
    SessionStatus,
)
from lark_bot.storage.codex_sqlite import SQLiteCodexStore


NOW = datetime(2026, 7, 12, 4, 0, tzinfo=timezone.utc)


@pytest.fixture
def local_database():
    path = Path("tests") / f".codex-test-{uuid4().hex}.db"
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def make_session(
    session_id: str = "session-1",
    status: SessionStatus = SessionStatus.STARTING,
) -> CodexSession:
    return CodexSession(
        id=session_id,
        name="automation task",
        cwd="C:/workspace",
        model="gpt-5",
        sandbox="workspace-write",
        status=status,
        summary="redacted task summary",
        created_at=NOW,
        updated_at=NOW,
    )


def make_interaction(
    interaction_id: str = "interaction-1",
    session_id: str = "session-1",
    *,
    kind: InteractionKind = InteractionKind.EXEC_APPROVAL,
    payload_summary: str = "redacted command summary",
) -> PendingInteraction:
    return PendingInteraction(
        id=interaction_id,
        session_id=session_id,
        request_id=f"request-{interaction_id}",
        kind=kind,
        status=InteractionStatus.PENDING,
        payload_summary=payload_summary,
        requested_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
    )


def test_create_get_list_and_update_sessions_in_memory():
    store = SQLiteCodexStore(":memory:")
    first = make_session()
    second = make_session("session-2", SessionStatus.RUNNING)

    store.create_session(first)
    store.create_session(second)

    assert store.get_session(first.id) == first
    assert [session.id for session in store.list_sessions()] == [first.id, second.id]
    assert [session.id for session in store.list_sessions(SessionStatus.RUNNING)] == [
        second.id
    ]

    updated_at = NOW + timedelta(minutes=1)
    updated = store.update_session(
        first.id,
        status=SessionStatus.WAITING_FOR_APPROVAL,
        thread_id="thread-1",
        turn_id="turn-1",
        summary="redacted approval summary",
        updated_at=updated_at,
    )

    assert updated is not None
    assert updated.status is SessionStatus.WAITING_FOR_APPROVAL
    assert updated.thread_id == "thread-1"
    assert updated.turn_id == "turn-1"
    assert updated.summary == "redacted approval summary"
    assert updated.updated_at == updated_at
    assert store.get_session("missing") is None


def test_update_session_if_status_is_compare_and_swap():
    store = SQLiteCodexStore(":memory:")
    store.create_session(make_session(status=SessionStatus.RUNNING))
    updated_at = NOW + timedelta(minutes=1)

    assert store.update_session_if_status(
        "session-1",
        (SessionStatus.RUNNING,),
        status=SessionStatus.WAITING_FOR_APPROVAL,
        turn_id="turn-1",
        updated_at=updated_at,
    )
    assert not store.update_session_if_status(
        "session-1",
        (SessionStatus.RUNNING,),
        status=SessionStatus.FAILED,
        updated_at=NOW + timedelta(minutes=2),
    )

    stored = store.get_session("session-1")
    assert stored.status is SessionStatus.WAITING_FOR_APPROVAL
    assert stored.turn_id == "turn-1"
    assert stored.updated_at == updated_at


def test_create_interaction_and_mark_waiting_is_atomic_and_pending_id_is_reusable():
    store = SQLiteCodexStore(":memory:")
    store.create_session(make_session(status=SessionStatus.RUNNING))
    first = make_interaction()

    assert store.create_interaction_and_mark_waiting(
        first,
        SessionStatus.WAITING_FOR_APPROVAL,
        NOW + timedelta(minutes=1),
    )
    duplicate = make_interaction("interaction-2").model_copy(
        update={"request_id": first.request_id}
    )
    assert not store.create_interaction_and_mark_waiting(
        duplicate,
        SessionStatus.WAITING_FOR_APPROVAL,
        NOW + timedelta(minutes=2),
    )
    assert store.get_interaction(duplicate.id) is None

    assert store.resolve_interaction(
        first.id, decision="approved", actor_id="user", resolved_at=NOW
    )
    store.update_session(
        "session-1", status=SessionStatus.RUNNING, updated_at=NOW
    )
    assert store.create_interaction_and_mark_waiting(
        duplicate,
        SessionStatus.WAITING_FOR_APPROVAL,
        NOW + timedelta(minutes=3),
    )


def test_create_interaction_and_mark_waiting_rejects_inactive_session_without_insert():
    store = SQLiteCodexStore(":memory:")
    store.create_session(make_session(status=SessionStatus.SUCCEEDED))

    assert not store.create_interaction_and_mark_waiting(
        make_interaction(),
        SessionStatus.WAITING_FOR_APPROVAL,
        NOW,
    )
    assert store.get_interaction("interaction-1") is None


def test_resolve_interaction_and_refresh_session_updates_both_or_neither():
    store = SQLiteCodexStore(":memory:")
    store.create_session(make_session(status=SessionStatus.RUNNING))
    interaction = make_interaction()
    assert store.create_interaction_and_mark_waiting(
        interaction, SessionStatus.WAITING_FOR_APPROVAL, NOW
    )
    resolved_at = NOW + timedelta(minutes=1)

    assert store.resolve_interaction_and_refresh_session(
        interaction.id,
        decision="approved",
        actor_id="user-1",
        updated_at=resolved_at,
    )
    assert not store.resolve_interaction_and_refresh_session(
        interaction.id,
        decision="denied",
        actor_id="user-2",
        updated_at=NOW + timedelta(minutes=2),
    )

    stored = store.get_interaction(interaction.id)
    assert stored.status is InteractionStatus.RESOLVED
    assert stored.actor_id == "user-1"
    assert store.get_session("session-1").status is SessionStatus.RUNNING
    assert store.get_session("session-1").updated_at == resolved_at


def test_cancel_interaction_and_refresh_session_is_pending_only():
    store = SQLiteCodexStore(":memory:")
    store.create_session(make_session(status=SessionStatus.RUNNING))
    interaction = make_interaction()
    assert store.create_interaction_and_mark_waiting(
        interaction, SessionStatus.WAITING_FOR_APPROVAL, NOW
    )
    cancelled_at = NOW + timedelta(minutes=1)

    assert store.cancel_interaction_and_refresh_session(
        interaction.id, updated_at=cancelled_at
    )
    assert not store.cancel_interaction_and_refresh_session(
        interaction.id, updated_at=NOW + timedelta(minutes=2)
    )

    stored = store.get_interaction(interaction.id)
    assert stored.status is InteractionStatus.CANCELLED
    assert stored.actor_id is None
    assert stored.decision is None
    assert stored.resolved_at == cancelled_at
    assert store.get_session("session-1").status is SessionStatus.RUNNING


def test_resolve_interaction_refreshes_to_highest_priority_remaining_wait():
    store = SQLiteCodexStore(":memory:")
    store.create_session(make_session(status=SessionStatus.RUNNING))
    approval = make_interaction("approval")
    user_input = make_interaction("input", kind=InteractionKind.USER_INPUT)
    store.create_interaction(approval)
    store.create_interaction(user_input)
    store.update_session(
        "session-1", status=SessionStatus.WAITING_FOR_INPUT, updated_at=NOW
    )

    assert store.resolve_interaction_and_refresh_session(
        user_input.id,
        decision="the answer",
        actor_id="user-1",
        updated_at=NOW + timedelta(minutes=1),
    )

    assert (
        store.get_session("session-1").status
        is SessionStatus.WAITING_FOR_APPROVAL
    )


def test_resolve_interaction_commits_claim_without_overwriting_terminal_session():
    store = SQLiteCodexStore(":memory:")
    store.create_session(make_session(status=SessionStatus.CANCELLED))
    interaction = make_interaction()
    store.create_interaction(interaction)

    assert store.resolve_interaction_and_refresh_session(
        interaction.id,
        decision="denied",
        actor_id="user-1",
        updated_at=NOW + timedelta(minutes=1),
    )

    assert store.get_interaction(interaction.id).status is InteractionStatus.RESOLVED
    assert store.get_session("session-1").status is SessionStatus.CANCELLED


def test_claim_session_terminal_is_atomic_and_returns_cancelled_pending_ids():
    store = SQLiteCodexStore(":memory:")
    store.create_session(make_session(status=SessionStatus.WAITING_FOR_INPUT))
    for interaction_id in ("interaction-b", "interaction-a"):
        store.create_interaction(make_interaction(interaction_id))
    completed_at = NOW + timedelta(minutes=1)

    claimed = store.claim_session_terminal(
        "session-1",
        SessionStatus.CANCELLED,
        "cancelled by user",
        InteractionStatus.CANCELLED,
        completed_at,
    )

    assert claimed == ["interaction-a", "interaction-b"]
    assert store.get_session("session-1").status is SessionStatus.CANCELLED
    assert all(
        store.get_interaction(interaction_id).status is InteractionStatus.CANCELLED
        for interaction_id in claimed
    )
    assert (
        store.claim_session_terminal(
            "session-1",
            SessionStatus.FAILED,
            "late failure",
            InteractionStatus.EXPIRED,
            NOW + timedelta(minutes=2),
        )
        is None
    )


def test_get_session_by_thread_returns_exact_match():
    store = SQLiteCodexStore(":memory:")
    first = make_session()
    second = make_session("session-2")
    store.create_session(first)
    store.create_session(second)
    store.update_session(first.id, thread_id="thread-1", updated_at=NOW)

    assert store.get_session_by_thread("thread-1").id == first.id
    assert store.get_session_by_thread("missing") is None


def test_codex_store_creates_dedicated_schema_tables():
    store = SQLiteCodexStore(":memory:")

    with store._connection() as connection:
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert {
        "codex_sessions",
        "codex_interactions",
        "codex_event_dedupe",
        "notification_outbox",
        "codex_audit",
    } <= table_names


def test_create_get_and_attach_message_to_pending_interaction():
    store = SQLiteCodexStore(":memory:")
    store.create_session(make_session())
    interaction = make_interaction()

    store.create_interaction(interaction)

    assert store.get_interaction(interaction.id) == interaction
    assert store.get_pending_interaction(interaction.request_id) == interaction
    assert store.attach_lark_message_id(interaction.id, "message-1")
    stored = store.get_interaction(interaction.id)
    assert stored is not None
    assert stored.lark_message_id == "message-1"
    assert store.get_pending_interaction_by_lark_message_id("message-1") == stored
    assert store.get_pending_interaction_by_lark_message_id("missing") is None
    assert not store.attach_lark_message_id(interaction.id, "message-hijack")
    assert store.get_interaction(interaction.id).lark_message_id == "message-1"
    assert not store.attach_lark_message_id("missing", "message-2")


def test_first_pending_interaction_resolver_wins_without_overwrite():
    store = SQLiteCodexStore(":memory:")
    store.create_session(make_session())
    interaction = make_interaction()
    store.create_interaction(interaction)
    first_resolution = NOW + timedelta(minutes=1)

    assert store.resolve_interaction(
        interaction.id,
        decision="approved",
        actor_id="user-1",
        resolved_at=first_resolution,
    )
    assert not store.resolve_interaction(
        interaction.id,
        decision="denied",
        actor_id="user-2",
        resolved_at=NOW + timedelta(minutes=2),
    )

    stored = store.get_interaction(interaction.id)
    assert stored is not None
    assert stored.status is InteractionStatus.RESOLVED
    assert stored.decision == "approved"
    assert stored.actor_id == "user-1"
    assert stored.resolved_at == first_resolution
    assert store.get_pending_interaction(interaction.request_id) is None
    assert store.get_pending_interaction_by_lark_message_id("message-1") is None


def test_expire_interaction_is_pending_only_and_first_transition_wins():
    store = SQLiteCodexStore(":memory:")
    store.create_session(make_session())
    store.create_interaction(make_interaction())
    expired_at = NOW + timedelta(minutes=31)

    assert store.expire_interaction("interaction-1", resolved_at=expired_at)
    assert not store.expire_interaction("interaction-1", resolved_at=expired_at)
    stored = store.get_interaction("interaction-1")
    assert stored.status is InteractionStatus.EXPIRED
    assert stored.resolved_at == expired_at


@pytest.mark.parametrize("status", [InteractionStatus.EXPIRED, InteractionStatus.CANCELLED])
def test_cancel_pending_interactions_updates_only_pending_in_id_order(status):
    store = SQLiteCodexStore(":memory:")
    store.create_session(make_session())
    for interaction_id in ("interaction-b", "interaction-a", "interaction-c"):
        store.create_interaction(make_interaction(interaction_id))
    assert store.resolve_interaction(
        "interaction-c", decision="approved", actor_id="user", resolved_at=NOW
    )
    resolved_at = NOW + timedelta(minutes=1)

    changed = store.cancel_pending_interactions(
        "session-1", status=status, resolved_at=resolved_at
    )

    assert changed == ["interaction-a", "interaction-b"]
    assert store.get_interaction("interaction-a").status is status
    assert store.get_interaction("interaction-b").resolved_at == resolved_at
    assert store.get_interaction("interaction-c").status is InteractionStatus.RESOLVED


def test_event_dedupe_insertion_succeeds_only_once():
    store = SQLiteCodexStore(":memory:")

    assert store.record_event_once("event-1", received_at=NOW)
    assert not store.record_event_once(
        "event-1",
        received_at=NOW + timedelta(seconds=1),
    )
    assert store.record_event_once("event-2", received_at=NOW)


def test_outbox_enqueue_list_due_and_mark_sent():
    store = SQLiteCodexStore(":memory:")
    store.create_session(make_session())
    due_id = store.enqueue_outbox(
        notification_type="session_started",
        payload_summary="redacted start summary",
        session_id="session-1",
        next_attempt_at=NOW,
        created_at=NOW,
    )
    future_id = store.enqueue_outbox(
        notification_type="session_update",
        payload_summary="redacted future summary",
        session_id="session-1",
        next_attempt_at=NOW + timedelta(minutes=5),
        created_at=NOW,
    )

    due = store.list_due_outbox(now=NOW, limit=10)

    assert [item.id for item in due] == [due_id]
    assert due[0].attempt_count == 0
    assert store.mark_outbox_sent(due_id, sent_at=NOW + timedelta(seconds=1))
    assert not store.mark_outbox_sent(due_id, sent_at=NOW + timedelta(seconds=2))
    assert store.list_due_outbox(now=NOW + timedelta(minutes=10), limit=10) == [
        store.get_outbox_item(future_id)
    ]


def test_outbox_failure_increments_attempts_and_reschedules():
    store = SQLiteCodexStore(":memory:")
    outbox_id = store.enqueue_outbox(
        notification_type="daemon_warning",
        payload_summary="redacted warning",
        next_attempt_at=NOW,
        created_at=NOW,
    )
    retry_at = NOW + timedelta(seconds=30)

    assert store.record_outbox_failure(
        outbox_id,
        error="temporary timeout",
        next_attempt_at=retry_at,
    )
    assert store.list_due_outbox(now=NOW, limit=10) == []

    item = store.get_outbox_item(outbox_id)
    assert item is not None
    assert item.attempt_count == 1
    assert item.next_attempt_at == retry_at
    assert item.last_error == "temporary timeout"
    assert store.list_due_outbox(now=retry_at, limit=10) == [item]


def test_startup_reconciliation_interrupts_active_sessions_and_expires_pending():
    store = SQLiteCodexStore(":memory:")
    active_statuses = (
        SessionStatus.STARTING,
        SessionStatus.RUNNING,
        SessionStatus.WAITING_FOR_APPROVAL,
        SessionStatus.WAITING_FOR_INPUT,
    )
    for index, status in enumerate(active_statuses):
        session = make_session(f"active-{index}", status)
        store.create_session(session)
        store.create_interaction(
            make_interaction(f"pending-{index}", session.id)
        )
    terminal = make_session("terminal", SessionStatus.SUCCEEDED)
    store.create_session(terminal)
    resolved = make_interaction("resolved", terminal.id)
    store.create_interaction(resolved)
    assert store.resolve_interaction(
        resolved.id,
        decision="approved",
        actor_id="user-1",
        resolved_at=NOW,
    )

    result = store.reconcile_startup(now=NOW + timedelta(hours=1))

    assert result.session_ids == [
        "active-0",
        "active-1",
        "active-2",
        "active-3",
    ]
    assert result.interaction_ids == [
        "pending-0",
        "pending-1",
        "pending-2",
        "pending-3",
    ]
    assert all(
        store.get_session(f"active-{index}").status is SessionStatus.INTERRUPTED
        for index in range(4)
    )
    assert store.get_session("terminal") == terminal
    assert all(
        store.get_interaction(f"pending-{index}").status
        is InteractionStatus.EXPIRED
        for index in range(4)
    )
    assert store.get_interaction("resolved").status is InteractionStatus.RESOLVED


def test_audit_entries_are_recorded_and_listed_in_creation_order():
    store = SQLiteCodexStore(":memory:")
    store.create_session(make_session())
    interaction = make_interaction()
    store.create_interaction(interaction)

    first_id = store.record_audit(
        event_type="session_started",
        detail_summary="redacted start detail",
        session_id="session-1",
        created_at=NOW,
    )
    second_id = store.record_audit(
        event_type="interaction_created",
        detail_summary="redacted interaction detail",
        session_id="session-1",
        interaction_id="interaction-1",
        actor_id="daemon",
        created_at=NOW + timedelta(seconds=1),
    )

    assert store.list_audit(session_id="session-1") == [
        CodexAuditEntry(
            id=first_id,
            session_id="session-1",
            interaction_id=None,
            event_type="session_started",
            actor_id=None,
            detail_summary="redacted start detail",
            created_at=NOW,
        ),
        CodexAuditEntry(
            id=second_id,
            session_id="session-1",
            interaction_id="interaction-1",
            event_type="interaction_created",
            actor_id="daemon",
            detail_summary="redacted interaction detail",
            created_at=NOW + timedelta(seconds=1),
        ),
    ]


def test_persisted_summaries_and_errors_are_redacted_and_bounded():
    store = SQLiteCodexStore(":memory:")
    session = make_session()
    session.summary = "token=session-secret " + ("x" * 3000)
    store.create_session(session)
    interaction = make_interaction(
        payload_summary="password=interaction-secret"
    )
    store.create_interaction(interaction)
    outbox_id = store.enqueue_outbox(
        notification_type="session_update",
        payload_summary="api_key=outbox-secret",
        created_at=NOW,
    )
    store.record_outbox_failure(
        outbox_id,
        error="authorization: bearer failure-secret",
        next_attempt_at=NOW,
    )
    store.record_audit(
        event_type="security_test",
        detail_summary="secret=audit-secret",
        created_at=NOW,
    )

    stored_session = store.get_session(session.id)
    stored_interaction = store.get_interaction(interaction.id)
    stored_outbox = store.get_outbox_item(outbox_id)
    stored_audit = store.list_audit()[0]

    assert stored_session is not None
    assert stored_interaction is not None
    assert stored_outbox is not None
    persisted_text = (
        stored_session.summary,
        stored_interaction.payload_summary,
        stored_outbox.payload_summary,
        stored_outbox.last_error,
        stored_audit.detail_summary,
    )
    for raw_secret in (
        "session-secret",
        "interaction-secret",
        "outbox-secret",
        "failure-secret",
        "audit-secret",
    ):
        assert all(
            value is not None and raw_secret not in value for value in persisted_text
        )
    assert all(len(value) <= 2000 for value in persisted_text if value is not None)
    assert all("[REDACTED]" in value for value in persisted_text if value is not None)


@pytest.mark.parametrize(
    ("kind", "decision"),
    [
        (InteractionKind.EXEC_APPROVAL, "approved"),
        (InteractionKind.EXEC_APPROVAL, "denied"),
        (InteractionKind.FILE_CHANGE_APPROVAL, "accept"),
        (InteractionKind.FILE_CHANGE_APPROVAL, "decline"),
        (InteractionKind.PERMISSION_REQUEST, "granted"),
        (InteractionKind.PERMISSION_REQUEST, "denied"),
    ],
)
def test_approval_interactions_persist_only_kind_specific_outcomes(kind, decision):
    store = SQLiteCodexStore(":memory:")
    store.create_session(make_session())
    interaction = make_interaction(kind=kind)
    store.create_interaction(interaction)

    assert store.resolve_interaction(
        interaction.id,
        decision=decision,
        actor_id="user-1",
        resolved_at=NOW,
    )
    assert store.get_interaction(interaction.id).decision == decision


def test_invalid_approval_outcome_is_rejected_without_resolving():
    store = SQLiteCodexStore(":memory:")
    store.create_session(make_session())
    interaction = make_interaction()
    store.create_interaction(interaction)

    with pytest.raises(ValueError, match="decision"):
        store.resolve_interaction(
            interaction.id,
            decision="acceptForSession",
            actor_id="user-1",
            resolved_at=NOW,
        )

    assert store.get_interaction(interaction.id).status is InteractionStatus.PENDING


def test_create_interaction_rejects_bypassed_pending_resolution_metadata():
    store = SQLiteCodexStore(":memory:")
    store.create_session(make_session())
    invalid = make_interaction().model_copy(
        update={"decision": InteractionDecision.APPROVED}
    )

    with pytest.raises(ValueError, match="pending interaction"):
        store.create_interaction(invalid)

    with store._connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM codex_interactions"
        ).fetchone()[0]
    assert count == 0


def test_user_input_resolution_never_persists_raw_reply():
    store = SQLiteCodexStore(":memory:")
    store.create_session(make_session())
    interaction = make_interaction(kind=InteractionKind.USER_INPUT)
    store.create_interaction(interaction)
    raw_reply = "token=reply-secret complete the deployment"

    assert store.resolve_interaction(
        interaction.id,
        decision=raw_reply,
        actor_id="user-1",
        resolved_at=NOW,
    )

    stored = store.get_interaction(interaction.id)
    assert stored.decision == "submitted"
    with store._connection() as connection:
        raw_decision = connection.execute(
            "SELECT decision FROM codex_interactions WHERE id = ?",
            (interaction.id,),
        ).fetchone()[0]
    assert raw_decision == "submitted"
    assert "reply-secret" not in stored.model_dump_json()


def test_connection_lifecycle_closes_file_connections_and_close_is_idempotent(
    local_database,
):
    store = SQLiteCodexStore(local_database)

    with store._connection() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")

    store.close()
    store.close()
    with pytest.raises(RuntimeError, match="closed"):
        store.list_sessions()


def test_memory_connection_is_shared_until_store_is_closed():
    with SQLiteCodexStore(":memory:") as store:
        store.create_session(make_session())
        with store._connection() as first:
            first_identity = id(first)
        with store._connection() as second:
            assert id(second) == first_identity
        assert store.get_session("session-1") is not None

    store.close()
    with pytest.raises(RuntimeError, match="closed"):
        store.get_session("session-1")


def test_concurrent_file_backed_resolution_has_exactly_one_winner(local_database):
    setup_store = SQLiteCodexStore(local_database)
    setup_store.create_session(make_session())
    setup_store.create_interaction(make_interaction())
    setup_store.close()
    barrier = threading.Barrier(2)
    results: list[bool] = []
    errors: list[BaseException] = []

    def resolve(decision: str, actor_id: str) -> None:
        try:
            with SQLiteCodexStore(local_database) as store:
                barrier.wait(timeout=5)
                results.append(
                    store.resolve_interaction(
                        "interaction-1",
                        decision=decision,
                        actor_id=actor_id,
                        resolved_at=NOW,
                    )
                )
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=resolve, args=("approved", "user-1")),
        threading.Thread(target=resolve, args=("denied", "user-2")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert not any(thread.is_alive() for thread in threads)
    assert sorted(results) == [False, True]


def test_schema_version_and_required_indexes_are_installed():
    store = SQLiteCodexStore(":memory:")

    with store._connection() as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }

    assert version == 2
    assert {
        "idx_codex_sessions_status",
        "idx_codex_interactions_status",
        "idx_notification_outbox_due",
        "idx_codex_audit_session_created",
        "idx_codex_interactions_pending_request",
    } <= indexes


def test_schema_v1_migration_canonicalizes_request_ids_and_preserves_reuse(
    local_database,
):
    connection = sqlite3.connect(local_database)
    connection.executescript(
        """
        PRAGMA user_version = 1;
        CREATE TABLE codex_sessions (
            id TEXT PRIMARY KEY, thread_id TEXT, turn_id TEXT, name TEXT NOT NULL,
            cwd TEXT NOT NULL, model TEXT, sandbox TEXT NOT NULL, status TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE codex_interactions (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES codex_sessions(id),
            request_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, status TEXT NOT NULL,
            lark_message_id TEXT, payload_summary TEXT NOT NULL DEFAULT '',
            requested_at TEXT NOT NULL, resolved_at TEXT, expires_at TEXT NOT NULL,
            actor_id TEXT, decision TEXT
        );
        CREATE TABLE notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT REFERENCES codex_sessions(id),
            interaction_id TEXT REFERENCES codex_interactions(id),
            notification_type TEXT NOT NULL, payload_summary TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL,
            sent_at TEXT, last_error TEXT, created_at TEXT NOT NULL
        );
        """
    )
    session = make_session()
    connection.execute(
        """INSERT INTO codex_sessions
        (id, name, cwd, model, sandbox, status, summary, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session.id, session.name, session.cwd, session.model, session.sandbox,
            session.status.value, session.summary, session.created_at.isoformat(),
            session.updated_at.isoformat(),
        ),
    )
    interaction = make_interaction()
    connection.execute(
        """INSERT INTO codex_interactions
        (id, session_id, request_id, kind, status, payload_summary, requested_at,
         expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            interaction.id, interaction.session_id, "legacy-id", interaction.kind.value,
            InteractionStatus.RESOLVED.value, interaction.payload_summary,
            interaction.requested_at.isoformat(), interaction.expires_at.isoformat(),
        ),
    )
    connection.execute(
        """INSERT INTO notification_outbox
        (interaction_id, notification_type, payload_summary, next_attempt_at, created_at)
        VALUES (?, 'approval', 'safe', ?, ?)""",
        (interaction.id, NOW.isoformat(), NOW.isoformat()),
    )
    connection.commit()
    connection.close()

    store = SQLiteCodexStore(local_database)

    assert store.get_interaction(interaction.id).request_id == '"legacy-id"'
    with store._connection() as migrated:
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 2
    reused = make_interaction("interaction-2").model_copy(
        update={"request_id": '"legacy-id"'}
    )
    store.create_interaction(reused)
