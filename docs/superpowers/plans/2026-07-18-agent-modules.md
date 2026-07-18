# Agent Modules Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (recommended) or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize `remind_bot` into concise, business-owned modules so Codex and Claude share one Bot, run as independent sessions, and remain easy to extend without coupling provider-specific code to shared runtime code.

**Architecture:** Keep `src/lark_bot` as the Python package and add `core/` plus `modules/` beneath it, following the reference backend's module-per-business-capability pattern. `modules/agent` owns the shared session, event, interaction, and adapter contracts; `modules/task`, `modules/notification`, and `modules/lark` own reusable execution and Bot capabilities; `modules/codex` and `modules/claude` own provider-specific behavior. The daemon and CLI remain composition entrypoints, while compatibility shims preserve existing public imports during migration.

**Tech Stack:** Python 3.11+, Pydantic v2, FastAPI, Typer, asyncio, SQLite, pytest, Hatchling.

---

## Scope And Invariants

- One daemon and one configured Lark Bot may serve multiple independent sessions.
- Every session has a stable `session_id`; provider-specific identifiers such as Codex `thread_id` are stored separately as `conversation_id`.
- Lark replies resolve through `lark_message_id -> interaction_id -> session_id`.
- Completion, failure, interruption, approval, and input notifications expose the agent name, session name, and a readable session identifier. The full ID remains available in structured data; the visible label uses the first eight characters unless the ID is shorter.
- Codex behavior, CLI names, daemon HTTP paths, SQLite table names, and current tests remain compatible unless a task explicitly adds a new canonical import.
- Claude is limited to event/Hook notification integration in this plan. It does not receive a Codex-style app-server or interactive TUI implementation.
- File names are descriptive and module-prefixed where the containing folder alone is not enough: `codex_orchestrator.py`, `claude_adapter.py`, `notification_render.py`.
- Do not create a generic `common.py`, `helpers.py`, or catch-all `utils/` for code that belongs to a named module.

## Locked File Map

Create these canonical packages and files:

```text
src/lark_bot/core/
  __init__.py
  config.py
  logging.py
  redaction.py

src/lark_bot/modules/
  __init__.py
  agent/
    __init__.py
    agent_model.py
    agent_event.py
    agent_protocol.py
    agent_session.py
    agent_store.py
    agent_service.py
  task/
    __init__.py
    task_model.py
    task_detector.py
    task_runner.py
  notification/
    __init__.py
    notification_model.py
    notification_base.py
    notification_service.py
    notification_store.py
  lark/
    __init__.py
    lark_client.py
    lark_connection.py
    lark_event.py
    lark_message.py
    lark_render.py
    lark_router.py
  codex/
    __init__.py
    codex_model.py
    codex_adapter.py
    codex_hook.py
    codex_hook_adapter.py
    codex_gateway.py
    codex_interactive.py
    codex_probe.py
    codex_tui.py
    codex_orchestrator.py
    codex_service.py
    codex_mapper.py
    codex_schema.py
    codex_store.py
    app_server/
      __init__.py
      app_server_client.py
      app_server_message.py
      app_server_response.py
    orchestration/
      __init__.py
      orchestration_event.py
      orchestration_interaction.py
      orchestration_summary.py
  claude/
    __init__.py
    claude_model.py
    claude_adapter.py
    claude_hook.py
    claude_service.py

tests/
  test_agent_modules.py
  test_task_modules.py
  test_notification_modules.py
  test_lark_modules.py
  test_claude_adapter.py
  test_session_concurrency.py
```

Keep `src/lark_bot/cli.py`, `src/lark_bot/__main__.py`, `src/lark_bot/server/`, and the current root packages as compatibility/composition entrypoints until the final cleanup task. Root shims must contain only imports and `__all__`; business logic belongs in the canonical module.

### Task 1: Establish Baseline And Canonical Agent Contracts

