from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from lark_bot.core.redaction import redact_text
from lark_bot.modules.agent.agent_model import AgentInteraction, AgentSession

SUMMARY_LIMIT = 2000


def serialize_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def serialize_optional_datetime(value: datetime | None) -> str | None:
    return serialize_datetime(value) if value is not None else None


def safe_summary(value: str | None) -> str:
    return redact_text(value or "")[:SUMMARY_LIMIT]


def session_from_row(row: sqlite3.Row) -> AgentSession:
    return AgentSession(
        session_id=row["id"], agent=row["agent"], name=row["name"],
        conversation_id=row["conversation_id"], turn_id=row["turn_id"], cwd=row["cwd"],
        model=row["model"], sandbox=row["sandbox"], permission_mode=row["permission_mode"],
        status=row["status"], summary=row["summary"],
        created_at=datetime.fromisoformat(row["created_at"]), updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def interaction_from_row(row: sqlite3.Row) -> AgentInteraction:
    return AgentInteraction(
        interaction_id=row["id"], session_id=row["session_id"], request_id=row["request_id"],
        kind=row["kind"], status=row["status"], lark_message_id=row["lark_message_id"],
        payload_summary=row["payload_summary"], requested_at=datetime.fromisoformat(row["requested_at"]),
        resolved_at=(datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None),
        expires_at=(datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None),
        actor_id=row["actor_id"], decision=row["decision"],
    )


def session_values(session: AgentSession) -> tuple[object, ...]:
    return (
        session.session_id, session.agent.value, session.name, session.conversation_id,
        session.turn_id, session.cwd, session.model, session.sandbox, session.permission_mode,
        session.status.value, safe_summary(session.summary), serialize_datetime(session.created_at),
        serialize_datetime(session.updated_at),
    )


def interaction_values(interaction: AgentInteraction) -> tuple[object, ...]:
    return (
        interaction.interaction_id, interaction.session_id, interaction.request_id,
        interaction.kind.value, interaction.status.value, interaction.lark_message_id,
        safe_summary(interaction.payload_summary), serialize_datetime(interaction.requested_at),
        serialize_optional_datetime(interaction.resolved_at), serialize_optional_datetime(interaction.expires_at),
        interaction.actor_id, safe_summary(interaction.decision) if interaction.decision else None,
    )
