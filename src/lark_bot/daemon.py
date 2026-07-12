from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from lark_bot.codex_app_server import CodexAppServerClient, ProcessExitedError, ServerRpcError
from lark_bot.codex_models import CodexSession, InteractionKind, SessionStatus
from lark_bot.codex_orchestrator import CodexOrchestrator
from lark_bot.lark_control import LarkControlRouter, LarkLongConnection
from lark_bot.notifier.lark import LarkBotClient
from lark_bot.redaction import redact_text
from lark_bot.storage.codex_sqlite import SQLiteCodexStore

MAX_HOOK_BYTES = 64 * 1024


async def _read_bounded_body(request: Request, limit: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > limit:
        raise HTTPException(status_code=413, detail="payload too large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status_code=413, detail="payload too large")
    return bytes(body)


def ensure_daemon_token(path: str | Path) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise RuntimeError("daemon token file must not be a symlink")
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(secrets.token_urlsafe(32))
    try:
        os.chmod(target, 0o600)
        if os.name != "nt" and target.stat().st_mode & 0o077:
            raise RuntimeError("daemon token file permissions are insecure")
        token = target.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError("daemon token file is unreadable") from error
    if len(token) < 32:
        raise RuntimeError("daemon token file is empty or insecure")
    return token


class SessionCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    cwd: str = Field(min_length=1, max_length=4096)
    prompt: str = Field(min_length=1, max_length=1_000_000, repr=False)
    model: str | None = Field(default=None, max_length=200)
    sandbox: str = Field(default="workspace-write", pattern="^(read-only|workspace-write)$")


def _public_session(session: CodexSession) -> dict[str, Any]:
    return session.model_dump(mode="json")


class DaemonRuntime:
    def __init__(self, settings: Any, store: Any, orchestrator: Any, lark_client: Any, long_connection: Any, control_router: Any) -> None:
        self.settings, self.store, self.orchestrator = settings, store, orchestrator
        self.lark_client, self.long_connection, self.control_router = lark_client, long_connection, control_router
        self._tasks: list[asyncio.Task[None]] = []
        self._closed = False
        self.degraded_reason: str | None = None

    async def start(self) -> None:
        try:
            self._drain_spool()
            await self.orchestrator.start()
            await self.long_connection.start()
            self._tasks = [asyncio.create_task(self._route_lark(), name="lark-event-router"), asyncio.create_task(self._outbox_worker(), name="notification-outbox")]
            self._tasks.append(
                asyncio.create_task(
                    self._interaction_expiry_worker(),
                    name="interaction-expiry",
                )
            )
        except BaseException:
            await self.close()
            raise

    async def _route_lark(self) -> None:
        try:
            while True:
                try:
                    event = await asyncio.wait_for(self.long_connection.events.get(), timeout=0.5)
                except TimeoutError:
                    if self.long_connection.terminal_error is not None:
                        raise self.long_connection.terminal_error
                    continue
                await self.control_router.route(event)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            self.degraded_reason = f"Lark event routing unavailable ({type(error).__name__})"

    def _drain_spool(self) -> None:
        spool = Path(self.settings.daemon_token_path).parent / "spool"
        if not spool.is_dir():
            return
        for path in sorted(spool.glob("hook-*.json")):
            try:
                if path.is_symlink() or path.stat().st_size > MAX_HOOK_BYTES:
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    continue
                name = payload.get("hook_event_name")
                if name not in {"SessionStart", "PermissionRequest", "Stop"}:
                    continue
                event_id = payload.get("event_id")
                if isinstance(event_id, str) and event_id and not self.store.record_event_once(f"hook:{event_id}"):
                    path.unlink()
                    continue
                self.store.enqueue_outbox(notification_type=f"hook:{name}", payload_summary=f"Codex hook {name}")
                path.unlink()
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue

    async def _outbox_worker(self) -> None:
        poll = float(self.settings.outbox_poll_seconds)
        while True:
            for item in self.store.list_due_outbox(now=datetime.now(timezone.utc), limit=50):
                try:
                    message_id = await asyncio.to_thread(self.lark_client.send_text, self._render(item))
                    if item.interaction_id:
                        self.store.attach_lark_message_id(item.interaction_id, message_id)
                    self.store.mark_outbox_sent(item.id)
                except asyncio.CancelledError:
                    raise
                except BaseException as error:
                    delay = min(300.0, poll * (2 ** min(item.attempt_count + 1, 10)))
                    self.store.record_outbox_failure(item.id, error=f"send failed ({type(error).__name__})", next_attempt_at=datetime.now(timezone.utc) + timedelta(seconds=delay))
            try:
                await asyncio.wait_for(self.orchestrator.events.get(), timeout=poll)
            except TimeoutError:
                pass

    async def _interaction_expiry_worker(self) -> None:
        poll = float(getattr(self.settings, "interaction_expiry_poll_seconds", 1.0))
        reported_error: str | None = None
        while True:
            try:
                await self.orchestrator.expire_due_interactions()
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                safe_error = f"Interaction expiry unavailable ({type(error).__name__})"
                self.degraded_reason = safe_error
                if reported_error != safe_error:
                    self.store.enqueue_outbox(
                        notification_type="runtime:degraded",
                        payload_summary=safe_error,
                    )
                    reported_error = safe_error
            await asyncio.sleep(poll)

    def _render(self, item: Any) -> str:
        summary = redact_text(str(item.payload_summary))[:500]
        heading = str(item.notification_type).replace("orchestrator:", "Codex ").replace("_", " ")
        text = f"{heading}\n{summary}"
        if item.notification_type.endswith("interaction_requested"):
            getter = getattr(self.store, "get_interaction", None)
            interaction = getter(item.interaction_id) if getter is not None and item.interaction_id else None
            if interaction is not None and interaction.kind is InteractionKind.USER_INPUT:
                text += "\nReply to this message and @Bot. For multiple questions, use `1: answer` on separate lines."
            else:
                text += "\nReact 👍 to approve or 👎 to deny."
        return text

    async def close(self) -> None:
        if self._closed: return
        self._closed = True
        for task in self._tasks: task.cancel()
        if self._tasks: await asyncio.gather(*self._tasks, return_exceptions=True)
        for close in (self.long_connection.close, self.orchestrator.close):
            try: await close()
            except BaseException: pass
        try: self.lark_client.close()
        finally: self.store.close()


def create_daemon_app(runtime: DaemonRuntime, *, token: str) -> FastAPI:
    async def authenticate(authorization: str | None = Header(default=None)) -> None:
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.casefold() != "bearer" or not hmac.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="unauthorized")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await runtime.start()
        try: yield
        finally: await runtime.close()

    app = FastAPI(title="Lark Bot daemon", lifespan=lifespan)

    @app.get("/health")
    async def health():
        codex_error = getattr(runtime.orchestrator, "terminal_error", None)
        degraded = runtime.degraded_reason or (f"Codex unavailable ({type(codex_error).__name__})" if codex_error else None)
        return {"status": "degraded" if degraded else "ok", "codex": "degraded" if codex_error else "ready", "lark": runtime.degraded_reason or "ready"}

    prefix = "/api/v1/codex"

    @app.post(prefix + "/sessions", status_code=201, dependencies=[Depends(authenticate)])
    async def create_session(request: SessionCreate):
        try:
            session = await runtime.orchestrator.create_session(request.name, request.cwd, request.prompt, request.model, request.sandbox)
        except (ProcessExitedError, ServerRpcError, RuntimeError):
            raise HTTPException(status_code=502, detail="Codex app-server unavailable") from None
        return _public_session(session)

    @app.get(prefix + "/sessions", dependencies=[Depends(authenticate)])
    async def list_sessions(status: SessionStatus | None = None):
        return [_public_session(item) for item in runtime.store.list_sessions(status)]

    @app.get(prefix + "/sessions/{session_id}", dependencies=[Depends(authenticate)])
    async def get_session(session_id: str):
        session = runtime.store.get_session(session_id)
        if session is None: raise HTTPException(status_code=404, detail="session not found")
        return _public_session(session)

    @app.post(prefix + "/sessions/{session_id}/cancel", dependencies=[Depends(authenticate)])
    async def cancel_session(session_id: str):
        if runtime.store.get_session(session_id) is None: raise HTTPException(status_code=404, detail="session not found")
        if not await runtime.orchestrator.cancel_session(session_id): raise HTTPException(status_code=409, detail="session is not cancellable")
        return {"cancelled": True}

    @app.post(prefix + "/hooks", status_code=202, dependencies=[Depends(authenticate)])
    async def ingest_hook(request: Request):
        body = await _read_bounded_body(request, MAX_HOOK_BYTES)
        try: payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError): raise HTTPException(status_code=400, detail="invalid JSON") from None
        if not isinstance(payload, dict): raise HTTPException(status_code=400, detail="JSON object required")
        name = next((payload.get(key) for key in ("hook_event_name", "event_name", "hook_name") if isinstance(payload.get(key), str)), None)
        if name not in {"SessionStart", "PermissionRequest", "Stop"}: raise HTTPException(status_code=422, detail="unsupported hook event")
        event_id = payload.get("event_id")
        if isinstance(event_id, str) and event_id and not runtime.store.record_event_once(f"hook:{event_id}"):
            return {"accepted": True, "duplicate": True}
        runtime.store.enqueue_outbox(notification_type=f"hook:{name}", payload_summary=f"Codex hook {name}")
        return {"accepted": True}
    return app


def build_runtime(settings: Any) -> DaemonRuntime:
    store = SQLiteCodexStore(settings.sqlite_path)
    app_server = CodexAppServerClient(codex_path=settings.codex_path)
    orchestrator = CodexOrchestrator(store, app_server, now=lambda: datetime.now(timezone.utc), id_factory=lambda: str(uuid.uuid4()), interaction_timeout_seconds=settings.interaction_timeout_seconds)
    lark = LarkBotClient(app_id=settings.lark_app_id, app_secret=settings.lark_app_secret, receive_id=settings.lark_receive_id, receive_id_type=settings.lark_receive_id_type, base_url=settings.lark_base_url, timeout_seconds=settings.http_timeout_seconds)
    connection = LarkLongConnection(settings.lark_app_id, settings.lark_app_secret, queue_capacity=settings.lark_event_queue_capacity)
    return DaemonRuntime(settings, store, orchestrator, lark, connection, LarkControlRouter(store, orchestrator))
