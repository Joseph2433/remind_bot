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
