from datetime import datetime, timedelta, timezone
import inspect
import sqlite3
import threading
from uuid import uuid4
import pytest

from lark_bot.modules.agent.agent_model import (
    AgentKind,
    AgentInteraction,
    AgentSession,
    InteractionKind,
    InteractionStatus,
    SessionStatus,
)
from lark_bot.modules.agent.agent_store import SQLiteAgentStore
from lark_bot.modules.agent import agent_schema
from lark_bot.modules.codex import codex_schema


def test_schema_migrations_are_explicit_and_shared():
    source = inspect.getsource(agent_schema)
    assert ".split(" not in source
    assert "executescript" not in source
    assert set(agent_schema.MIGRATIONS) == {1, 2, 3, 4}
    assert all(agent_schema.MIGRATIONS[version] for version in range(1, 5))
    assert any("json_quote(request_id)" in statement for statement in agent_schema.MIGRATIONS[2])
    assert all(column in " ".join(agent_schema.MIGRATIONS[3]) for column in ("agent", "session_name"))
    assert any("CREATE TABLE IF NOT EXISTS agent_sessions" in statement for statement in agent_schema.MIGRATIONS[4])
    assert any("CREATE TRIGGER" in statement for statement in agent_schema.MIGRATIONS[4])
    assert codex_schema.MIGRATIONS is agent_schema.MIGRATIONS
    assert codex_schema.initialize_schema is agent_schema.initialize_schema


def test_shared_schema_and_session_round_trip():
    now = datetime.now(timezone.utc)
    session = AgentSession(
        session_id="s1",
        agent=AgentKind.CLAUDE,
        name="chat",
        conversation_id="c1",
        cwd="/tmp",
        status=SessionStatus.RUNNING,
        created_at=now,
        updated_at=now,
    )
    with SQLiteAgentStore(":memory:") as store:
        store.create(session)
        assert store.get("s1") == session
        with store._connection() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert {
            "agent_sessions",
            "agent_interactions",
            "agent_event_dedupe",
            "agent_notification_outbox",
            "agent_audit",
        } <= tables
        with store._connection() as connection:
            assert "id" in {row["name"] for row in connection.execute("PRAGMA table_info(agent_sessions)")}
            assert "id" in {row["name"] for row in connection.execute("PRAGMA table_info(agent_interactions)")}
        assert store.get_session("s1") == session
        assert store.list_sessions(agent=AgentKind.CLAUDE) == [session]


def test_provider_scoped_mutators_cannot_touch_claude_rows():
    now = datetime.now(timezone.utc)
    with SQLiteAgentStore(":memory:") as store:
        store.create(AgentSession(session_id="cx", agent=AgentKind.CODEX, name="cx", status=SessionStatus.RUNNING, created_at=now, updated_at=now))
        store.create(AgentSession(session_id="cl", agent=AgentKind.CLAUDE, name="cl", status=SessionStatus.RUNNING, created_at=now, updated_at=now))
        store.create_interaction(AgentInteraction(interaction_id="ci", session_id="cl", request_id="cr", kind=InteractionKind.EXEC_APPROVAL, requested_at=now, expires_at=now))
        assert store.get_interaction("ci", agent=AgentKind.CODEX) is None
        assert not store.attach_lark_message_id("ci", "m", agent=AgentKind.CODEX)
        assert not store.resolve_interaction("ci", decision="approved", actor_id="x", agent=AgentKind.CODEX)
        assert not store.cancel_interaction_and_refresh_session("ci", updated_at=now, agent=AgentKind.CODEX)
        assert not store.expire_interaction("ci", resolved_at=now, agent=AgentKind.CODEX)
        assert store.get_session("cl", agent=AgentKind.CODEX) is None
        assert not store.update_session_if_status("cl", (SessionStatus.RUNNING,), status=SessionStatus.FAILED, agent=AgentKind.CODEX)
        assert store.get_session("cl").status is SessionStatus.RUNNING


