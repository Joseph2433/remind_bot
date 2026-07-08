from __future__ import annotations

from fastapi import FastAPI

from lack_bot.server.agent_events import router as agent_events_router
from lack_bot.server.lark_events import router as lark_events_router


def create_app() -> FastAPI:
    app = FastAPI(title="Lack Bot", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(lark_events_router)
    app.include_router(agent_events_router)
    return app


app = create_app()
