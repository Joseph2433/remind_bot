from __future__ import annotations

from typing import Any

from lark_bot.modules.agent.agent_model import AgentKind, AgentSession, SessionStatus


class CodexService:
    """AgentAdapter facade over the existing Codex orchestrator."""

    agent = AgentKind.CODEX

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    async def start(self) -> None:
        await self.orchestrator.start()

    async def close(self) -> None:
        await self.orchestrator.close()

    async def create_session(
        self,
        name: str,
        cwd: str,
        prompt: str,
        **options: Any,
    ) -> AgentSession:
        session = await self.orchestrator.create_session(
            name,
            cwd,
            prompt,
            model=options.get("model"),
            sandbox=options.get("sandbox", "workspace-write"),
        )
        return AgentSession(
            session_id=session.id,
            agent=AgentKind.CODEX,
            name=session.name,
            conversation_id=session.thread_id,
            status=SessionStatus(session.status.value),
            summary=session.summary,
            created_at=session.created_at,
            updated_at=session.updated_at,
        )

    async def cancel_session(self, session_id: str) -> bool:
        return await self.orchestrator.cancel_session(session_id)

    async def list_sessions(self, status: SessionStatus | None = None) -> list[AgentSession]:
        method = getattr(self.orchestrator, "list_sessions", None)
        if method is not None:
            values = method(status)
            return await values if hasattr(values, "__await__") else values
        store = getattr(self.orchestrator, "_store", None)
        values = store.list_sessions(status) if store is not None else []
        return [
            AgentSession(
                session_id=value.id,
                agent=AgentKind.CODEX,
                name=value.name,
                conversation_id=value.thread_id,
                turn_id=value.turn_id,
                cwd=value.cwd,
                model=value.model,
                sandbox=value.sandbox,
                status=SessionStatus(value.status.value),
                summary=value.summary,
                created_at=value.created_at,
                updated_at=value.updated_at,
            )
            for value in values
        ]

    async def get_session(self, session_id: str) -> AgentSession | None:
        method = getattr(self.orchestrator, "get_session", None)
        if method is not None:
            value = method(session_id)
            return await value if hasattr(value, "__await__") else value
        sessions = await self.list_sessions()
        return next((session for session in sessions if session.session_id == session_id), None)

    async def resolve_interaction(self, interaction_id: str, actor_id: str, **kwargs: Any) -> bool:
        return await self.orchestrator.resolve_interaction(interaction_id, actor_id, **kwargs)

    def get_user_input_question_ids(self, interaction_id: str) -> tuple[str, ...]:
        method = getattr(self.orchestrator, "get_user_input_question_ids", None)
        return tuple(method(interaction_id)) if method is not None else ()

    async def expire_due_interactions(self, now: Any = None) -> list[str]:
        method = getattr(self.orchestrator, "expire_due_interactions")
        return await method(now)
