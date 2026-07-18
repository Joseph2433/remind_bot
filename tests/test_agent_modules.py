from datetime import datetime, timezone
from importlib import import_module


def test_agent_module_exports_session_and_event_contracts() -> None:
    model = import_module("lark_bot.modules.agent.agent_model")
    event = import_module("lark_bot.modules.agent.agent_event")
    protocol = import_module("lark_bot.modules.agent.agent_protocol")

    assert model.AgentKind.CODEX.value == "codex"
    assert model.AgentSession
    assert event.AgentEvent
    assert protocol.AgentAdapter


def test_agent_session_keeps_provider_conversation_id_separate() -> None:
    model = import_module("lark_bot.modules.agent.agent_model")
    session = model.AgentSession(
        session_id="session-1",
        agent=model.AgentKind.CODEX,
        name="build",
        conversation_id="thread-1",
        status=model.SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    assert session.session_id == "session-1"
    assert session.conversation_id == "thread-1"


def test_agent_event_requires_session_identity() -> None:
    model = import_module("lark_bot.modules.agent.agent_model")
    event = import_module("lark_bot.modules.agent.agent_event")
    value = event.AgentEvent(
        session=model.SessionRef(
            session_id="session-1",
            name="build",
            agent=model.AgentKind.CODEX,
        ),
        event_type="session_completed",
        status=model.SessionStatus.SUCCEEDED,
        summary="done",
    )

    assert value.session.session_id == "session-1"
