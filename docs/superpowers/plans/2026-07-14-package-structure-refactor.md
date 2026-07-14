# Package Structure Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize `src/lark_bot` by functional ownership and split the largest modules while preserving CLI, protocol, persistence, and notification behavior.

**Architecture:** Keep `lark_bot.cli:app`, `python -m lark_bot`, shared settings/models/redaction, and `lark_bot.server.app:app` stable. Move task, notification, Lark, Codex, and daemon implementations into functional packages; split stateful large classes with small helper modules or operation mixins while retaining their public service/store facades.

**Tech Stack:** Python 3.11+, Typer, FastAPI, Pydantic v2, asyncio, websockets, SQLite, pytest, Hatchling.

---

## Locked File Map

Create these packages and modules:

```text
src/lark_bot/commands/{__init__,common,tasks,codex,daemon,hooks}.py
src/lark_bot/tasks/{__init__,detector,runner}.py
src/lark_bot/notifications/{__init__,base}.py
src/lark_bot/notifications/adapters/{__init__,codex}.py
src/lark_bot/lark/{__init__,client,events,router,connection}.py
src/lark_bot/codex/{__init__,models,gateway,interactive,tui,hooks,hook_adapter,probe}.py
src/lark_bot/codex/app_server/{__init__,client,messages,responses}.py
src/lark_bot/codex/orchestration/{__init__,service,events,interactions,summaries}.py
src/lark_bot/storage/codex/{__init__,store,schema,sessions,interactions,outbox,audit,mappers}.py
src/lark_bot/server/daemon/{__init__,app,runtime,auth}.py
tests/test_package_structure.py
```

Retain these stable files:

```text
src/lark_bot/__init__.py
src/lark_bot/__main__.py
src/lark_bot/cli.py
src/lark_bot/config.py
src/lark_bot/models.py
src/lark_bot/redaction.py
src/lark_bot/server/app.py
src/lark_bot/server/agent_events.py
src/lark_bot/server/lark_events.py
src/lark_bot/storage/base.py
src/lark_bot/storage/sqlite.py
src/lark_bot/storage/redis.py
```

Delete obsolete implementation paths only after every internal import and test uses the canonical path. Do not stage `AGENTS.md` or `CLAUDE.md`.

### Task 1: Establish structure contracts and move task/notification leaves

**Files:**
- Create: `tests/test_package_structure.py`
- Create: `src/lark_bot/tasks/__init__.py`
- Move: `src/lark_bot/detector.py` → `src/lark_bot/tasks/detector.py`
- Move: `src/lark_bot/runner.py` → `src/lark_bot/tasks/runner.py`
- Create: `src/lark_bot/notifications/__init__.py`
- Move: `src/lark_bot/notifier/base.py` → `src/lark_bot/notifications/base.py`
- Create: `src/lark_bot/notifications/adapters/__init__.py`
- Move: `src/lark_bot/adapters/codex.py` → `src/lark_bot/notifications/adapters/codex.py`
- Modify: `src/lark_bot/cli.py`
- Modify: `src/lark_bot/server/agent_events.py`
- Modify: `tests/test_detector.py`
- Modify: `tests/test_runner.py`
- Modify: `tests/test_codex_adapter.py`

- [ ] **Step 1: Record the passing baseline**

Run: `python -m pytest`

Expected: `279 passed` with only the existing FastAPI/Starlette `httpx` deprecation warning.

- [ ] **Step 2: Add failing canonical-import tests**

Add to `tests/test_package_structure.py`:

```python
from importlib import import_module


def test_task_and_notification_modules_have_functional_owners() -> None:
    assert import_module("lark_bot.tasks.detector").detect_output
    assert import_module("lark_bot.tasks.runner").run_command
    assert import_module("lark_bot.notifications.base").Notifier
    assert import_module("lark_bot.notifications.adapters.codex").CodexEvent
```

- [ ] **Step 3: Verify the structure test fails before the move**

Run: `python -m pytest tests/test_package_structure.py -q`

