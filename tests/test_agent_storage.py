from datetime import datetime, timezone

from lark_bot.modules.agent.agent_model import AgentKind, AgentInteraction, AgentSession, InteractionKind, SessionStatus
from lark_bot.modules.agent.agent_store import SQLiteAgentStore
from lark_bot.modules.agent import agent_schema


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