def test_outbox_requires_canonical_agent_and_scopes_mutators():
    now = datetime.now(timezone.utc)
    with SQLiteAgentStore(":memory:") as store:
        store.create(AgentSession(session_id="cl", agent=AgentKind.CLAUDE, name="cl", status=SessionStatus.RUNNING, created_at=now, updated_at=now))
        with pytest.raises(ValueError):
            store.enqueue_outbox(notification_type="n", payload_summary="x")
        with pytest.raises(ValueError):
            store.enqueue_outbox(notification_type="n", payload_summary="x", session_id="cl", agent=AgentKind.CODEX)
        outbox_id = store.enqueue_outbox(notification_type="n", payload_summary="x", session_id="cl", agent=AgentKind.CLAUDE, created_at=now)
        assert store.get_outbox_item(outbox_id, agent=AgentKind.CODEX) is None
        assert not store.mark_outbox_sent(outbox_id, agent=AgentKind.CODEX)
        assert not store.record_outbox_failure(outbox_id, error="x", next_attempt_at=now, agent=AgentKind.CODEX)


def test_model_and_schema_defaults_are_non_nullable():
    now = datetime.now(timezone.utc)
    session = AgentSession(session_id="s", agent=AgentKind.CODEX, name="n")
    assert session.sandbox == "workspace-write"
    interaction = AgentInteraction(interaction_id="i", session_id="s", request_id="r", kind=InteractionKind.EXEC_APPROVAL)
    assert interaction.expires_at is not None
    with SQLiteAgentStore(":memory:") as store:
        with store._connection() as connection:
            session_columns = {row[1]: row for row in connection.execute("PRAGMA table_info(agent_sessions)")}
            interaction_columns = {row[1]: row for row in connection.execute("PRAGMA table_info(agent_interactions)")}
        assert session_columns["sandbox"][4] == "'workspace-write'"
        assert interaction_columns["expires_at"][3] == 1


def test_formal_agent_store_contract_is_exported_with_real_methods():
    from lark_bot.modules.agent import AgentStoreContract
    assert hasattr(AgentStoreContract, "create_session")
    assert inspect.isfunction(SQLiteAgentStore.create_session)
    assert inspect.signature(SQLiteAgentStore.update_session).parameters["session_id"]


def test_update_session_if_status_explicit_none_clears_summary():
    now = datetime.now(timezone.utc)
    with SQLiteAgentStore(":memory:") as store:
        store.create(AgentSession(session_id="s", agent=AgentKind.CODEX, name="n", status=SessionStatus.RUNNING, summary="present", created_at=now, updated_at=now))
        assert store.update_session_if_status("s", (SessionStatus.RUNNING,), status=SessionStatus.SUCCEEDED, summary=None, agent=AgentKind.CODEX)
        assert store.get_session("s").summary == ""



def test_agent_scope_does_not_expose_other_provider_rows():
    now = datetime.now(timezone.utc)
    with SQLiteAgentStore(":memory:") as store:
        for agent in (AgentKind.CODEX, AgentKind.CLAUDE):
            store.create(AgentSession(session_id=agent.value, agent=agent, name=agent.value, status=SessionStatus.RUNNING, created_at=now, updated_at=now))
        assert [s.agent for s in store.list(agent=AgentKind.CLAUDE)] == [AgentKind.CLAUDE]
        assert store.get_by_conversation("missing", agent=AgentKind.CODEX) is None


def test_user_input_resolution_is_submitted_summary():
    now = datetime.now(timezone.utc)
    with SQLiteAgentStore(":memory:") as store:
        store.create(AgentSession(session_id="s", agent=AgentKind.CODEX, name="x", status=SessionStatus.RUNNING, created_at=now, updated_at=now))
        store.create_interaction(AgentInteraction(interaction_id="i", session_id="s", request_id="r", kind=InteractionKind.USER_INPUT, requested_at=now, expires_at=now))
        assert store.resolve_interaction("i", decision="token=secret reply", actor_id="u", resolved_at=now)
        assert store.get_interaction("i").decision == "submitted"