Expected: FAIL with `ModuleNotFoundError` for `lark_bot.tasks` or `lark_bot.notifications`.

- [ ] **Step 4: Move the leaf modules and update imports**

Use the canonical imports everywhere:

```python
from lark_bot.tasks.detector import dedupe_tags, detect_output
from lark_bot.tasks.runner import run_command
from lark_bot.notifications.adapters.codex import CodexEvent, codex_event_to_notification
from lark_bot.notifications.base import Notifier
```

Keep `models.py` and `redaction.py` at the package root. Remove the empty `adapters/` directory after the move. Keep `notifier/lark.py` until Task 4 moves the Lark client.

- [ ] **Step 5: Run targeted tests**

Run: `python -m pytest tests/test_package_structure.py tests/test_detector.py tests/test_runner.py tests/test_codex_adapter.py tests/test_agent_events.py tests/test_cli_codex.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the leaf-module move**

```bash
git add src/lark_bot/tasks src/lark_bot/notifications src/lark_bot/cli.py src/lark_bot/server/agent_events.py tests/test_package_structure.py tests/test_detector.py tests/test_runner.py tests/test_codex_adapter.py
git commit -m "refactor: group task and notification modules"
```

### Task 2: Split the Codex app-server client

**Files:**
- Create: `src/lark_bot/codex/__init__.py`
- Create: `src/lark_bot/codex/app_server/__init__.py`
- Create: `src/lark_bot/codex/app_server/messages.py`
- Create: `src/lark_bot/codex/app_server/responses.py`
- Create: `src/lark_bot/codex/app_server/client.py`
- Delete: `src/lark_bot/codex_app_server.py`
- Modify: `src/lark_bot/codex_gateway.py`
- Modify: `src/lark_bot/codex_interactive.py`
- Modify: `src/lark_bot/codex_orchestrator.py`
- Modify: `src/lark_bot/daemon.py`
- Modify: `tests/test_codex_app_server.py`
- Modify: dependent Codex and daemon tests

- [ ] **Step 1: Add failing canonical app-server imports**

Extend `tests/test_package_structure.py`:

```python
def test_codex_app_server_is_split_by_wire_and_lifecycle_responsibility() -> None:
    messages = import_module("lark_bot.codex.app_server.messages")
    responses = import_module("lark_bot.codex.app_server.responses")
    client = import_module("lark_bot.codex.app_server.client")
    assert messages.ServerRequest
    assert messages.ServerNotification
    assert responses.user_input_response
    assert client.CodexAppServerClient
```

- [ ] **Step 2: Verify the new imports fail**

Run: `python -m pytest tests/test_package_structure.py::test_codex_app_server_is_split_by_wire_and_lifecycle_responsibility -q`

Expected: FAIL because `lark_bot.codex.app_server` does not exist.

- [ ] **Step 3: Extract message contracts**

Move `ServerRequest`, `ServerNotification`, and reader/writer/process protocols into `messages.py`. Export public wire contracts from `app_server/__init__.py`:

```python
from .client import CodexAppServerClient, ProcessExitedError, ProtocolError, ServerRpcError
from .messages import ServerNotification, ServerRequest
from .responses import (
    command_approval_response,
    file_approval_response,
    permission_response,
    user_input_response,
)