**Files:**
- Create: `src/lark_bot/core/__init__.py`
- Create: `src/lark_bot/core/config.py`, `src/lark_bot/core/logging.py`, `src/lark_bot/core/redaction.py`
- Create: `src/lark_bot/modules/__init__.py`
- Create: `src/lark_bot/modules/agent/__init__.py`
- Create: `src/lark_bot/modules/agent/agent_model.py`
- Create: `src/lark_bot/modules/agent/agent_event.py`
- Create: `src/lark_bot/modules/agent/agent_protocol.py`
- Create: `tests/test_agent_modules.py`
- Modify: `src/lark_bot/config.py`, `src/lark_bot/redaction.py` only to re-export canonical implementations after the new files pass tests

- [ ] **Step 1: Record the clean baseline**

Run:

```powershell
python -m pytest
git status --short --branch
```

Expected: all existing tests pass, the branch is `feat/20260718-modules-拆分`, and no source changes are present.

- [ ] **Step 2: Add failing canonical package tests**

Add to `tests/test_agent_modules.py`:

```python
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
        session=model.SessionRef(session_id="session-1", name="build", agent=model.AgentKind.CODEX),
        event_type="session_completed",
        status=model.SessionStatus.SUCCEEDED,
        summary="done",
    )

    assert value.session.session_id == "session-1"
```

- [ ] **Step 3: Verify the new tests fail before implementation**

Run:

```powershell
python -m pytest tests/test_agent_modules.py -q
```

Expected: `ModuleNotFoundError` because `lark_bot.modules.agent` does not yet exist.

- [ ] **Step 4: Implement the shared models**

Implement `agent_model.py` with these stable types:

```python
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class AgentKind(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"


class SessionStatus(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    WAITING = "waiting"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    WAITING_FOR_INPUT = "waiting_for_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class InteractionStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class SessionRef(BaseModel):
    session_id: str = Field(min_length=1)
    agent: AgentKind
    name: str = Field(min_length=1)


class AgentSession(SessionRef):
    conversation_id: str | None = None
    status: SessionStatus
    summary: str = ""
    created_at: datetime
    updated_at: datetime


class AgentInteraction(BaseModel):
    interaction_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    status: InteractionStatus = InteractionStatus.PENDING
    lark_message_id: str | None = None


class SessionDisplay(BaseModel):
    session_id: str = Field(min_length=1)
    session_name: str = Field(min_length=1)
    agent: AgentKind

    @property
    def short_id(self) -> str:
        return self.session_id[:8]

    @property
    def label(self) -> str:
        return f"{self.agent.value} / {self.session_name} [{self.short_id}]"
```

`agent_event.py` must define `AgentEvent` with `SessionRef`, `event_type`, `SessionStatus`, `summary`, and optional `interaction_id`. `agent_protocol.py` must define an `AgentAdapter` protocol with `agent: AgentKind`, `start()`, `close()`, `create_session()`, and `cancel_session()` signatures, plus `AgentRegistry` with deterministic `register()` and `get()` behavior. Unsupported interactive operations remain provider-specific and are not added to the common protocol.

- [ ] **Step 5: Add the core re-exports and rerun tests**

Move the existing configuration, logging, and redaction implementations into `core/` without behavior changes. Make `src/lark_bot/config.py` and `src/lark_bot/redaction.py` compatibility shims:

```python
from lark_bot.core.config import *
```

Run:

