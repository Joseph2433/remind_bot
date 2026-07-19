from __future__ import annotations

import asyncio
import hmac
import json
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from lark_bot.modules.codex.app_server import CodexAppServerClient, ProcessExitedError, ServerRpcError
from lark_bot.modules.codex.codex_interactive import InteractiveSessionManager
from lark_bot.modules.codex.codex_model import CodexSession, SessionStatus
from lark_bot.modules.codex.codex_orchestrator import CodexOrchestrator
from lark_bot.modules.codex.codex_service import CodexService
from lark_bot.modules.claude.claude_service import ClaudeService
from lark_bot.modules.claude.claude_session_manager import ClaudeSessionManager
from lark_bot.modules.claude.claude_sdk import ClaudeAgentSdkBridge
from lark_bot.modules.agent.agent_service import AgentRegistry, AgentInteractionDispatcher
from lark_bot.modules.agent.agent_model import AgentKind, SessionDisplay
from lark_bot.modules.agent.agent_store import SQLiteAgentStore
from lark_bot.modules.lark.lark_client import LarkBotClient
from lark_bot.modules.lark.lark_connection import LarkLongConnection
from lark_bot.modules.lark.lark_message import MessageFormat, RenderedMessage
from lark_bot.modules.lark.lark_render import render_outbox_notification
from lark_bot.modules.lark.lark_router import LarkControlRouter
from lark_bot.modules.codex.codex_store import SQLiteCodexStore

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


class SessionCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    cwd: str = Field(min_length=1, max_length=4096)
    prompt: str = Field(min_length=1, max_length=1_000_000, repr=False)
    model: str | None = Field(default=None, max_length=200)
    sandbox: str = Field(default="workspace-write", pattern="^(read-only|workspace-write)$")


class AgentSessionCreate(BaseModel):
    """Provider-neutral managed-session request.

    The prompt is accepted for dispatch but deliberately excluded from repr and
    all response models so accidental logging cannot disclose it.
    """

    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    cwd: str = Field(min_length=1, max_length=4096)
    prompt: str = Field(min_length=1, max_length=1_000_000, repr=False)
    model: str | None = Field(default=None, max_length=200)
    sandbox: str = Field(default="workspace-write", pattern="^(read-only|workspace-write)$")
    permission_mode: str | None = Field(default=None, max_length=100)
    resume_id: str | None = Field(default=None, max_length=200)


class InteractiveSessionCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(default="interactive", min_length=1, max_length=200)
    cwd: str = Field(min_length=1, max_length=4096)
    model: str | None = Field(default=None, max_length=200)
    sandbox: str = Field(default="workspace-write", pattern="^(read-only|workspace-write)$")


def _public_session(session: CodexSession) -> dict[str, Any]:
    return session.model_dump(mode="json")


def _public_agent_session(session: Any) -> dict[str, Any]:
    """Serialize a provider session without accepting arbitrary provider data."""

    if hasattr(session, "model_dump"):
        value = session.model_dump(mode="json")
    else:
        value = {
            key: getattr(session, key)
            for key in (
                "session_id",
                "agent",
                "name",
                "conversation_id",
                "turn_id",
                "cwd",
                "model",
                "sandbox",
                "permission_mode",
                "status",
                "summary",
                "created_at",
                "updated_at",
            )
            if hasattr(session, key)
        }
    value.pop("prompt", None)
    return value


def _agent_kind(value: str) -> AgentKind:
    try:
        return AgentKind(value)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid agent") from None


def _adapter_for(runtime: "DaemonRuntime", value: str) -> tuple[AgentKind, Any]:
    agent = _agent_kind(value)
    registry = runtime.agent_registry
    if registry is None:
        raise HTTPException(status_code=404, detail="agent not configured")
    try:
        return agent, registry.get(agent)
    except (KeyError, ValueError):
        raise HTTPException(status_code=404, detail="agent not configured") from None


def _record_event(store: Any, event_id: str, agent: AgentKind) -> bool:
    try:
        return bool(store.record_event_once(event_id, agent=agent))
    except TypeError:
        return bool(store.record_event_once(event_id))