__all__ = [
    "CodexAppServerClient",
    "ProcessExitedError",
    "ProtocolError",
    "ServerRpcError",
    "ServerNotification",
    "ServerRequest",
    "command_approval_response",
    "file_approval_response",
    "permission_response",
    "user_input_response",
]
```

- [ ] **Step 4: Extract response builders and move lifecycle code**

Move the four response-builder functions unchanged to `responses.py`. Move errors, lifecycle enum, process factory, and `CodexAppServerClient` to `client.py`; import wire protocols from `messages.py`. Preserve constructor and method signatures exactly.

- [ ] **Step 5: Update canonical imports and tests**

Use:

```python
from lark_bot.codex.app_server import CodexAppServerClient, ServerNotification, ServerRequest
from lark_bot.codex.app_server import command_approval_response, permission_response
```

Patch lifecycle implementation through `lark_bot.codex.app_server.client` in tests.

- [ ] **Step 6: Run app-server and immediate dependent tests**

Run: `python -m pytest tests/test_codex_app_server.py tests/test_codex_gateway.py tests/test_codex_interactive.py tests/test_codex_orchestrator.py tests/test_daemon_core.py tests/test_package_structure.py -q`

Expected: PASS.

- [ ] **Step 7: Commit the app-server split**

```bash
git add src/lark_bot/codex src/lark_bot/codex_gateway.py src/lark_bot/codex_interactive.py src/lark_bot/codex_orchestrator.py src/lark_bot/daemon.py tests
git commit -m "refactor: split codex app server client"
```

### Task 3: Group the remaining Codex runtime modules

**Files:**
- Move: `src/lark_bot/codex_models.py` → `src/lark_bot/codex/models.py`
- Move: `src/lark_bot/codex_gateway.py` → `src/lark_bot/codex/gateway.py`
- Move: `src/lark_bot/codex_interactive.py` → `src/lark_bot/codex/interactive.py`
- Move: `src/lark_bot/codex_tui.py` → `src/lark_bot/codex/tui.py`
- Move: `src/lark_bot/hooks.py` → `src/lark_bot/codex/hooks.py`
- Move: `src/lark_bot/codex_hook_adapter.py` → `src/lark_bot/codex/hook_adapter.py`
- Move: `src/lark_bot/codex_remote_probe.py` → `src/lark_bot/codex/probe.py`
- Modify: all Codex, storage, CLI, daemon, and Lark imports
- Modify: corresponding tests and README probe command

- [ ] **Step 1: Add failing Codex package ownership test**

Add:

```python
def test_codex_runtime_modules_live_under_codex_package() -> None:
    for name in ("models", "gateway", "interactive", "tui", "hooks", "hook_adapter", "probe"):
        assert import_module(f"lark_bot.codex.{name}")
```

- [ ] **Step 2: Verify the ownership test fails**

Run: `python -m pytest tests/test_package_structure.py::test_codex_runtime_modules_live_under_codex_package -q`

Expected: FAIL on the first not-yet-moved module.

- [ ] **Step 3: Move modules and convert all imports atomically**

Canonical examples:

```python
from lark_bot.codex.models import CodexSession, InteractionKind, SessionStatus
from lark_bot.codex.gateway import CodexGateway
from lark_bot.codex.interactive import InteractiveSessionManager
from lark_bot.codex.tui import CodexTuiLauncher, CodexTuiOptions
from lark_bot.codex.hooks import build_notify_override
```

Keep probe execution with:

```python
if __name__ == "__main__":
    main()
```

Document `python -m lark_bot.codex.probe` as the canonical probe command.

- [ ] **Step 4: Update monkeypatch targets to canonical owners**

Use `lark_bot.codex.tui.shutil.which`, `lark_bot.codex.tui._read_existing_notify`, `lark_bot.codex.hook_adapter.subprocess.Popen`, and `lark_bot.codex.probe.subprocess.run`.

- [ ] **Step 5: Run the Codex-focused suite**

Run: `python -m pytest tests/test_codex_*.py tests/test_hooks.py tests/test_cli_codex.py tests/test_daemon_core.py tests/test_package_structure.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the Codex grouping**

```bash
git add src/lark_bot/codex src/lark_bot/cli.py src/lark_bot/daemon.py src/lark_bot/lark_control.py src/lark_bot/storage tests README.md
git commit -m "refactor: group codex runtime modules"
```

### Task 4: Split Lark client, events, routing, and connection

**Files:**
- Create: `src/lark_bot/lark/__init__.py`
- Move: `src/lark_bot/notifier/lark.py` → `src/lark_bot/lark/client.py`
- Create: `src/lark_bot/lark/events.py`
- Create: `src/lark_bot/lark/router.py`
- Create: `src/lark_bot/lark/connection.py`
- Delete: `src/lark_bot/lark_control.py`
- Delete: remaining `src/lark_bot/notifier/`
- Modify: `src/lark_bot/cli.py`
- Modify: `src/lark_bot/daemon.py`
- Modify: Lark, payload, daemon, and package-structure tests