```powershell
python -m pytest tests/test_agent_modules.py tests/test_config.py tests/test_redaction.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit the contracts**

```powershell
git add src/lark_bot/core src/lark_bot/modules src/lark_bot/config.py src/lark_bot/redaction.py tests/test_agent_modules.py
git commit -m "重构: 建立Agent公共模块契约"
```

### Task 2: Move Task And Notification Capabilities Under Modules

**Files:**
- Create: `src/lark_bot/modules/task/__init__.py`, `task_model.py`, `task_detector.py`, `task_runner.py`
- Create: `src/lark_bot/modules/notification/__init__.py`, `notification_model.py`, `notification_base.py`, `notification_service.py`, `notification_store.py`
- Modify: `src/lark_bot/models.py`, `src/lark_bot/tasks/*.py`, `src/lark_bot/notifications/*.py`, `src/lark_bot/notifications/adapters/codex.py`
- Modify: `src/lark_bot/commands/common.py`, `src/lark_bot/server/agent_events.py`
- Create: `tests/test_task_modules.py`, `tests/test_notification_modules.py`
- Modify: `tests/test_detector.py`, `tests/test_runner.py`, `tests/test_codex_adapter.py`, `tests/test_agent_events.py`

- [ ] **Step 1: Add canonical import tests before moving code**

Add:

```python
from importlib import import_module


def test_task_module_owns_detection_and_execution() -> None:
    task = import_module("lark_bot.modules.task")
    assert task.detect_output
    assert task.run_command


def test_notification_module_owns_request_and_sender_contract() -> None:
    notification = import_module("lark_bot.modules.notification")
    assert notification.NotificationRequest
    assert notification.Notifier
```

- [ ] **Step 2: Verify the canonical tests fail**

Run:

```powershell
python -m pytest tests/test_task_modules.py tests/test_notification_modules.py -q
```

Expected: import failures for `lark_bot.modules.task` and `lark_bot.modules.notification`.

- [ ] **Step 3: Move the pure task code**

Copy `TaskStatus`, `DetectionResult`, `TaskResult`, `detect_output`, `dedupe_tags`, `run_command`, and `_tail_lines` into the module-prefixed files without changing signatures. Make the existing `models.py`, `tasks/detector.py`, and `tasks/runner.py` re-export canonical names. Update internal imports to use `modules.task`.

Export `TaskStatus`, `DetectionResult`, `TaskResult`, `detect_output`, `dedupe_tags`, and `run_command` from `modules/task/__init__.py` so the package-level contract test has one obvious entrypoint.

- [ ] **Step 4: Move notification contracts and add session context**

Define `NotificationContext` in `notification_model.py`:

```python
from pydantic import BaseModel

from lark_bot.modules.agent.agent_model import AgentKind
from lark_bot.modules.task.task_model import DetectionResult, TaskResult


class NotificationContext(BaseModel):
    agent: AgentKind
    session_id: str
    session_name: str


class NotificationRequest(BaseModel):
    task: TaskResult
    detection: DetectionResult
    context: NotificationContext | None = None

    @property
    def dedupe_key(self) -> str:
        command_text = " ".join(self.task.command)
        session = self.context.session_id if self.context else "-"
        return f"{self.task.source}:{session}:{self.task.name}:{command_text}:{self.detection.status}"
```

Keep `context=None` valid for existing `run` and `send-test` commands. Move `Notifier` to `notification_base.py`, and keep `notification_service.py` responsible for dedupe plus the configured Lark sender.

Export `NotificationRequest`, `NotificationContext`, and `Notifier` from `modules/notification/__init__.py` so the package-level contract test does not depend on implementation filenames.

- [ ] **Step 5: Update adapters and run targeted tests**

Use canonical imports:

```python
from lark_bot.modules.task.task_detector import dedupe_tags, detect_output
from lark_bot.modules.task.task_model import DetectionResult, TaskResult, TaskStatus
from lark_bot.modules.notification.notification_model import NotificationRequest
```

Run:

```powershell
python -m pytest tests/test_task_modules.py tests/test_notification_modules.py tests/test_detector.py tests/test_runner.py tests/test_codex_adapter.py tests/test_agent_events.py tests/test_cli_codex.py -q
```

Expected: PASS, including existing context-free notification behavior.

- [ ] **Step 6: Commit task and notification modules**

```powershell
git add src/lark_bot/modules/task src/lark_bot/modules/notification src/lark_bot/models.py src/lark_bot/tasks src/lark_bot/notifications src/lark_bot/commands/common.py src/lark_bot/server/agent_events.py tests
git commit -m "重构: 抽取任务与通知模块"
```

### Task 3: Move Shared Lark Transport And Reply Correlation

**Files:**
- Create: `src/lark_bot/modules/lark/__init__.py`
- Create: `src/lark_bot/modules/lark/lark_client.py`, `lark_connection.py`, `lark_event.py`, `lark_message.py`, `lark_render.py`, `lark_router.py`
- Modify: `src/lark_bot/lark/*.py`, `src/lark_bot/server/agent_events.py`, `src/lark_bot/server/daemon/*.py`, `src/lark_bot/commands/common.py`
- Create: `tests/test_lark_modules.py`
- Modify: `tests/test_lark_control.py`, `tests/test_lark_payload.py`, `tests/test_lark_render.py`, `tests/test_daemon_core.py`

- [ ] **Step 1: Add canonical Lark package tests**

Assert that `lark_client.LarkBotClient`, `lark_connection.LarkLongConnection`, `lark_router.LarkControlRouter`, and `lark_render.render_outbox_notification` are importable from `lark_bot.modules.lark`.

- [ ] **Step 2: Move transport files without changing public behavior**

Move the current Lark client, long connection, event models, message builders, renderer, and control router into the module-prefixed files. Preserve HTTP timeouts, receive ID configuration, redaction, message formats, and the current `LarkControlResult` values. Root `lark/*.py` files become re-export shims.

- [ ] **Step 3: Centralize reply correlation**

Add `find_pending_interaction(message_id: str) -> AgentInteraction | None` to `lark_router.py` by calling the existing store lookup. The router must pass only `interaction_id` and parsed answer data to the provider-facing service; it must never import `CodexSession` or `CodexOrchestrator` types.

- [ ] **Step 4: Update daemon and CLI imports**

Replace internal imports with:

```python
from lark_bot.modules.lark.lark_client import LarkBotClient
from lark_bot.modules.lark.lark_connection import LarkLongConnection
from lark_bot.modules.lark.lark_render import render_outbox_notification
from lark_bot.modules.lark.lark_router import LarkControlRouter
```

- [ ] **Step 5: Run Lark and daemon tests**

```powershell
python -m pytest tests/test_lark_modules.py tests/test_lark_control.py tests/test_lark_payload.py tests/test_lark_render.py tests/test_daemon_core.py -q
```

Expected: PASS with no network calls.

- [ ] **Step 6: Commit the Lark module**

```powershell
git add src/lark_bot/modules/lark src/lark_bot/lark src/lark_bot/server src/lark_bot/commands/common.py tests
git commit -m "重构: 抽取飞书通信模块"
```

### Task 4: Implement Shared Session Registry And Per-Session Concurrency

**Files:**
- Create: `src/lark_bot/modules/agent/agent_session.py`, `agent_store.py`, `agent_service.py`
- Modify: `src/lark_bot/modules/agent/agent_protocol.py`, `src/lark_bot/modules/agent/__init__.py`
- Create: `tests/test_session_concurrency.py`

- [ ] **Step 1: Add a failing concurrency test**

Use two independent session IDs and an `asyncio.Event` barrier:

```python
import asyncio

from lark_bot.modules.agent.agent_model import AgentKind, AgentSession, SessionStatus
from lark_bot.modules.agent.agent_service import AgentSessionService


async def test_two_sessions_run_concurrently_and_keep_identity() -> None:
    service = AgentSessionService()
    started = {"one": asyncio.Event(), "two": asyncio.Event()}
    release = asyncio.Event()

    async def operation(session_id: str) -> str:
        started[session_id].set()
        await release.wait()
        return session_id

    first = asyncio.create_task(service.run_serialized("one", operation, "one"))
    second = asyncio.create_task(service.run_serialized("two", operation, "two"))
    await asyncio.wait_for(asyncio.gather(started["one"].wait(), started["two"].wait()), timeout=1)
    release.set()

    assert await asyncio.gather(first, second) == ["one", "two"]


async def test_same_session_operations_are_serialized() -> None:
    service = AgentSessionService()
    order: list[str] = []
    first_release = asyncio.Event()

    async def first() -> None:
        order.append("first-start")
        await first_release.wait()
        order.append("first-end")

    async def second() -> None:
        order.append("second")

    first_task = asyncio.create_task(service.run_serialized("same", first))
    await asyncio.sleep(0)
    second_task = asyncio.create_task(service.run_serialized("same", second))
    await asyncio.sleep(0)
    assert order == ["first-start"]
    first_release.set()
    await asyncio.gather(first_task, second_task)
    assert order == ["first-start", "first-end", "second"]
```

- [ ] **Step 2: Verify the concurrency test fails**

Run:

```powershell
python -m pytest tests/test_session_concurrency.py -q
```

Expected: import or attribute failure because `AgentSessionService` does not exist.

- [ ] **Step 3: Implement the session service**

`AgentSessionService` must keep a lock per `session_id`, never use a global lock for unrelated sessions, and remove an idle lock after the operation completes. Its public method is:

```python
async def run_serialized(
    self,
    session_id: str,
    operation: Callable[..., Awaitable[T]],
    *args: object,
    **kwargs: object,
) -> T:
    ...
```

`AgentSessionStore` remains a Protocol with `create`, `get`, `list`, and `update` methods. `AgentRegistry` owns one adapter instance per `AgentKind`; it does not create one Lark client per session.

- [ ] **Step 4: Run concurrency and existing session tests**

```powershell
python -m pytest tests/test_session_concurrency.py tests/test_codex_orchestrator.py tests/test_codex_interactive.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the shared runtime boundary**

```powershell
git add src/lark_bot/modules/agent tests/test_session_concurrency.py
git commit -m "重构: 建立会话并发服务"
```

### Task 5: Move Codex Into Its Own Provider Module

**Files:**
- Create: `src/lark_bot/modules/codex/` files listed in the locked map
- Create: `src/lark_bot/modules/codex/app_server/` and `orchestration/` files listed in the locked map
- Modify: `src/lark_bot/codex/*.py`, `src/lark_bot/codex/app_server/*.py`, `src/lark_bot/codex/orchestration/*.py`, `src/lark_bot/storage/codex/*.py`
- Modify: `src/lark_bot/server/daemon/app.py`, `src/lark_bot/server/daemon/runtime.py`, `src/lark_bot/commands/*.py`
- Create: `tests/test_codex_modules.py`
- Modify: all existing Codex and daemon tests only where import paths change

- [ ] **Step 1: Add canonical Codex import assertions**

Assert these imports and symbols:

```python
from importlib import import_module


def test_codex_is_a_provider_module() -> None:
    assert import_module("lark_bot.modules.codex.codex_model").CodexSession
    assert import_module("lark_bot.modules.codex.codex_adapter").CodexEvent
    assert import_module("lark_bot.modules.codex.codex_orchestrator").CodexOrchestrator
    assert import_module("lark_bot.modules.codex.app_server.app_server_client").CodexAppServerClient
    assert import_module("lark_bot.modules.codex.orchestration.orchestration_event").OrchestratorEvent
```

- [ ] **Step 2: Verify canonical Codex imports fail**

Run:

```powershell
python -m pytest tests/test_codex_modules.py -q
```

Expected: `ModuleNotFoundError` for `lark_bot.modules.codex`.

- [ ] **Step 3: Move Codex models, adapter, hooks, gateway, TUI, and probe**

Move implementation bodies unchanged into the named canonical files. Replace imports that point to `lark_bot.codex.*` with `lark_bot.modules.codex.*` or shared module imports. Preserve `CodexEvent` aliases, hook spool behavior, TUI callback arguments, gateway token validation, and process cleanup.

- [ ] **Step 4: Move app-server and orchestration submodules**

Place wire contracts in `app_server_message.py`, response builders in `app_server_response.py`, and lifecycle code in `app_server_client.py`. Place event dataclasses, pure interaction decisions, summaries, and `CodexOrchestrator` in the three `orchestration_*` files. Preserve all public method signatures and task names used by tests.

- [ ] **Step 5: Place Codex persistence behind the provider module**

Make `modules/codex/codex_store.py` the canonical import for `SQLiteCodexStore`, while preserving the current SQLite schema and migration statements. Keep `storage/codex/*.py` as re-export shims until all imports pass through the provider module.

- [ ] **Step 6: Wire the daemon to the canonical Codex provider**

`build_runtime()` must construct one `CodexAppServerClient`, one `CodexOrchestrator`, one `SQLiteCodexStore`, one `InteractiveSessionManager`, and one shared Lark client. Multiple calls to `create_session()` use the same provider and Lark instances but receive distinct session IDs.

- [ ] **Step 7: Run the full Codex regression set**

```powershell
python -m pytest tests/test_codex_modules.py tests/test_codex_models.py tests/test_codex_adapter.py tests/test_codex_app_server.py tests/test_codex_gateway.py tests/test_codex_hook_adapter.py tests/test_codex_interactive.py tests/test_codex_orchestrator.py tests/test_codex_remote_probe.py tests/test_codex_storage.py tests/test_codex_tui.py tests/test_daemon_core.py tests/test_lark_control.py -q
```

Expected: PASS with unchanged CLI and daemon behavior.

- [ ] **Step 8: Commit the Codex provider move**

```powershell
git add src/lark_bot/modules/codex src/lark_bot/codex src/lark_bot/storage/codex src/lark_bot/server/daemon src/lark_bot/commands tests
git commit -m "重构: 封装Codex专属模块"
```

### Task 6: Add The Claude Provider Module

**Files:**
- Create: `src/lark_bot/modules/claude/__init__.py`
- Create: `src/lark_bot/modules/claude/claude_model.py`, `claude_adapter.py`, `claude_hook.py`, `claude_service.py`
- Modify: `src/lark_bot/modules/agent/agent_protocol.py`, `src/lark_bot/modules/agent/agent_service.py`
- Modify: `src/lark_bot/commands/app.py` or the canonical command module that owns event commands
- Create: `tests/test_claude_adapter.py`

- [ ] **Step 1: Add failing Claude adapter tests**

Cover a successful Stop event, a failed event, a `PermissionRequest` waiting event, the `session_id` alias, and rejection of an unsupported event. Example:

```python
from lark_bot.modules.claude.claude_adapter import ClaudeEvent, claude_event_to_notification
from lark_bot.modules.task.task_model import TaskStatus


def test_claude_stop_event_preserves_session_identity() -> None:
    request = claude_event_to_notification(
        ClaudeEvent(
            session_id="claude-session-1",
            session_name="docs",
            event_name="Stop",
            status="completed",
            summary="finished",
        )
    )

    assert request.context.session_id == "claude-session-1"
    assert request.context.session_name == "docs"
    assert request.context.agent.value == "claude"
    assert request.detection.status is TaskStatus.SUCCEEDED
```

- [ ] **Step 2: Verify Claude tests fail before implementation**

Run:

```powershell
python -m pytest tests/test_claude_adapter.py -q
```

Expected: import failure for `lark_bot.modules.claude`.

- [ ] **Step 3: Implement Claude event models and adapter**

`ClaudeEvent` must normalize `session_id`/`sessionId`, `session_name`/`name`, `event_name`/`hook_event_name`, `stdout_tail`/`output_tail`, and `stderr_tail`. `claude_event_to_notification()` must construct a `NotificationRequest` with `NotificationContext(agent=AgentKind.CLAUDE, ...)`, reuse shared output detection, map `Stop` to success/failure, and map permission/input events to `WAITING`.

- [ ] **Step 4: Implement Claude Hook ingestion without Codex imports**

`claude_hook.py` reads bounded JSON from stdin, rejects unsupported event names, redacts summaries before spooling, and forwards accepted events to the shared notification service. It may reuse `modules.notification.notification_service`, but it must not import `modules.codex`.

- [ ] **Step 5: Register Claude beside Codex**

Add Claude to `AgentRegistry` and expose a `claude-event` command only if the existing command composition has a stable event-command owner. The command must accept `--file` or stdin and use the same dedupe/send path as `codex-event`.

- [ ] **Step 6: Run provider tests**

```powershell
python -m pytest tests/test_claude_adapter.py tests/test_codex_adapter.py tests/test_cli_codex.py tests/test_agent_events.py -q
```

Expected: PASS and no real network access.

- [ ] **Step 7: Commit the Claude module**

```powershell
git add src/lark_bot/modules/claude src/lark_bot/modules/agent src/lark_bot/commands tests/test_claude_adapter.py
git commit -m "功能: 增加Claude适配模块"
```

### Task 7: Add Session Identity To Notifications And Lark Rendering

**Files:**
- Modify: `src/lark_bot/modules/notification/notification_model.py`, `notification_service.py`
- Modify: `src/lark_bot/modules/lark/lark_render.py`, `lark_router.py`
- Modify: `src/lark_bot/server/daemon/runtime.py`
- Modify: `src/lark_bot/modules/codex/codex_orchestrator.py`, `src/lark_bot/modules/claude/claude_adapter.py`
- Modify: `src/lark_bot/modules/codex/codex_store.py` and its schema migration source
- Modify: `tests/test_lark_render.py`, `tests/test_daemon_core.py`, `tests/test_codex_orchestrator.py`, `tests/test_claude_adapter.py`

- [ ] **Step 1: Add failing visible-identity tests**

For an outbox item with `session_id="abcdef123456789"` and a session name `build`, assert the card contains `codex / build [abcdef12]`. For a context-bearing task notification, assert the same identity appears in text and card formats. For an item without a session, assert existing output is unchanged.

- [ ] **Step 2: Add a session lookup to daemon rendering**

When rendering an outbox item with `session_id`, `DaemonRuntime._render()` must load the session record and pass a `SessionDisplay` to `render_outbox_notification()`. Extend the `notification_outbox` schema with nullable `agent` and `session_name` columns and write those values when enqueuing an event. If the session has been deleted, render the agent, session name, and short ID from those outbox columns and do not fail the outbox worker. Existing rows receive `NULL` values and use the current Codex heading fallback.

- [ ] **Step 3: Render a stable session header**

Use a single helper:

```python
def session_label(display: SessionDisplay | None) -> str | None:
    return display.label if display is not None else None
```

The card title and text body must include agent, session name, and short session ID exactly once. The existing status heading remains the status signal; the session label is added to the body or metadata line, not duplicated as a second status heading.

- [ ] **Step 4: Ensure completion and interaction events carry identity**

`CodexOrchestrator._emit()` must preserve `session_id`, `agent`, `session_name`, and `interaction_id` in every outbox row. Add a SQLite migration that uses `ALTER TABLE notification_outbox ADD COLUMN agent TEXT` and `ALTER TABLE notification_outbox ADD COLUMN session_name TEXT`, guarded by the existing schema version mechanism. Claude adapter notifications must populate `NotificationContext`. Lark reply handling must continue to use `lark_message_id` and must not infer a session from display text.

- [ ] **Step 5: Run rendering and daemon tests**

```powershell
python -m pytest tests/test_lark_render.py tests/test_daemon_core.py tests/test_codex_orchestrator.py tests/test_claude_adapter.py -q
```

Expected: PASS, including context-free notifications and exact reply correlation.

- [ ] **Step 6: Commit visible session identity**

```powershell
git add src/lark_bot/modules/notification src/lark_bot/modules/lark src/lark_bot/modules/codex src/lark_bot/modules/claude src/lark_bot/server/daemon tests
git commit -m "功能: 增加通知会话标识"
```

### Task 8: Make Daemon Composition Provider-Aware

**Files:**
- Modify: `src/lark_bot/server/daemon/app.py`, `src/lark_bot/server/daemon/runtime.py`
- Modify: `src/lark_bot/modules/agent/agent_service.py`, `src/lark_bot/modules/notification/notification_service.py`
- Modify: `tests/test_daemon_core.py`, `tests/test_session_concurrency.py`

- [ ] **Step 1: Add a shared-Bot construction test**

Build a runtime with test doubles for Codex and Claude adapters. Assert that both adapters receive the same `LarkBotClient` and that creating two sessions produces distinct `session_id` values.

- [ ] **Step 2: Register providers once during runtime construction**

`build_runtime()` must create one `AgentRegistry`, register one Codex adapter and one Claude adapter, and pass the registry plus one notification service to `DaemonRuntime`. Do not instantiate a Bot client from an adapter or from a session operation.

- [ ] **Step 3: Route provider events by session identity**

For each provider event, validate that `session_id` belongs to the registered provider before writing the outbox record. An unknown provider or session returns a typed error and does not send a notification.

- [ ] **Step 4: Verify concurrent provider sessions**

Run:

```powershell
python -m pytest tests/test_session_concurrency.py tests/test_daemon_core.py tests/test_codex_orchestrator.py tests/test_claude_adapter.py -q
```

Expected: two independent sessions may progress simultaneously, same-session operations remain serialized, and one shared Lark client is used.

- [ ] **Step 5: Commit provider-aware composition**

```powershell
git add src/lark_bot/server/daemon src/lark_bot/modules/agent src/lark_bot/modules/notification tests
git commit -m "重构: 统一多Agent运行时"
```

### Task 9: Remove Compatibility Debt, Update Documentation, And Verify The Package

**Files:**
- Modify: `src/lark_bot/cli.py`, `src/lark_bot/__main__.py` only if canonical imports require it
- Delete: old implementation bodies under `src/lark_bot/codex`, `src/lark_bot/lark`, `src/lark_bot/tasks`, `src/lark_bot/notifications`, and `src/lark_bot/storage/codex` only after they are pure shims or have no remaining imports
- Modify: `tests/test_package_structure.py`
- Modify: `README.md`

- [ ] **Step 1: Add stale-import checks**

Run:

```powershell
rg -n "from lark_bot\.(codex|lark|tasks|notifications|storage\.codex)\." src tests README.md
```

Expected: output is limited to deliberate compatibility shims; every business implementation import points to `lark_bot.modules.*`. Inspect each remaining line before deleting a shim.

- [ ] **Step 2: Enforce canonical package structure**

Extend `tests/test_package_structure.py` to assert:

```python
from importlib import import_module


def test_provider_and_shared_modules_are_importable() -> None:
    for name in (
        "lark_bot.modules.agent.agent_service",
        "lark_bot.modules.task.task_runner",
        "lark_bot.modules.notification.notification_service",
        "lark_bot.modules.lark.lark_router",
        "lark_bot.modules.codex.codex_orchestrator",
        "lark_bot.modules.claude.claude_adapter",
    ):
        assert import_module(name)
```

- [ ] **Step 3: Update reader-facing documentation**

Update README's project structure section with the `core/`, `modules/agent`, `modules/task`, `modules/notification`, `modules/lark`, `modules/codex`, and `modules/claude` tree. Document the one-Bot/multiple-session model and the `session_id`/`lark_message_id` correlation path. Keep the current CLI examples unchanged unless a new `claude-event` command was actually registered.

- [ ] **Step 4: Run the complete verification set**

```powershell
python -m pytest
git diff --check
$env:PYTHONPATH="src"; python -m lark_bot --help
$env:PYTHONPATH="src"; python -c "from lark_bot.cli import app; from lark_bot.server.app import app as server_app; print(bool(app), bool(server_app))"
```

Expected: all tests pass, `git diff --check` is clean, CLI help starts successfully, and the final command prints `True True`.

- [ ] **Step 5: Inspect the final dependency direction**

Run:

```powershell
rg -n "from lark_bot\.modules\.(codex|claude)" src/lark_bot/modules/agent src/lark_bot/modules/task src/lark_bot/modules/notification src/lark_bot/modules/lark
```

Expected: no shared module imports Codex or Claude implementation details. Codex and Claude may import shared modules; they must not import each other.

- [ ] **Step 6: Commit documentation and cleanup**

```powershell
git add README.md src/lark_bot tests
git commit -m "文档: 完善Agent模块结构说明"
```

## Commit Order

Use the following focused Chinese commits, one per completed task:

1. `重构: 建立Agent公共模块契约`
2. `重构: 抽取任务与通知模块`
3. `重构: 抽取飞书通信模块`
4. `重构: 建立会话并发服务`
5. `重构: 封装Codex专属模块`
6. `功能: 增加Claude适配模块`
7. `功能: 增加通知会话标识`
8. `重构: 统一多Agent运行时`
9. `文档: 完善Agent模块结构说明`

Do not amend earlier commits, add coworker attribution, or add `Co-Authored-By` trailers.

## Plan Self-Review

- The one-Bot/multiple-session invariant is covered by Tasks 4, 7, and 8.
- Provider-specific ownership is covered by Tasks 5 and 6; Claude has no dependency on Codex.
- Descriptive module-prefixed names are locked in the file map and checked in Task 9.
- Session identity is carried in the common model, outbox, renderer, and reply correlation tests.
- Existing imports and behavior are preserved through shims until the final cleanup task.
- Each task has explicit files, failing tests, verification commands, and a Chinese commit message.