def test_provider_scoped_conversation_lookup_with_same_conversation():
    now = datetime.now(timezone.utc)
    with SQLiteAgentStore(":memory:") as store:
        for agent in (AgentKind.CODEX, AgentKind.CLAUDE):
            store.create(AgentSession(session_id=agent.value, agent=agent, name=agent.value, conversation_id="same", status=SessionStatus.RUNNING, created_at=now, updated_at=now))
        assert store.get_by_conversation("same", agent=AgentKind.CODEX).agent is AgentKind.CODEX
        assert store.get_by_conversation("same", agent=AgentKind.CLAUDE).agent is AgentKind.CLAUDE


def test_claude_rows_do_not_mirror_legacy_tables():
    now = datetime.now(timezone.utc)
    with SQLiteAgentStore(":memory:") as store:
        store.create(AgentSession(session_id="claude", agent=AgentKind.CLAUDE, name="c", status=SessionStatus.RUNNING, created_at=now, updated_at=now))
        with store._connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM codex_sessions").fetchone()[0] == 0


def test_event_dedupe_is_provider_scoped():
    with SQLiteAgentStore(":memory:") as store:
        assert store.record_event_once("same", agent=AgentKind.CODEX)
        assert store.record_event_once("same", agent=AgentKind.CLAUDE)


def test_schema_failure_rolls_back_and_restores_foreign_keys(monkeypatch):
    import sqlite3

    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA user_version = 3")
    monkeypatch.setattr(agent_schema, "_codex_mirror_triggers", lambda _: (_ for _ in ()).throw(RuntimeError("injected")))
    try:
        agent_schema.initialize_schema(connection)
    except RuntimeError:
        pass
    else:
        raise AssertionError("injected migration failure was not raised")
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
    assert connection.execute("SELECT name FROM sqlite_master WHERE name='agent_sessions'").fetchone() is None