- [ ] **Step 1: Add failing Lark boundary test**

```python
def test_lark_modules_separate_client_routing_and_connection() -> None:
    assert import_module("lark_bot.lark.client").LarkBotClient
    assert import_module("lark_bot.lark.events").LarkMessageEvent
    assert import_module("lark_bot.lark.router").LarkControlRouter
    assert import_module("lark_bot.lark.connection").LarkLongConnection
```

- [ ] **Step 2: Verify the boundary test fails**

Run: `python -m pytest tests/test_package_structure.py::test_lark_modules_separate_client_routing_and_connection -q`

Expected: FAIL because `lark_bot.lark` does not exist.

- [ ] **Step 3: Extract event normalization**

Move `LarkReactionEvent`, `LarkMessageEvent`, `LarkControlResult`, `normalize_reaction_event`, `normalize_message_event`, and their small field helpers into `events.py`. Keep the union alias:

```python
LarkControlEvent = LarkReactionEvent | LarkMessageEvent
```

- [ ] **Step 4: Extract routing and reply parsing**

Move `LarkControlRouter`, `_strip_mentions`, `_parse_approval_answer`, and `_parse_answers` into `router.py`. Import normalized event types from `events.py`; preserve `route()` result values.

- [ ] **Step 5: Extract the spawn-safe long connection**

Move `_safe_child_put`, `_lark_ws_worker`, `decode_child_event`, and `LarkLongConnection` into `connection.py`. Ensure process construction uses the module-level `_lark_ws_worker` from the same canonical module.

- [ ] **Step 6: Move the HTTP client and update imports**

Use:

```python
from lark_bot.lark.client import LarkAPIError, LarkBotClient, build_text_message
from lark_bot.lark.connection import LarkLongConnection
from lark_bot.lark.router import LarkControlRouter
```

- [ ] **Step 7: Run Lark and integration tests**

Run: `python -m pytest tests/test_lark_control.py tests/test_lark_payload.py tests/test_daemon_core.py tests/test_cli_codex.py tests/test_package_structure.py -q`

Expected: PASS.

- [ ] **Step 8: Commit the Lark split**

```bash
git add src/lark_bot/lark src/lark_bot/cli.py src/lark_bot/daemon.py tests
git commit -m "refactor: split lark integration modules"
```

### Task 5: Split the Codex SQLite store by aggregate

**Files:**
- Create: `src/lark_bot/storage/codex/__init__.py`
- Create: `src/lark_bot/storage/codex/store.py`
- Create: `src/lark_bot/storage/codex/schema.py`
- Create: `src/lark_bot/storage/codex/sessions.py`
- Create: `src/lark_bot/storage/codex/interactions.py`
- Create: `src/lark_bot/storage/codex/outbox.py`
- Create: `src/lark_bot/storage/codex/audit.py`
- Create: `src/lark_bot/storage/codex/mappers.py`
- Delete: `src/lark_bot/storage/codex_sqlite.py`
- Modify: `src/lark_bot/server/daemon/runtime.py` or current `daemon.py`
- Modify: `tests/test_codex_storage.py`
- Modify: `tests/test_codex_orchestrator.py`

- [ ] **Step 1: Add failing canonical store import and schema test**

```python
def test_codex_store_has_canonical_package_path() -> None:
    module = import_module("lark_bot.storage.codex")
    assert module.SQLiteCodexStore.__module__ == "lark_bot.storage.codex.store"
```

Add to `tests/test_codex_storage.py`:

```python
def test_refactored_store_keeps_existing_tables(tmp_path):
    store = SQLiteCodexStore(tmp_path / "codex.sqlite3")
    with store._connection() as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {
        "codex_sessions",
        "codex_interactions",
        "codex_event_dedupe",
        "notification_outbox",
        "codex_audit",
    } <= names
```

- [ ] **Step 2: Verify the canonical import fails**