class DaemonRuntime:
    def __init__(self, settings: Any, store: Any, orchestrator: Any, lark_client: Any, long_connection: Any, control_router: Any, *, interactive_manager: Any | None = None, agent_registry: AgentRegistry | None = None, now: Callable[[], datetime] | None = None) -> None:
        self.settings, self.store, self.orchestrator = settings, store, orchestrator
        self.lark_client, self.long_connection, self.control_router = lark_client, long_connection, control_router
        self.interactive_manager = interactive_manager
        self.agent_registry = agent_registry
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._tasks: list[asyncio.Task[None]] = []
        self._closed = False
        self.degraded_reason: str | None = None
        self.provider_errors: dict[AgentKind, str] = {}
        self.lark_error: str | None = None

    async def start(self) -> None:
        try:
            self._drain_spool()
            if self.agent_registry is None:
                try:
                    await self.orchestrator.start()
                except BaseException as error:
                    self.provider_errors[AgentKind.CODEX] = f"Codex unavailable ({type(error).__name__})"
            else:
                for agent in self.agent_registry.registered():
                    try:
                        await self.agent_registry.get(agent).start()
                    except asyncio.CancelledError:
                        raise
                    except BaseException as error:
                        self.provider_errors[agent] = f"{agent.value.title()} unavailable ({type(error).__name__})"
            if self.interactive_manager is not None:
                try:
                    await self.interactive_manager.start()
                except BaseException as error:
                    self.provider_errors[AgentKind.CODEX] = f"Codex interactive unavailable ({type(error).__name__})"
            try:
                await self.long_connection.start()
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                self.lark_error = f"Lark unavailable ({type(error).__name__})"
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
            self.lark_error = f"Lark event routing unavailable ({type(error).__name__})"
            self.degraded_reason = self.degraded_reason or self.lark_error

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
                agent_value = payload.get("agent", "codex")
                try:
                    agent = AgentKind(agent_value)
                except (TypeError, ValueError):
                    continue
                safe_keys = (
                    {"agent", "hook_event_name", "event_id", "callback_type", "thread_id"}
                    if agent is AgentKind.CODEX
                    else {"agent", "hook_event_name", "event_id", "session_id", "prompt_id", "source", "reason", "notification_type", "title", "error"}
                )
                if any(key not in safe_keys for key in payload):
                    continue
                allowed = {"SessionStart", "PermissionRequest", "Stop"} if agent is AgentKind.CODEX else {
                    "SessionStart", "Notification", "PermissionRequest", "UserPromptSubmit", "Stop", "StopFailure", "SessionEnd",
                }
                if name not in allowed:
                    continue
                if "event_id" in payload and (not isinstance(event_id, str) or not event_id or len(event_id) > 200):
                    continue
                if isinstance(payload.get("agent"), str) and payload["agent"] != agent.value:
                    continue
                if isinstance(event_id, str) and event_id and not _record_event(self.store, f"hook:{event_id}" if agent is AgentKind.CODEX else f"{agent.value}:hook:{event_id}", agent):
                    path.unlink()
                    continue
                notification_type = f"hook:{name}" if agent is AgentKind.CODEX else f"{agent.value}:hook:{name}"
                self.enqueue_notification(
                    notification_type=notification_type,
                    payload_summary=f"{agent.value.title()} hook {name}",
                    agent=agent,
                )
                path.unlink()
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue

    async def _outbox_worker(self) -> None:
        poll = float(self.settings.outbox_poll_seconds)
        while True:
            try:
                items = self.store.list_due_outbox(now=self._utc_now(), limit=50)
            except TypeError:
                items = self.store.list_due_outbox(now=self._utc_now(), limit=50, agent=None)
            for item in items:
                try:
                    rendered = self._render(item)
                    message_id = await asyncio.to_thread(self.lark_client.send_rendered, rendered)
                    if item.interaction_id:
                        self._store_call("attach_lark_message_id", item.interaction_id, message_id, item=item)
                    self._store_call("mark_outbox_sent", item.id, item=item)
                except asyncio.CancelledError:
                    raise
                except BaseException as error:
                    delay = min(300.0, poll * (2 ** min(item.attempt_count + 1, 10)))
                    self._store_call(
                        "record_outbox_failure",
                        item.id,
                        error=f"send failed ({type(error).__name__})",
                        next_attempt_at=self._utc_now() + timedelta(seconds=delay),
                        item=item,
                    )
            await asyncio.sleep(poll)

    async def _interaction_expiry_worker(self) -> None:
        poll = float(getattr(self.settings, "interaction_expiry_poll_seconds", 1.0))
        reported_errors: dict[AgentKind, str] = {}
        while True:
            adapters = (
                [(agent, self.agent_registry.get(agent)) for agent in self.agent_registry.registered()]
                if self.agent_registry is not None
                else [(AgentKind.CODEX, self.orchestrator)]
            )
            for agent, adapter in adapters:
                try:
                    try:
                        await adapter.expire_due_interactions(self._utc_now())
                    except TypeError:
                        await adapter.expire_due_interactions()
                except asyncio.CancelledError:
                    raise
                except BaseException as error:
                    safe_error = f"{agent.value.title()} interaction expiry unavailable ({type(error).__name__})"
                    self.provider_errors[agent] = safe_error
                    if self.degraded_reason is None or agent is AgentKind.CODEX:
                        self.degraded_reason = safe_error if agent is not AgentKind.CODEX else f"Interaction expiry unavailable ({type(error).__name__})"
                    if reported_errors.get(agent) != safe_error:
                        payload_summary = (
                            f"Interaction expiry unavailable ({type(error).__name__})"
                            if agent is AgentKind.CODEX
                            else safe_error
                        )
                        self.enqueue_notification(notification_type="runtime:degraded", payload_summary=payload_summary, agent=agent)
                        reported_errors[agent] = safe_error
            await asyncio.sleep(poll)

    def enqueue_notification(self, **kwargs: Any) -> int:
        created_at = self._utc_now()
        delay = float(self.settings.notification_delay_seconds)
        return self.store.enqueue_outbox(
            **kwargs,
            created_at=created_at,
            next_attempt_at=created_at + timedelta(seconds=delay),
        )

    def _store_call(self, method: str, *args: Any, item: Any | None = None, **kwargs: Any) -> Any:
        function = getattr(self.store, method)
        agent = getattr(item, "agent", None) if item is not None else kwargs.pop("agent", None)
        if agent is not None:
            try:
                return function(*args, agent=agent, **kwargs)
            except TypeError:
                pass
        return function(*args, **kwargs)

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _render(self, item: Any) -> RenderedMessage:
        interaction = None
        if str(item.notification_type).endswith("interaction_requested"):
            getter = getattr(self.store, "get_interaction", None)
            if getter is not None and item.interaction_id:
                interaction = getter(item.interaction_id)
        session_display = None
        session_id = getattr(item, "session_id", None)
        if session_id:
            try:
                session = self.store.get_session(session_id, agent=getattr(item, "agent", None))
            except TypeError:
                session = self.store.get_session(session_id)
            if session is not None:
                session_display = SessionDisplay(
                    session_id=session_id,
                    session_name=session.name,
                    agent=getattr(item, "agent", None) or AgentKind.CODEX,
                )
        message_format: MessageFormat = getattr(self.settings, "message_format", "card")
        if message_format not in {"card", "text"}:
            message_format = "card"
        return render_outbox_notification(
            item,
            message_format=message_format,
            interaction=interaction,
            session=session_display,
        )

    async def close(self) -> None:
        if self._closed: return
        self._closed = True
        for task in self._tasks: task.cancel()
        if self._tasks: await asyncio.gather(*self._tasks, return_exceptions=True)
        closes = [self.long_connection.close]
        if self.interactive_manager is not None:
            closes.append(self.interactive_manager.close)
        if self.agent_registry is None:
            closes.append(self.orchestrator.close)
        else:
            closes.extend(self.agent_registry.get(agent).close for agent in self.agent_registry.registered())
        for close in closes:
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
        codex_degraded = codex_error is not None or AgentKind.CODEX in runtime.provider_errors
        claude_degraded = AgentKind.CLAUDE in runtime.provider_errors
        lark_degraded = runtime.lark_error is not None
        details = {
            agent.value: error
            for agent, error in runtime.provider_errors.items()
        }
        if runtime.lark_error:
            details["lark"] = runtime.lark_error
        return {
            "status": "degraded" if (codex_degraded or claude_degraded or lark_degraded) else "ok",
            "codex": "degraded" if codex_degraded else "ready",
            "claude": "degraded" if claude_degraded else "ready",
            "lark": "degraded" if lark_degraded else "ready",
            "details": details,
        }

    prefix = "/api/v1/codex"

    @app.post(prefix + "/sessions", status_code=201, dependencies=[Depends(authenticate)])
    async def create_session(request: SessionCreate):
        try:
            session = await runtime.orchestrator.create_session(request.name, request.cwd, request.prompt, request.model, request.sandbox)
        except (ProcessExitedError, ServerRpcError, RuntimeError):
            raise HTTPException(status_code=502, detail="Codex app-server unavailable") from None
        return _public_session(session)

    @app.post(prefix + "/interactive-sessions", status_code=201, dependencies=[Depends(authenticate)])
    async def create_interactive_session(request: InteractiveSessionCreate):
        manager = runtime.interactive_manager
        if manager is None:
            raise HTTPException(
                status_code=503,
                detail="interactive Codex sessions are not configured",
            )
        try:
            descriptor = await manager.create_session(
                name=request.name,
                cwd=request.cwd,
                model=request.model,
                sandbox=request.sandbox,
            )
        except (OSError, RuntimeError):
            raise HTTPException(
                status_code=502,
                detail="interactive Codex app-server unavailable",
            ) from None
        return {
            "session_id": descriptor.session_id,
            "endpoint": descriptor.endpoint,
            "remote_auth_token": descriptor.remote_auth_token,
        }

    @app.delete(prefix + "/interactive-sessions/{session_id}", status_code=204, dependencies=[Depends(authenticate)])
    async def close_interactive_session(session_id: str):
        manager = runtime.interactive_manager
        if manager is None:
            raise HTTPException(
                status_code=503,
                detail="interactive Codex sessions are not configured",
            )
        if not await manager.close_session(session_id):
            raise HTTPException(status_code=404, detail="interactive session not found")
        return Response(status_code=204)

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

    @app.post("/api/v1/agents/{agent}/sessions", status_code=201, dependencies=[Depends(authenticate)])
    async def create_agent_session(agent: str, request: AgentSessionCreate):
        _, adapter = _adapter_for(runtime, agent)
        try:
            session = await adapter.create_session(
                request.name,
                request.cwd,
                request.prompt,
                model=request.model,
                sandbox=request.sandbox,
                permission_mode=request.permission_mode,
                resume_id=request.resume_id,
            )
        except (ProcessExitedError, ServerRpcError, RuntimeError, OSError, ModuleNotFoundError):
            raise HTTPException(status_code=502, detail="agent unavailable") from None
        return _public_agent_session(session)

    @app.get("/api/v1/agents/{agent}/sessions", dependencies=[Depends(authenticate)])
    async def list_agent_sessions(agent: str, status: SessionStatus | None = None):
        _, adapter = _adapter_for(runtime, agent)
        try:
            values = await adapter.list_sessions(status)
        except (RuntimeError, OSError):
            raise HTTPException(status_code=503, detail="agent unavailable") from None
        return [_public_agent_session(item) for item in values]

    @app.get("/api/v1/agents/{agent}/sessions/{session_id}", dependencies=[Depends(authenticate)])
    async def get_agent_session(agent: str, session_id: str):
        _, adapter = _adapter_for(runtime, agent)
        try:
            session = await adapter.get_session(session_id)
        except (RuntimeError, OSError):
            raise HTTPException(status_code=503, detail="agent unavailable") from None
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        return _public_agent_session(session)

    @app.post("/api/v1/agents/{agent}/sessions/{session_id}/cancel", dependencies=[Depends(authenticate)])
    async def cancel_agent_session(agent: str, session_id: str):
        _, adapter = _adapter_for(runtime, agent)
        try:
            session = await adapter.get_session(session_id)
        except (RuntimeError, OSError):
            raise HTTPException(status_code=503, detail="agent unavailable") from None
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        try:
            cancelled = await adapter.cancel_session(session_id)
        except (RuntimeError, OSError):
            raise HTTPException(status_code=503, detail="agent unavailable") from None
        if not cancelled:
            raise HTTPException(status_code=409, detail="session is not cancellable")
        return {"cancelled": True}

    @app.post("/api/v1/agents/{agent}/hooks", status_code=202, dependencies=[Depends(authenticate)])
    async def ingest_agent_hook(agent: str, request: Request):
        provider, _ = _adapter_for(runtime, agent)
        body = await _read_bounded_body(request, MAX_HOOK_BYTES)
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(status_code=400, detail="invalid JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="JSON object required")
        supplied_agent = payload.get("agent")
        if supplied_agent is not None and supplied_agent != provider.value:
            raise HTTPException(status_code=422, detail="agent must match route")
        safe_keys = (
            {"agent", "hook_event_name", "event_name", "hook_name", "event_id", "callback_type", "thread_id"}
            if provider is AgentKind.CODEX
            else {"agent", "hook_event_name", "event_name", "hook_name", "event_id", "session_id", "prompt_id", "source", "reason", "notification_type", "title", "error"}
        )
        if any(key not in safe_keys for key in payload):
            raise HTTPException(status_code=422, detail="unsupported hook fields")
        name = next(
            (payload.get(key) for key in ("hook_event_name", "event_name", "hook_name") if isinstance(payload.get(key), str)),
            None,
        )
        allowed = {"SessionStart", "PermissionRequest", "Stop"} if provider is AgentKind.CODEX else {
            "SessionStart", "Notification", "PermissionRequest", "UserPromptSubmit", "Stop", "StopFailure", "SessionEnd",
        }
        if name not in allowed:
            raise HTTPException(status_code=422, detail="unsupported hook event")
        event_id = payload.get("event_id")
        if event_id is not None and (not isinstance(event_id, str) or not event_id or len(event_id) > 200):
            raise HTTPException(status_code=422, detail="invalid event_id")
        dedupe_id = f"{provider.value}:hook:{event_id}" if event_id else None
        if dedupe_id and not _record_event(runtime.store, dedupe_id, provider):
            return {"accepted": True, "duplicate": True}
        runtime.enqueue_notification(
            notification_type=f"{provider.value}:hook:{name}",
            payload_summary=f"{provider.value.title()} hook {name}",
            agent=provider,
        )
        return {"accepted": True}

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
        runtime.enqueue_notification(notification_type=f"hook:{name}", payload_summary=f"Codex hook {name}")
        return {"accepted": True}
    return app


def build_runtime(settings: Any) -> DaemonRuntime:
    store = SQLiteAgentStore(settings.sqlite_path)
    codex_store = SQLiteCodexStore(store)
    app_server = CodexAppServerClient(codex_path=settings.codex_path)
    orchestrator = CodexOrchestrator(codex_store, app_server, now=lambda: datetime.now(timezone.utc), id_factory=lambda: str(uuid.uuid4()), interaction_timeout_seconds=settings.interaction_timeout_seconds, notification_delay_seconds=settings.notification_delay_seconds)
    claude_manager = ClaudeSessionManager(
        store,
        ClaudeAgentSdkBridge(),
        outbox=store,
        interaction_timeout_seconds=settings.interaction_timeout_seconds,
    )
    lark = LarkBotClient(
        app_id=settings.lark_app_id,
        app_secret=settings.lark_app_secret,
        receive_id=settings.lark_receive_id,
        receive_id_type=settings.lark_receive_id_type,
        base_url=settings.lark_base_url,
        timeout_seconds=settings.http_timeout_seconds,
        message_format=getattr(settings, "message_format", "card"),
        output_tail_lines=getattr(settings, "output_tail_lines", 40),
    )
    connection = LarkLongConnection(settings.lark_app_id, settings.lark_app_secret, queue_capacity=settings.lark_event_queue_capacity)
    interactive_manager = InteractiveSessionManager(
        orchestrator,
        codex_path=settings.codex_path,
    )
    agent_registry = AgentRegistry()
    agent_registry.register(CodexService(orchestrator))
    agent_registry.register(ClaudeService(claude_manager))
    dispatcher = AgentInteractionDispatcher(store, agent_registry)
    return DaemonRuntime(
        settings,
        store,
        orchestrator,
        lark,
        connection,
        LarkControlRouter(store, orchestrator, dispatcher=dispatcher),
        interactive_manager=interactive_manager,
        agent_registry=agent_registry,
    )