def test_version3_legacy_rows_migrate_to_all_canonical_tables():
    import sqlite3

    connection = sqlite3.connect(":memory:")
    connection.executescript("""
    PRAGMA user_version=3;
    CREATE TABLE codex_sessions(id TEXT PRIMARY KEY,thread_id TEXT,turn_id TEXT,name TEXT,cwd TEXT,model TEXT,sandbox TEXT,status TEXT,summary TEXT,created_at TEXT,updated_at TEXT);
    CREATE TABLE codex_interactions(id TEXT PRIMARY KEY,session_id TEXT,request_id TEXT,kind TEXT,status TEXT,lark_message_id TEXT,payload_summary TEXT,requested_at TEXT,resolved_at TEXT,expires_at TEXT,actor_id TEXT,decision TEXT);
    CREATE TABLE codex_event_dedupe(event_id TEXT PRIMARY KEY,received_at TEXT);
    CREATE TABLE notification_outbox(id INTEGER PRIMARY KEY,session_id TEXT,interaction_id TEXT,notification_type TEXT,payload_summary TEXT,attempt_count INTEGER,next_attempt_at TEXT,sent_at TEXT,last_error TEXT,created_at TEXT);
    CREATE TABLE codex_audit(id INTEGER PRIMARY KEY,session_id TEXT,interaction_id TEXT,event_type TEXT,actor_id TEXT,detail_summary TEXT,created_at TEXT);
    INSERT INTO codex_sessions VALUES('s','conv',NULL,'n','/tmp',NULL,'workspace-write','running','','2020','2020');
    INSERT INTO codex_interactions VALUES('i','s','req','exec_approval','pending',NULL,'safe','2020',NULL,'2021',NULL,NULL);
    INSERT INTO codex_event_dedupe VALUES('e','2020');
    INSERT INTO notification_outbox VALUES(1,'s','i','approval','safe',0,'2020',NULL,NULL,'2020');
    INSERT INTO codex_audit VALUES(1,'s','i','started',NULL,'safe','2020');
    """)
    agent_schema.initialize_schema(connection)
    assert connection.execute("SELECT id FROM agent_sessions").fetchone()[0] == "s"
    assert connection.execute("SELECT id FROM agent_interactions").fetchone()[0] == "i"
    assert connection.execute("SELECT event_id FROM agent_event_dedupe").fetchone()[0] == "e"
    assert connection.execute("SELECT session_name FROM agent_notification_outbox").fetchone()[0] == "n"
    assert connection.execute("SELECT id FROM agent_audit").fetchone()[0] == 1
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_partial_v1_migration_adds_missing_tables_and_quotes_request_id():
    import sqlite3

    connection = sqlite3.connect(":memory:")
    connection.executescript("""
    PRAGMA user_version=1;
    CREATE TABLE codex_sessions(id TEXT PRIMARY KEY,thread_id TEXT,turn_id TEXT,name TEXT,cwd TEXT,model TEXT,sandbox TEXT,status TEXT,summary TEXT,created_at TEXT,updated_at TEXT);
    CREATE TABLE codex_interactions(id TEXT PRIMARY KEY,session_id TEXT,request_id TEXT,kind TEXT,status TEXT,lark_message_id TEXT,payload_summary TEXT,requested_at TEXT,resolved_at TEXT,expires_at TEXT,actor_id TEXT,decision TEXT);
    INSERT INTO codex_sessions VALUES('s',NULL,NULL,'n','/tmp',NULL,'workspace-write','running','','2020','2020');
    INSERT INTO codex_interactions VALUES('i','s','legacy','exec_approval','resolved',NULL,'safe','2020',NULL,'2021',NULL,NULL);
    """)
    agent_schema.initialize_schema(connection)
    assert connection.execute("SELECT request_id FROM agent_interactions").fetchone()[0] == '"legacy"'
    assert connection.execute("SELECT COUNT(*) FROM agent_event_dedupe").fetchone()[0] == 0
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v1_migration_quotes_legacy_request_id_once_in_physical_and_canonical_tables():
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
    PRAGMA user_version=1;
    CREATE TABLE codex_sessions(id TEXT PRIMARY KEY,thread_id TEXT,turn_id TEXT,name TEXT,cwd TEXT,model TEXT,sandbox TEXT,status TEXT,summary TEXT,created_at TEXT,updated_at TEXT);
    CREATE TABLE codex_interactions(id TEXT PRIMARY KEY,session_id TEXT,request_id TEXT UNIQUE,kind TEXT,status TEXT,lark_message_id TEXT,payload_summary TEXT,requested_at TEXT,resolved_at TEXT,expires_at TEXT,actor_id TEXT,decision TEXT);
    INSERT INTO codex_sessions VALUES('s',NULL,NULL,'n','/tmp',NULL,'workspace-write','running','','2020','2020');
    INSERT INTO codex_interactions VALUES('i','s','legacy-id','exec_approval','resolved',NULL,'safe','2020',NULL,'2021',NULL,NULL);
    """)
    agent_schema.initialize_schema(connection)
    physical = connection.execute("SELECT request_id FROM codex_interactions WHERE id='i'").fetchone()[0]
    canonical = connection.execute("SELECT request_id FROM agent_interactions WHERE id='i'").fetchone()[0]
    assert physical == canonical == '"legacy-id"'
    connection.close()


def test_concurrent_refresh_resolution_has_exactly_one_winner():
    from pathlib import Path

    path = Path("tests") / f".agent-test-{uuid4().hex}.db"
    now = datetime.now(timezone.utc)
    try:
        with SQLiteAgentStore(path) as setup:
            setup.create(AgentSession(session_id="s", agent=AgentKind.CODEX, name="codex", status=SessionStatus.RUNNING, created_at=now, updated_at=now))
            setup.create_interaction(AgentInteraction(interaction_id="i", session_id="s", request_id="r", kind=InteractionKind.EXEC_APPROVAL, requested_at=now, expires_at=now + timedelta(minutes=1)))
        barrier = threading.Barrier(2)
        results: list[bool] = []
        errors: list[BaseException] = []

        def resolve(actor: str) -> None:
            try:
                with SQLiteAgentStore(path) as store:
                    barrier.wait(timeout=5)
                    results.append(store.resolve_interaction_and_refresh_session("i", decision="approved", actor_id=actor, updated_at=now + timedelta(seconds=1)))
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=resolve, args=("a",)), threading.Thread(target=resolve, args=("b",))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert not errors
        assert sorted(results) == [False, True]
        with SQLiteAgentStore(path) as store:
            assert store.get_interaction("i").status is InteractionStatus.RESOLVED
            assert store.get_session("s").status is SessionStatus.RUNNING
    finally:
        path.unlink(missing_ok=True)


def test_reconcile_startup_filters_provider_and_unfiltered_scope():
    from pathlib import Path

    now = datetime.now(timezone.utc)
    path = Path("tests") / f".agent-test-{uuid4().hex}.db"
    try:
        with SQLiteAgentStore(path) as store:
            for agent in (AgentKind.CODEX, AgentKind.CLAUDE):
                session_id = f"{agent.value}-s"
                store.create(AgentSession(session_id=session_id, agent=agent, name=agent.value, conversation_id="same", status=SessionStatus.RUNNING, created_at=now, updated_at=now))
                store.create_interaction(AgentInteraction(interaction_id=f"{agent.value}-i", session_id=session_id, request_id=f"{agent.value}-r", kind=InteractionKind.EXEC_APPROVAL, requested_at=now, expires_at=now + timedelta(minutes=1)))
            result = store.reconcile_startup(now=now + timedelta(hours=1), agent=AgentKind.CODEX)
            assert result.session_ids == ["codex-s"]
            assert result.interaction_ids == ["codex-i"]
            assert store.get_session("claude-s").status is SessionStatus.RUNNING
            assert store.get_interaction("claude-i").status is InteractionStatus.PENDING
            result = store.reconcile_startup(now=now + timedelta(hours=2))
            assert result.session_ids == ["claude-s"]
            assert result.interaction_ids == ["claude-i"]
    finally:
        path.unlink(missing_ok=True)


def test_codex_mirrors_all_rows_and_updates_without_mirroring_claude():
    from pathlib import Path

    now = datetime.now(timezone.utc)
    path = Path("tests") / f".agent-test-{uuid4().hex}.db"
    try:
        with SQLiteAgentStore(path) as store:
            store.create(AgentSession(session_id="cx", agent=AgentKind.CODEX, name="codex", conversation_id="conv", cwd="/old", status=SessionStatus.RUNNING, created_at=now, updated_at=now))
            store.create(AgentSession(session_id="cl", agent=AgentKind.CLAUDE, name="claude", status=SessionStatus.RUNNING, created_at=now, updated_at=now))
            store.create_interaction(AgentInteraction(interaction_id="ci", session_id="cx", request_id="cr", kind=InteractionKind.EXEC_APPROVAL, payload_summary="payload", requested_at=now, expires_at=now + timedelta(minutes=1)))
            store.create_interaction(AgentInteraction(interaction_id="li", session_id="cl", request_id="lr", kind=InteractionKind.EXEC_APPROVAL, payload_summary="claude-secret", requested_at=now, expires_at=now + timedelta(minutes=1)))
            assert store.record_event_once("event", agent=AgentKind.CODEX)
            assert store.record_event_once("event", agent=AgentKind.CLAUDE)
            outbox_id = store.enqueue_outbox(notification_type="notice", payload_summary="outbox", session_id="cx", agent=AgentKind.CODEX, created_at=now)
            claude_outbox_id = store.enqueue_outbox(notification_type="notice", payload_summary="claude-outbox", session_id="cl", agent=AgentKind.CLAUDE, created_at=now)
            audit_id = store.record_audit(event_type="started", detail_summary="audit", session_id="cx", created_at=now, agent=AgentKind.CODEX)
            sessionless_id = store.record_audit(event_type="sessionless", detail_summary="sessionless", created_at=now, agent=AgentKind.CODEX)
            store.record_audit(event_type="claude", detail_summary="claude-audit", created_at=now, agent=AgentKind.CLAUDE)
            with store._connection() as connection:
                connection.execute("UPDATE agent_sessions SET name=?,cwd=?,updated_at=? WHERE id='cx'", ("codex-updated", "/new", now.isoformat()))
                connection.execute("UPDATE agent_interactions SET payload_summary=?,actor_id=?,decision=?,status=? WHERE id='ci'", ("payload-updated", "actor", "approved", "resolved"))
                connection.execute("UPDATE agent_notification_outbox SET payload_summary=?,last_error=?,attempt_count=? WHERE id=?", ("outbox-updated", "err", 2, outbox_id))
                connection.execute("UPDATE agent_audit SET detail_summary=? WHERE id=?", ("audit-updated", audit_id))
                rows = {
                    name: connection.execute(f"SELECT * FROM {name}").fetchall()
                    for name in ("codex_sessions", "codex_interactions", "codex_event_dedupe", "notification_outbox", "codex_audit")
                }
            assert rows["codex_sessions"][0][0:4] == ("cx", "conv", None, "codex-updated")
            assert rows["codex_interactions"][0][0] == "ci" and rows["codex_interactions"][0][6] == "payload-updated"
            assert {row[0] for row in rows["codex_event_dedupe"]} == {"event"}
            assert any(row[0] == outbox_id and row[4] == "outbox-updated" for row in rows["notification_outbox"])
            assert {row[0] for row in rows["codex_audit"]} >= {audit_id, sessionless_id}
            assert all("claude" not in str(row) for table in rows.values() for row in table)
            assert claude_outbox_id not in {row[0] for row in rows["notification_outbox"]}
    finally:
        path.unlink(missing_ok=True)


def test_agent_transition_removes_codex_legacy_rows_and_privacy_holds():
    from pathlib import Path

    now = datetime.now(timezone.utc)
    secret = "token=raw-user-secret-123"
    path = Path("tests") / f".agent-test-{uuid4().hex}.db"
    try:
        with SQLiteAgentStore(path) as store:
            store.create(AgentSession(session_id="s", agent=AgentKind.CODEX, name="name", status=SessionStatus.RUNNING, created_at=now, updated_at=now, summary=secret))
            store.create_interaction(AgentInteraction(interaction_id="i", session_id="s", request_id="r", kind=InteractionKind.USER_INPUT, payload_summary=secret, requested_at=now, expires_at=now))
            assert store.resolve_interaction("i", decision=secret, actor_id="u", resolved_at=now)
            outbox_id = store.enqueue_outbox(notification_type="n", payload_summary=secret, session_id="s", interaction_id="i", agent=AgentKind.CODEX, created_at=now)
            store.record_outbox_failure(outbox_id, error=secret, next_attempt_at=now)
            store.record_audit(event_type="sessionless", detail_summary=secret, agent=AgentKind.CODEX, created_at=now)
            with store._connection() as connection:
                text_fields = connection.execute("SELECT summary FROM agent_sessions UNION ALL SELECT payload_summary FROM agent_interactions UNION ALL SELECT payload_summary FROM agent_notification_outbox UNION ALL SELECT last_error FROM agent_notification_outbox UNION ALL SELECT detail_summary FROM agent_audit").fetchall()
                assert all(secret not in (row[0] or "") for row in text_fields)
                connection.execute("UPDATE agent_sessions SET agent='claude' WHERE id='s'")
                assert connection.execute("SELECT COUNT(*) FROM codex_sessions WHERE id='s'").fetchone()[0] == 0
                assert connection.execute("SELECT COUNT(*) FROM codex_interactions WHERE session_id='s'").fetchone()[0] == 0
                assert connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE session_id='s'").fetchone()[0] == 0
                assert connection.execute("SELECT COUNT(*) FROM codex_audit WHERE session_id='s'").fetchone()[0] == 0
    finally:
        path.unlink(missing_ok=True)