Run: `python -m pytest tests/test_package_structure.py::test_codex_store_has_canonical_package_path -q`

Expected: FAIL because `lark_bot.storage.codex` does not exist.

- [ ] **Step 3: Extract schema and row mapping helpers**

Move `_SCHEMA_VERSION`, `_MIGRATIONS`, and the versioned migration loop into `schema.py` as:

```python
def initialize_schema(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"unsupported Codex schema version {version}; maximum is {SCHEMA_VERSION}"
        )
    if version == SCHEMA_VERSION:
        return
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("BEGIN IMMEDIATE")
    for target_version in range(version + 1, SCHEMA_VERSION + 1):
        for statement in MIGRATIONS[target_version]:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {target_version}")
    connection.commit()
    connection.execute("PRAGMA foreign_keys = ON")
```

Move `_session_from_row`, `_interaction_from_row`, `_outbox_from_row`, `_audit_from_row`, datetime serializers, decision normalization, and interaction value serialization into `mappers.py` without changing field conversions.

- [ ] **Step 4: Group store operations into private mixins**

Create the implementation classes `SessionOperations`, `InteractionOperations`, `OutboxOperations`, and `AuditOperations` in their matching modules.

Move methods as follows:

- sessions: session CRUD, conditional updates, terminal claims, interactive turn completion, startup reconciliation;
- interactions: creation, lookup, Lark attachment, resolution, cancellation, expiry;
- outbox: event dedupe, enqueue, due-list, sent/failure transitions;
- audit: record and list audit entries.

Each mixin calls `self._connection()` and preserves the existing lock/transaction boundaries and SQL statements exactly.

- [ ] **Step 5: Build the public store facade**

In `store.py`:

```python
class SQLiteCodexStore(SessionOperations, InteractionOperations, OutboxOperations, AuditOperations):
    def __init__(self, path: str | Path) -> None:
        self.database = str(path)
        self._closed = False
        self._memory_connection: sqlite3.Connection | None = None
        if self.database == ":memory:":
            self._memory_connection = self._new_connection(self.database)
        else:
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            initialize_schema(connection)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self._closed:
            raise RuntimeError("SQLiteCodexStore is closed")
        connection = self._memory_connection
        owns_connection = connection is None
        if connection is None:
            connection = self._new_connection(str(self.path))
        try:
            with connection:
                yield connection
        finally:
            if owns_connection:
                connection.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._memory_connection is not None:
            self._memory_connection.close()
            self._memory_connection = None

    def __enter__(self) -> Self:
        if self._closed:
            raise RuntimeError("SQLiteCodexStore is closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @staticmethod
    def _new_connection(database: str) -> sqlite3.Connection:
        connection = sqlite3.connect(database, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection
```

Export only `SQLiteCodexStore` from `storage/codex/__init__.py`.

- [ ] **Step 6: Update imports and run storage tests**

Run: `python -m pytest tests/test_codex_storage.py tests/test_codex_orchestrator.py tests/test_daemon_core.py tests/test_package_structure.py -q`

Expected: PASS with unchanged storage behavior.

- [ ] **Step 7: Commit the persistence split**

```bash
git add src/lark_bot/storage src/lark_bot/daemon.py src/lark_bot/server tests
git commit -m "refactor: split codex sqlite persistence"
```

### Task 6: Split Codex orchestration contracts and pure logic

**Files:**
- Create: `src/lark_bot/codex/orchestration/__init__.py`
- Create: `src/lark_bot/codex/orchestration/events.py`
- Create: `src/lark_bot/codex/orchestration/interactions.py`
- Create: `src/lark_bot/codex/orchestration/summaries.py`
- Create: `src/lark_bot/codex/orchestration/service.py`
- Delete: `src/lark_bot/codex_orchestrator.py`
- Modify: daemon/runtime imports
- Modify: `tests/test_codex_orchestrator.py`
- Modify: `tests/test_daemon_core.py`

- [ ] **Step 1: Add failing canonical orchestrator test**

