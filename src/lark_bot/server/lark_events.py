from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.post("/lark/events")
async def lark_events(request: Request) -> dict[str, object]:
    payload = await request.json()
    if "challenge" in payload:
        return {"challenge": payload["challenge"]}
    return {"ok": True, "handled": False}