```python
def test_codex_orchestrator_has_focused_modules() -> None:
    assert import_module("lark_bot.codex.orchestration.service").CodexOrchestrator
    assert import_module("lark_bot.codex.orchestration.events").OrchestratorEventType
    assert import_module("lark_bot.codex.orchestration.interactions").terminal_decision
    assert import_module("lark_bot.codex.orchestration.summaries").request_summary
```

- [ ] **Step 2: Verify the canonical test fails**

Run: `python -m pytest tests/test_package_structure.py::test_codex_orchestrator_has_focused_modules -q`

Expected: FAIL because `lark_bot.codex.orchestration` does not exist.

- [ ] **Step 3: Extract event contracts**

Move `OrchestratorEventType` and `OrchestratorEvent` unchanged into `events.py` and export them from `orchestration/__init__.py`.

- [ ] **Step 4: Extract summary helpers**

Move `_safe_summary`, `_request_summary`, `_terminal_status`, and `_turn_summary` into public-within-package helpers named `safe_summary`, `request_summary`, `terminal_status`, and `turn_summary`. Update service calls; preserve truncation, redaction, and status mapping.

- [ ] **Step 5: Extract pure interaction decision logic**

Move `_resolution`, `_terminal_decision`, `_validate_terminal_answers`, `_denial_response`, `_denial_decision`, and `_canonical_request_id` into `interactions.py`. Replace the service-bound methods with pure functions using the exact signatures `resolution(kind: InteractionKind, request: ServerRequest, allow: bool | None, answers: Mapping[str, str] | None) -> tuple[object, str]` and `terminal_decision(kind: InteractionKind, request: ServerRequest, result: object) -> str`.

Preserve exact response payload construction by calling `codex.app_server.responses`.

- [ ] **Step 6: Move the stateful service and update imports**

Move `CodexOrchestrator`, `_LiveInteraction`, consumer loops, session lifecycle, and emit logic into `service.py`. Keep the constructor and public async method signatures unchanged.

- [ ] **Step 7: Run orchestration tests**

Run: `python -m pytest tests/test_codex_orchestrator.py tests/test_codex_gateway.py tests/test_codex_interactive.py tests/test_daemon_core.py tests/test_lark_control.py tests/test_package_structure.py -q`

Expected: PASS.

- [ ] **Step 8: Commit the orchestration split**

```bash
git add src/lark_bot/codex/orchestration src/lark_bot/daemon.py src/lark_bot/server tests
git commit -m "refactor: split codex orchestration logic"
```

### Task 7: Split daemon authentication, runtime, and HTTP endpoints

**Files:**
- Create: `src/lark_bot/server/daemon/__init__.py`
- Create: `src/lark_bot/server/daemon/auth.py`
- Create: `src/lark_bot/server/daemon/runtime.py`
- Create: `src/lark_bot/server/daemon/app.py`
- Delete: `src/lark_bot/daemon.py`
- Modify: `src/lark_bot/commands/daemon.py` or current `cli.py`
- Modify: `tests/test_daemon_core.py`
- Modify: `tests/test_cli_codex.py`

- [ ] **Step 1: Add failing daemon boundary test**

```python
def test_daemon_separates_auth_runtime_and_routes() -> None:
    assert import_module("lark_bot.server.daemon.auth").ensure_daemon_token
    assert import_module("lark_bot.server.daemon.runtime").DaemonRuntime
    assert import_module("lark_bot.server.daemon.app").create_daemon_app
```

- [ ] **Step 2: Verify the boundary test fails**

Run: `python -m pytest tests/test_package_structure.py::test_daemon_separates_auth_runtime_and_routes -q`

Expected: FAIL because `lark_bot.server.daemon` does not exist.

- [ ] **Step 3: Extract authentication**

Move bounded body reading, token file creation, and bearer-token validation helpers into `auth.py`. Keep token permissions and comparison behavior unchanged.

- [ ] **Step 4: Extract runtime workers and composition**

Move `DaemonRuntime`, `_public_session`, `build_runtime`, Lark routing, spool draining, outbox delivery, expiry, rendering, and shutdown into `runtime.py`.

- [ ] **Step 5: Extract request models and routes**

Move `SessionCreate`, `InteractiveSessionCreate`, and `create_daemon_app` into `app.py`. Import the authentication dependency and runtime contract explicitly. Keep all paths, status codes, and response bodies unchanged.

- [ ] **Step 6: Update CLI imports and run daemon tests**

Run: `python -m pytest tests/test_daemon_core.py tests/test_cli_codex.py tests/test_lark_control.py tests/test_codex_orchestrator.py tests/test_package_structure.py -q`

Expected: PASS.

- [ ] **Step 7: Commit the daemon split**

```bash
git add src/lark_bot/server/daemon src/lark_bot/cli.py tests
git commit -m "refactor: split daemon runtime and api"
```

### Task 8: Split Typer commands and reduce `cli.py` to composition

**Files:**
- Create: `src/lark_bot/commands/__init__.py`
- Create: `src/lark_bot/commands/common.py`
- Create: `src/lark_bot/commands/tasks.py`
- Create: `src/lark_bot/commands/codex.py`
- Create: `src/lark_bot/commands/daemon.py`
- Create: `src/lark_bot/commands/hooks.py`
- Modify: `src/lark_bot/cli.py`
- Modify: `src/lark_bot/__main__.py` only if required to preserve invocation
- Modify: `tests/test_cli_codex.py`
- Modify: task/config/hook CLI tests

- [ ] **Step 1: Add failing command-ownership and stable-entry tests**

```python
def test_cli_is_a_thin_stable_composition_root() -> None:
    cli = import_module("lark_bot.cli")
    assert cli.app
    assert import_module("lark_bot.commands.tasks").register
    assert import_module("lark_bot.commands.codex").register
    assert import_module("lark_bot.commands.daemon").register
    assert import_module("lark_bot.commands.hooks").register
```

- [ ] **Step 2: Verify the command-module test fails**

Run: `python -m pytest tests/test_package_structure.py::test_cli_is_a_thin_stable_composition_root -q`

Expected: FAIL because `lark_bot.commands` does not exist.

- [ ] **Step 3: Extract shared command helpers**

Move `configure_logging`, `_emit_result`, `_validate_lark_settings`, and JSON notification conversion helpers into `commands/common.py`. Export non-private names used across command modules.

- [ ] **Step 4: Extract task and server commands**

In `commands/tasks.py`, define:

```python
def register(app: typer.Typer) -> None:
    app.command("run")(run_command_cli)
    app.command("send-test")(send_test)
    app.command("config")(config_command)
    app.command("codex-event")(codex_event)
    app.command("serve")(serve)
```

Move existing bodies without changing Typer option declarations or output.

- [ ] **Step 5: Extract Codex, daemon, and hook commands**

Each module exposes `register(app: typer.Typer) -> None`. `commands/codex.py` owns TUI/resume parsing and job subcommands; `commands/daemon.py` owns daemon startup and local daemon requests; `commands/hooks.py` owns hook management and callback ingestion.

- [ ] **Step 6: Compose the stable app in `cli.py`**

Keep the root application in `cli.py`; `commands.codex.register()` owns `_CodexFallbackGroup`, creates the Codex/job sub-apps, and returns the Codex app so hooks can attach beneath it:

```python
app = typer.Typer(help="Lark Bot: Lark/Feishu notifications for code agent tasks.")
register_task_commands(app)
register_daemon_commands(app)
codex_app = register_codex_commands(app)
register_hook_commands(codex_app)
```

Re-export `build_codex_notification_from_json` only if an existing external-facing test or documented usage still needs it. Update tests to patch canonical command-module globals.

- [ ] **Step 7: Run CLI tests and smoke help**

Run: `python -m pytest tests/test_cli_codex.py tests/test_hooks.py tests/test_config.py tests/test_runner.py tests/test_package_structure.py -q`

Run: `$env:PYTHONPATH='src'; python -m lark_bot --help`

Expected: tests PASS and help lists the same top-level commands.

- [ ] **Step 8: Commit the CLI split**

```bash
git add src/lark_bot/commands src/lark_bot/cli.py src/lark_bot/__main__.py tests
git commit -m "refactor: split cli command modules"
```

### Task 9: Enforce the final tree and update documentation

**Files:**
- Modify: `tests/test_package_structure.py`
- Modify: `README.md`
- Modify: `AGENTS.md` only if its project-structure list needs canonical paths; do not include the user's unrelated line change
- Delete: obsolete empty packages and root implementation files

- [ ] **Step 1: Add a failing root allowlist test**

Add:

```python
from pathlib import Path


def test_package_root_contains_only_entrypoints_and_shared_contracts() -> None:
    root = Path(__file__).parents[1] / "src" / "lark_bot"
    actual = {path.name for path in root.glob("*.py")}
    assert actual == {
        "__init__.py",
        "__main__.py",
        "cli.py",
        "config.py",
        "models.py",
        "redaction.py",
    }
```

- [ ] **Step 2: Verify the allowlist test fails before cleanup**

Run: `python -m pytest tests/test_package_structure.py::test_package_root_contains_only_entrypoints_and_shared_contracts -q`

Expected: FAIL and list remaining obsolete root modules.

- [ ] **Step 3: Remove obsolete modules and empty directories**

Delete only paths whose implementation and imports have moved. Run `rg` for each old module name before deletion and require zero source/test matches.

- [ ] **Step 4: Update reader-facing structure documentation**

Update README development notes and the committed portion of AGENTS project structure to name `commands/`, `codex/`, `lark/`, `tasks/`, `notifications/`, `storage/codex/`, and `server/daemon/`. Preserve the user's uncommitted AGENTS line by staging the documentation hunk selectively or leaving AGENTS out if isolation is unsafe.

- [ ] **Step 5: Run structural and import verification**

Run: `python -m pytest tests/test_package_structure.py -q`

Run: `$env:PYTHONPATH='src'; python -c "from lark_bot.cli import app; from lark_bot.server.app import app as server_app; print(bool(app), bool(server_app))"`

Expected: PASS and `True True`.

- [ ] **Step 6: Commit final structure and docs**

```bash
git add README.md src/lark_bot tests/test_package_structure.py
git commit -m "docs: document functional package layout"
```

### Task 10: Full regression, build, and final review

**Files:**
- Modify only files required to fix verified regressions

- [ ] **Step 1: Run the complete test suite**

Run: `python -m pytest`

Expected: all tests pass; the existing `httpx` deprecation warning may remain.

- [ ] **Step 2: Check formatting artifacts and stale imports**

Run: `git diff --check`

Run: `rg -n "lark_bot\.(codex_app_server|codex_gateway|codex_interactive|codex_models|codex_orchestrator|codex_tui|codex_hook_adapter|codex_remote_probe|daemon|lark_control|detector|runner|hooks|notifier|adapters)" src tests README.md`

Expected: no stale imports or commands except deliberate historical references in design/report documents.

- [ ] **Step 3: Verify stable entry points**

Run: `$env:PYTHONPATH='src'; python -m lark_bot --help`

Run: `$env:PYTHONPATH='src'; python -c "from uvicorn.importer import import_from_string; assert import_from_string('lark_bot.server.app:app')"`

Expected: both commands exit 0.

- [ ] **Step 4: Build and inspect the wheel**

Run: `python -m build`

Run: `python -m zipfile -l (Get-ChildItem dist\lark_bot-*.whl | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName`

Expected: build succeeds and the wheel contains all new package modules and `__init__.py` files.

- [ ] **Step 5: Review the final diff and working tree**

Run: `git diff dev...HEAD --stat`

Run: `git status --short --branch`

Expected: only the user's pre-existing `AGENTS.md` modification and `CLAUDE.md` remain outside committed work.

- [ ] **Step 6: Commit any isolated regression fixes**

```bash
git add -u src/lark_bot tests README.md
git add src/lark_bot tests README.md
git commit -m "fix: preserve behavior after package refactor"
```

Skip this commit when no regression fix is required.
