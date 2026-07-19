# Claude Code Capability Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Claude Code the same user-facing notification, Hook, managed-session, cancellation, resume, durable delivery, and Lark interaction capabilities as Codex through Claude-native supported interfaces.

**Architecture:** Ordinary terminal sessions use nonblocking Claude Code command Hooks. Managed sessions use an injected wrapper over the official Python Agent SDK. Provider-neutral session, interaction, outbox, audit, daemon API, and Lark routing behavior moves into `modules/agent`; Codex keeps a compatibility facade and its app-server protocol.

**Tech Stack:** Python 3.11+, Pydantic v2, Typer, FastAPI, SQLite, `claude-agent-sdk>=0.1.0,<1`, pytest, Claude Code 2.1.214.

---

## File Map

Create these focused modules:

- `src/lark_bot/modules/agent/agent_schema.py`: canonical Agent schema, legacy migration, and Codex mirror triggers.
- `src/lark_bot/modules/agent/agent_mapper.py`: shared row conversion, UTC serialization, redaction, and bounds.
- `src/lark_bot/modules/claude/claude_hook_adapter.py`: bounded Hook input, safe-field normalization, event identity, delivery and spool.
- `src/lark_bot/modules/claude/claude_hook_installer.py`: atomic settings merge/check/uninstall.
- `src/lark_bot/modules/claude/claude_sdk.py`: lazy third-party SDK bridge behind internal protocols.
- `src/lark_bot/modules/claude/claude_session_manager.py`: managed sessions, result consumer, live permissions, expiry, and cancellation.
- `src/lark_bot/modules/claude/claude_tui.py`: native executable passthrough.
- `tests/test_agent_storage.py`: canonical schema, transactions, migration, mirror, and privacy.
- `tests/test_claude_hooks.py`: official Hook fixtures and safe callback behavior.
- `tests/test_claude_hook_installer.py`: settings ownership and atomicity.
- `tests/test_claude_sdk.py`: lazy bridge translation.
- `tests/test_claude_sessions.py`: managed lifecycle and interactions with fakes.
- `tests/test_agent_api.py`: provider-neutral authenticated daemon API.
- `tests/test_cli_claude.py`: terminal, job, Hook, and callback CLI behavior.

The existing `commands/app.py` and `server/daemon/app.py` remain composition
roots. Do not add provider imports to shared Agent, task, notification, or Lark
modules.

### Task 1: Replace Preliminary Events With Real Hook Semantics

**Files:**

- Modify: `src/lark_bot/modules/task/task_model.py`
- Modify: `src/lark_bot/modules/notification/notification_model.py`
- Modify: `src/lark_bot/modules/notification/notification_builder.py`
- Modify: `src/lark_bot/modules/claude/claude_model.py`
- Modify: `src/lark_bot/modules/claude/claude_adapter.py`
- Modify: `src/lark_bot/modules/claude/claude_service.py`
- Modify: `tests/test_claude_adapter.py`
- Create: `tests/test_claude_hooks.py`

- [x] **Step 1: Add failing official-payload tests**

Add fixtures containing only official Hook fields. The core assertions are:

```python
def test_stop_is_completed_without_claiming_process_success() -> None:
    request = build_claude_notification_from_json(json.dumps({
        "session_id": "session-1",
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "last_assistant_message": "done",
    }))
    assert request.detection.status is TaskStatus.COMPLETED
    assert request.task.exit_code == 0
    assert "turn_completed" in request.detection.tags


def test_stop_failure_is_failed() -> None:
    request = build_claude_notification_from_json(json.dumps({
        "session_id": "session-1",
        "hook_event_name": "StopFailure",
        "error": "rate_limit",
        "error_details": "secret detail",
    }))
    assert request.detection.status is TaskStatus.FAILED
    assert request.task.stderr_tail == []
    assert "secret detail" not in request.model_dump_json()


def test_user_prompt_submit_is_observational_not_waiting() -> None:
    request = build_claude_notification_from_json(json.dumps({
        "session_id": "session-1",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "secret prompt",
    }))
    assert request.detection.status is TaskStatus.COMPLETED
    assert "waiting_for_input" not in request.detection.tags
    assert "secret prompt" not in request.model_dump_json()


def test_distinct_permission_events_have_distinct_dedupe_keys() -> None:
    first = build_claude_notification_from_json(permission_payload("prompt-1"))
    second = build_claude_notification_from_json(permission_payload("prompt-2"))
    assert first.dedupe_key != second.dedupe_key
```

Also cover `SessionStart`, `Notification` types `permission_prompt`,
`idle_prompt`, `agent_needs_input`, and `agent_completed`, `PermissionRequest`,
and `SessionEnd`. Assert unsupported event names fail closed.

- [x] **Step 2: Run the tests and verify the expected failures**

Run:

```powershell
python -m pytest tests/test_claude_adapter.py tests/test_claude_hooks.py -q
```

Expected: failures for missing `TaskStatus.COMPLETED`, unsupported official
events, incorrect `UserPromptSubmit` waiting status, and identical dedupe keys.

- [x] **Step 3: Implement the real event model and stable event identity**

Extend the shared status and notification request without changing old keys:

```python
class TaskStatus(StrEnum):
    SUCCEEDED = "succeeded"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING_FOR_INPUT = "waiting_for_input"


class AgentNotificationInput(BaseModel):
    # existing fields remain unchanged
    event_id: str | None = None


class NotificationRequest(BaseModel):
    task: TaskResult
    detection: DetectionResult
    context: NotificationContext | None = None
    event_id: str | None = None

    @property
    def dedupe_key(self) -> str:
        if self.event_id:
            return f"{self.task.source}:{self.event_id}"
        command_text = " ".join(self.task.command)
        session = self.context.session_id if self.context else "-"
        return f"{self.task.source}:{session}:{self.task.name}:{command_text}:{self.detection.status}"
```

Replace synthetic `status`, `exit_code`, duration, command, and output fields in
`ClaudeEvent` with the verified safe Hook fields:

```python
class ClaudeEvent(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    hook_event_name: str = Field(min_length=1, max_length=100)
    prompt_id: str | None = Field(default=None, max_length=200)
    source: str | None = Field(default=None, max_length=100)
    reason: str | None = Field(default=None, max_length=100)
    notification_type: str | None = Field(default=None, max_length=100)
    title: str | None = Field(default=None, max_length=200)
    message: str | None = Field(default=None, max_length=1000)
    error: str | None = Field(default=None, max_length=100)
    stop_hook_active: bool | None = None
```

Keep input aliases `event_name -> hook_event_name` and `sessionId ->
session_id` for the public `claude-event` compatibility surface. Ignore all
extra fields. Derive `event_id` as SHA-256 of the bounded tuple
`(session_id, prompt_id or "-", hook_event_name, notification_type or source or
reason or error or "-")`. Map `PermissionRequest` and action-required
`Notification` values to waiting; map `StopFailure` to failed; map observation,
turn completion, and lifecycle events to completed. Never use prompt,
`tool_input`, transcript, CWD, assistant output, or error details as output.

- [x] **Step 4: Run the focused and shared notification tests**

Run:

```powershell
python -m pytest tests/test_claude_adapter.py tests/test_claude_hooks.py tests/test_notification_modules.py tests/test_lark_render.py -q
```

Expected: all pass, including old Codex notification behavior.

- [x] **Step 5: Commit the event semantics**

```powershell
git add src/lark_bot/modules/task src/lark_bot/modules/notification src/lark_bot/modules/claude tests/test_claude_adapter.py tests/test_claude_hooks.py
git commit -m "功能: 完善事件语义"
```

### Task 2: Add Auditable Hooks And A Nonblocking Callback

**Files:**

- Create: `src/lark_bot/modules/claude/claude_hook_installer.py`
- Create: `src/lark_bot/modules/claude/claude_hook_adapter.py`
- Modify: `src/lark_bot/modules/claude/claude_hook.py`
- Modify: `src/lark_bot/modules/claude/__init__.py`
- Create: `tests/test_claude_hook_installer.py`
- Modify: `tests/test_claude_hooks.py`

- [x] **Step 1: Add failing installer ownership tests**

Use a settings fixture with unrelated permissions and Hooks. Assert:

```python
def test_install_preserves_unrelated_settings_and_is_idempotent(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({
        "permissions": {"allow": ["Read"]},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]},
    }), encoding="utf-8")

    install_hooks(tmp_path)
    first = settings.read_text(encoding="utf-8")
    install_hooks(tmp_path)

    value = json.loads(first)
    assert value["permissions"] == {"allow": ["Read"]}
    assert value["hooks"]["Stop"][0]["hooks"][0]["command"] == "other"
    assert settings.read_text(encoding="utf-8") == first


def test_uninstall_removes_only_owned_handlers(tmp_path: Path) -> None:
    install_hooks(tmp_path)
    uninstall_hooks(tmp_path)
    assert check_hooks(tmp_path).status == "missing"
```

Also assert malformed JSON is unchanged, symlinks are refused, modified owned
handlers report `modified`, and an absent file is handled idempotently.

- [x] **Step 2: Add failing callback privacy and outage tests**

```python
def test_safe_hook_payload_drops_sensitive_fields() -> None:
    safe = normalize_callback(stdin=json.dumps({
        "session_id": "s1",
        "prompt_id": "p1",
        "hook_event_name": "PermissionRequest",
        "transcript_path": "secret-path",
        "cwd": "secret-cwd",
        "tool_input": {"command": "secret-command"},
        "permission_suggestions": [{"secret": "value"}],
    }))
    assert safe == {
        "agent": "claude",
        "session_id": "s1",
        "prompt_id": "p1",
        "hook_event_name": "PermissionRequest",
        "event_id": safe["event_id"],
    }
    assert "secret" not in json.dumps(safe)


def test_delivery_failure_spools_only_safe_payload(tmp_path: Path) -> None:
    assert handle_callback(stdin=payload, sender=unavailable, spool_dir=tmp_path)
    persisted = json.loads(next(tmp_path.glob("hook-*.json")).read_text())
    assert persisted == normalize_callback(stdin=payload)
```

- [x] **Step 3: Verify both new test files fail for missing modules**

Run:

```powershell
python -m pytest tests/test_claude_hook_installer.py tests/test_claude_hooks.py -q
```

Expected: import failures for installer and callback functions.

- [x] **Step 4: Implement exact owned settings entries and atomic writes**

Use this handler for each event in `SessionStart`, `Notification`,
`PermissionRequest`, `Stop`, `StopFailure`, and `SessionEnd`:

```python
OWNED_HANDLER = {
    "type": "command",
    "command": "lark-bot",
    "args": ["claude-hook"],
    "async": True,
    "timeout": 10,
}


@dataclass(frozen=True)
class HookCheck:
    status: Literal["installed", "missing", "modified", "malformed"]
    detail: str = ""
```

Read JSON through `json.loads`, require a top-level object, merge only exact
owned handler objects, write a sibling temporary file with UTF-8 and `\n`, then
replace with `Path.replace()`. Refuse symlinks before every write. Uninstall
removes only objects equal to `OWNED_HANDLER` and prunes empty matcher/event
containers.

Implement `normalize_callback()` with the safe-field allow-list from the design
and delegate delivery to `agent_hook.deliver_sanitized_hook`. Return immediately
when `LARK_BOT_CLAUDE_HOOK_DISABLED=1`.

- [x] **Step 5: Run Hook tests and prove spool privacy**

Run:

```powershell
python -m pytest tests/test_claude_hook_installer.py tests/test_claude_hooks.py tests/test_agent_hooks.py -q
```

Expected: all pass; spool JSON contains no transcript, prompt, tool input, CWD,
or assistant output.

- [x] **Step 6: Commit the Hook layer**

```powershell
git add src/lark_bot/modules/claude tests/test_claude_hook_installer.py tests/test_claude_hooks.py
git commit -m "功能: 增加项目Hook接入"
```

### Task 3: Move Durable State Into The Shared Agent Layer

**Files:**

- Modify: `src/lark_bot/modules/agent/agent_model.py`
- Modify: `src/lark_bot/modules/agent/agent_store.py`
- Create: `src/lark_bot/modules/agent/agent_schema.py`
- Create: `src/lark_bot/modules/agent/agent_mapper.py`
- Modify: `src/lark_bot/modules/agent/__init__.py`
- Modify: `src/lark_bot/modules/codex/codex_schema.py`
- Modify: `src/lark_bot/modules/codex/codex_mapper.py`
- Modify: `src/lark_bot/modules/codex/codex_store.py`
- Modify: `src/lark_bot/modules/codex/codex_model.py`
- Create: `tests/test_agent_storage.py`
- Modify: `tests/test_codex_storage.py`
- Modify: `tests/test_package_structure.py`

- [x] **Step 1: Add failing shared schema and model round-trip tests**

Define tests for physical `agent_sessions`, `agent_interactions`,
`agent_event_dedupe`, `agent_notification_outbox`, and `agent_audit` tables;
agent-scoped list/get/conversation lookup; safe summaries; and no imports from
provider packages.

The shared model additions retain defaults so existing minimal constructors
continue to work:

```python
class AgentSession(SessionRef):
    conversation_id: str | None = None
    turn_id: str | None = None
    cwd: str = ""
    model: str | None = None
    sandbox: str = "workspace-write"
    permission_mode: str | None = None
    status: SessionStatus
    summary: str = ""
    created_at: datetime
    updated_at: datetime


class AgentInteraction(BaseModel):
    interaction_id: str
    session_id: str
    request_id: str
    kind: InteractionKind
    status: InteractionStatus = InteractionStatus.PENDING
    lark_message_id: str | None = None
    payload_summary: str = ""
    requested_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    resolved_at: datetime | None = None
    expires_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    actor_id: str | None = None
    decision: InteractionDecision | None = None
```

Managers and adapters always supply explicit request and expiry timestamps;
the defaults preserve the existing lightweight public constructor used by
package contract tests.

- [x] **Step 2: Verify shared storage tests fail**

Run:

```powershell
python -m pytest tests/test_agent_storage.py tests/test_agent_modules.py -q
```

Expected: failures for missing `SQLiteAgentStore` and canonical tables.

- [x] **Step 3: Implement schema version 4 and canonical tables**

Move schema coordination to `agent_schema.py`; `codex_schema.py` re-exports the
same `SCHEMA_VERSION`, `MIGRATIONS`, and `initialize_schema`. Version 4 must:

```sql
CREATE TABLE agent_sessions (
  id TEXT PRIMARY KEY, agent TEXT NOT NULL, conversation_id TEXT, turn_id TEXT,
  name TEXT NOT NULL, cwd TEXT NOT NULL, model TEXT, sandbox TEXT NOT NULL,
  permission_mode TEXT, status TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE agent_interactions (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES agent_sessions(id),
  request_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
  lark_message_id TEXT, payload_summary TEXT NOT NULL DEFAULT '',
  requested_at TEXT NOT NULL, resolved_at TEXT, expires_at TEXT NOT NULL,
  actor_id TEXT, decision TEXT
);
CREATE UNIQUE INDEX idx_agent_interactions_pending_request
  ON agent_interactions(request_id) WHERE status = 'pending';
```

Create equivalent canonical dedupe, outbox, and audit tables. Copy every legacy
row in the same `BEGIN IMMEDIATE` transaction with `agent='codex'`, preserving
IDs and timestamps. For legacy outbox rows with null identity, derive
`session_name` with a left join. Before copy, create missing legacy v1 tables so
partial old databases migrate. Run with foreign keys disabled, commit, re-enable
them, and verify tests call `PRAGMA foreign_key_check`.

- [x] **Step 4: Move the tested transaction behavior into `SQLiteAgentStore`**

Move the current `SQLiteCodexStore` method bodies without changing transaction
boundaries, substituting the canonical table/model names. The public shared API
must include:

```python
class AgentStoreContract(Protocol):
    def create_session(self, session: AgentSession) -> None:
        raise NotImplementedError
    def get_session(self, session_id: str, *, agent: AgentKind | None = None) -> AgentSession | None:
        raise NotImplementedError
    def get_session_by_conversation(self, conversation_id: str, *, agent: AgentKind | None = None) -> AgentSession | None:
        raise NotImplementedError
    def list_sessions(self, status: SessionStatus | None = None, *, agent: AgentKind | None = None) -> list[AgentSession]:
        raise NotImplementedError
    def update_session(self, session_id: str, **changes: object) -> AgentSession | None:
        raise NotImplementedError
    def update_session_if_status(self, session_id: str, expected_statuses: tuple[SessionStatus, ...], **changes: object) -> bool:
        raise NotImplementedError
    def create_interaction_and_mark_waiting(self, interaction: AgentInteraction, waiting_status: SessionStatus, updated_at: datetime) -> bool:
        raise NotImplementedError
    def resolve_interaction_and_refresh_session(self, interaction_id: str, *, decision: str, actor_id: str, updated_at: datetime, status: InteractionStatus = InteractionStatus.RESOLVED) -> bool:
        raise NotImplementedError
    def claim_session_terminal(self, session_id: str, terminal_status: SessionStatus, summary: str, pending_status: InteractionStatus, updated_at: datetime, *, agent: AgentKind | None = None) -> list[str] | None:
        raise NotImplementedError
    def reconcile_startup(self, *, now: datetime | None = None, agent: AgentKind | None = None) -> StartupReconciliationResult:
        raise NotImplementedError
```

Also move the existing interaction lookup/attach/resolve/cancel/expire,
interactive turn, event dedupe, outbox, audit, connection, close, and context
manager methods with the signatures inventoried in the design review. Filter
reconciliation by agent so Codex startup cannot interrupt Claude rows.

- [x] **Step 5: Add red tests for first-winner transactions and migration**

Copy the existing concurrent resolver pattern into `test_agent_storage.py` and
assert exactly one winner. Build a version-3 database with one row of every
legacy type, open `SQLiteAgentStore`, and assert exact canonical copies. Add a
partial version-1 fixture missing dedupe/audit tables. Assert raw replies become
`submitted` and never persist.

- [x] **Step 6: Implement the Codex compatibility facade and mirror triggers**

`SQLiteCodexStore` owns one `SQLiteAgentStore` and maps between `CodexSession` /
`PendingInteraction` and shared models. Preserve every old method signature;
`get_session_by_thread(thread_id)` delegates to
`get_session_by_conversation(thread_id, agent=AgentKind.CODEX)` and
`list_sessions()` is always Codex-scoped.

Keep physical legacy tables. Add canonical-table triggers for Codex rows so
inserts and updates mirror into `codex_sessions`, `codex_interactions`,
`codex_event_dedupe`, `notification_outbox`, and `codex_audit`. This preserves
old database inspection and the direct SQL privacy assertions. Claude rows must
never enter those legacy tables.

- [x] **Step 7: Run shared and complete Codex persistence regression**

Run:

```powershell
python -m pytest tests/test_agent_storage.py tests/test_agent_modules.py tests/test_codex_storage.py tests/test_codex_orchestrator.py tests/test_package_structure.py -q
```

Expected: all pass, physical legacy tables remain visible, and canonical rows
contain both providers safely.

- [x] **Step 8: Commit shared persistence**

```powershell
git add src/lark_bot/modules/agent src/lark_bot/modules/codex tests/test_agent_storage.py tests/test_agent_modules.py tests/test_codex_storage.py tests/test_package_structure.py
git commit -m "重构: 统一会话持久化"
```

### Task 4: Add A Lazy Official SDK Bridge

**Files:**

- Modify: `pyproject.toml`
- Create: `src/lark_bot/modules/claude/claude_sdk.py`
- Create: `tests/test_claude_sdk.py`

- [x] **Step 1: Add failing facade tests without importing the SDK globally**

Assert importing `lark_bot.cli` succeeds when an injected importer raises
`ModuleNotFoundError`. Inject a fake SDK module and assert option, result, and
permission translation.

- [x] **Step 2: Verify the facade tests fail**

Run:

```powershell
python -m pytest tests/test_claude_sdk.py -q
```

Expected: missing `claude_sdk` module and internal protocol types.

- [x] **Step 3: Define the internal protocol and normalized messages**

```python
JsonValue = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True)
class ClaudePermissionResult:
    allowed: bool
    updated_input: Mapping[str, JsonValue] | None = None
    message: str | None = None


@dataclass(frozen=True)
class ClaudeSdkOptions:
    cwd: str
    model: str | None
    permission_mode: str | None
    resume: str | None
    session_id: str
    can_use_tool: CanUseTool


@dataclass(frozen=True)
class ClaudeSdkResult:
    session_id: str
    subtype: str
    is_error: bool
    duration_ms: int
    result: str | None
    errors: tuple[str, ...] = ()


class ClaudeSdkClient(Protocol):
    async def connect(self) -> None:
        raise NotImplementedError
    async def query(self, prompt: str) -> None:
        raise NotImplementedError
    def receive_response(self) -> AsyncIterator[ClaudeSdkMessage]:
        raise NotImplementedError
    async def interrupt(self) -> None:
        raise NotImplementedError
    async def close(self) -> None:
        raise NotImplementedError


class ClaudeSdkClientFactory(Protocol):
    def __call__(self, options: ClaudeSdkOptions) -> ClaudeSdkClient:
        raise NotImplementedError
```

Only `ClaudeAgentSdkBridge.__call__()` imports `ClaudeSDKClient`,
`ClaudeAgentOptions`, `PermissionResultAllow`, and `PermissionResultDeny`.
Convert official callbacks and message classes there. Do not leak third-party
types into the manager.

- [x] **Step 4: Add the dependency and pass facade tests**

Add `"claude-agent-sdk>=0.1.0,<1"` to project dependencies. Run:

```powershell
python -m pytest tests/test_claude_sdk.py tests/test_package_structure.py -q
```

Expected: all pass without credentials or a model request.

- [x] **Step 5: Commit the SDK boundary**

```powershell
git add pyproject.toml src/lark_bot/modules/claude/claude_sdk.py tests/test_claude_sdk.py
git commit -m "功能: 增加托管运行桥接"
```

### Task 5: Implement Managed Session Lifecycle

**Files:**

- Create: `src/lark_bot/modules/claude/claude_session_manager.py`
- Modify: `src/lark_bot/modules/claude/claude_service.py`
- Modify: `src/lark_bot/modules/agent/agent_protocol.py`
- Modify: `src/lark_bot/modules/agent/agent_service.py`
- Create: `tests/test_claude_sessions.py`

- [x] **Step 1: Write failing lifecycle tests with fake clients**

The fake records call order and yields normalized result messages. Assert the
store sees `STARTING` before factory/connect, the prompt appears only in
`query()`, success/error result mapping is safe, cancel calls interrupt and
drains, close is idempotent, and restart reconciliation touches only Claude.

Resume is explicit: `start --resume PROVIDER_SESSION_ID` creates a new local
session ID and stores the provider ID in `conversation_id`; IDs are never
conflated.

- [x] **Step 2: Verify lifecycle tests fail**

Run:

```powershell
python -m pytest tests/test_claude_sessions.py -q
```

Expected: import failure for `ClaudeSessionManager`.

- [x] **Step 3: Implement the manager surface**

```python
class ClaudeSessionManagerContract(Protocol):
    async def start(self) -> None:
        raise NotImplementedError
    async def close(self) -> None:
        raise NotImplementedError
    async def create_session(
        self, name: str, cwd: str, prompt: str, *, model: str | None = None,
        permission_mode: str | None = None, resume_id: str | None = None,
    ) -> AgentSession:
        raise NotImplementedError
    async def list_sessions(self, status: SessionStatus | None = None) -> list[AgentSession]:
        raise NotImplementedError
    async def get_session(self, session_id: str) -> AgentSession | None:
        raise NotImplementedError
    async def cancel_session(self, session_id: str) -> bool:
        raise NotImplementedError
    async def resolve_interaction(self, interaction_id: str, actor_id: str, *, allow: bool | None = None, answers: Mapping[str, str] | None = None) -> bool:
        raise NotImplementedError
    def get_user_input_question_ids(self, interaction_id: str) -> tuple[str, ...]:
        raise NotImplementedError
    async def expire_due_interactions(self, now: datetime | None = None) -> list[str]:
        raise NotImplementedError
```

Persist before client construction, connect before marking running, then create
a named task that calls query and drains `receive_response()`. Map success to
`SUCCEEDED`, SDK errors to `FAILED`, explicit interrupt to `CANCELLED`, and
daemon loss to `INTERRUPTED`. Enqueue outbox events with `agent='claude'` and
safe bounded summaries. Keep prompts and message bodies only in memory.

Extend `AgentAdapter` with list/get/resolve/question-ID/expiry methods. Make
`ClaudeService` delegate to the manager and `CodexService` delegate equivalent
methods to its existing orchestrator/store.

- [x] **Step 4: Run lifecycle, service, and concurrency tests**

Run:

```powershell
python -m pytest tests/test_claude_sessions.py tests/test_agent_modules.py tests/test_session_concurrency.py tests/test_codex_orchestrator.py -q
```

Expected: all pass.

- [x] **Step 5: Commit managed lifecycle**

```powershell
git add src/lark_bot/modules/agent src/lark_bot/modules/claude src/lark_bot/modules/codex/codex_service.py tests/test_claude_sessions.py tests/test_agent_modules.py
git commit -m "功能: 增加托管会话生命周期"
```

### Task 6: Generalize Lark Interaction Resolution

**Files:**

- Modify: `src/lark_bot/modules/claude/claude_session_manager.py`
- Modify: `src/lark_bot/modules/agent/agent_service.py`
- Modify: `src/lark_bot/modules/lark/lark_router.py`
- Modify: `src/lark_bot/modules/lark/lark_render.py`
- Modify: `tests/test_claude_sessions.py`
- Modify: `tests/test_lark_control.py`
- Modify: `tests/test_lark_render.py`

- [x] **Step 1: Add failing permission, input, race, and fail-closed tests**

For a fake SDK `can_use_tool` call, assert `Bash` creates an approval and
`AskUserQuestion` creates a user-input interaction. Test Lark allow/deny,
answers injected into ephemeral `updated_input`, timeout deny, shutdown deny,
first-response-wins, late reply ignored/audited, and no raw command/question or
answer in SQLite.

- [x] **Step 2: Verify interaction tests fail**

Run:

```powershell
python -m pytest tests/test_claude_sessions.py tests/test_lark_control.py tests/test_lark_render.py -q
```

Expected: Claude resolver and provider dispatch failures.

- [x] **Step 3: Add live permission futures to the manager**

Use an in-memory record containing the future and original tool input:

```python
@dataclass(slots=True)
class _LivePermission:
    interaction: AgentInteraction
    tool_name: str
    input_data: Mapping[str, JsonValue]
    question_ids: tuple[str, ...]
    result: asyncio.Future[ClaudePermissionResult]
```

Generate a UUID request ID. Persist only a bounded tool name and generic
description. On approval resolve `ClaudePermissionResult(True,
updated_input=input_data)`. For `AskUserQuestion`, copy input in memory and add
`answers` before returning. On deny/timeout/shutdown return
`ClaudePermissionResult(False, message="denied")`. Resolve the store claim
before completing the future so one responder wins.

- [x] **Step 4: Dispatch Lark replies by stored session provider**

Add `AgentInteractionDispatcher` to look up interaction, session, and adapter:

```python
class AgentInteractionDispatcher:
    async def resolve_interaction(self, interaction_id: str, actor_id: str, **resolution: object) -> bool:
        interaction = self.store.get_interaction(interaction_id)
        session = self.store.get_session(interaction.session_id) if interaction else None
        if session is None:
            return False
        return await self.registry.get(session.agent).resolve_interaction(
            interaction_id, actor_id, **resolution
        )

    def get_user_input_question_ids(self, interaction_id: str) -> tuple[str, ...]:
        interaction = self.store.get_interaction(interaction_id)
        session = self.store.get_session(interaction.session_id) if interaction else None
        if session is None:
            return ()
        return self.registry.get(session.agent).get_user_input_question_ids(interaction_id)
```

Make `LarkControlRouter` depend on this dispatcher rather than Codex. Keep all
existing reply/reaction parsing unchanged. Render headings from
`SessionDisplay.agent`, with provider-neutral Chinese approval/input text and
existing Codex snapshot compatibility.

- [x] **Step 5: Run interaction regressions**

Run:

```powershell
python -m pytest tests/test_claude_sessions.py tests/test_lark_control.py tests/test_lark_render.py tests/test_codex_orchestrator.py -q
```

Expected: all pass, including races and fail-closed behavior.

- [x] **Step 6: Commit provider-neutral interactions**

```powershell
git add src/lark_bot/modules/agent src/lark_bot/modules/claude src/lark_bot/modules/lark tests/test_claude_sessions.py tests/test_lark_control.py tests/test_lark_render.py
git commit -m "重构: 统一交互响应路由"
```

### Task 7: Add Provider-Neutral Daemon APIs

**Files:**

- Modify: `src/lark_bot/server/daemon/app.py`
- Modify: `src/lark_bot/server/daemon/runtime.py`
- Modify: `src/lark_bot/server/daemon/__init__.py`
- Create: `tests/test_agent_api.py`
- Modify: `tests/test_daemon_core.py`

- [x] **Step 1: Add failing authenticated dispatch tests**

Cover POST/list/get/cancel for `/api/v1/agents/claude/sessions`, invalid
providers, status filter, `resume_id`, prompt exclusion, generic Hook ingestion,
Codex compatibility aliases, independent health degradation, outbox delivery,
and one shared expiry loop.

- [x] **Step 2: Verify API tests fail**

Run:

```powershell
python -m pytest tests/test_agent_api.py tests/test_daemon_core.py -q
```

Expected: 404 for generic routes and missing provider health.

- [x] **Step 3: Add generic request models and routes**

```python
class AgentSessionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    cwd: str = Field(min_length=1, max_length=4096)
    prompt: str = Field(min_length=1, max_length=1_000_000, repr=False)
    model: str | None = Field(default=None, max_length=200)
    sandbox: str = Field(default="workspace-write", pattern="^(read-only|workspace-write)$")
    permission_mode: str | None = Field(default=None, max_length=100)
    resume_id: str | None = Field(default=None, max_length=200)
```

Use `AgentRegistry` for `/api/v1/agents/{agent}/sessions`; never return prompt.
Keep `/api/v1/codex/*` wrappers and interactive routes unchanged for existing
clients. Generic Hook ingress accepts only the safe callback mapping and derives
the provider from the path, not an untrusted body field.

- [x] **Step 4: Generalize runtime workers and health**

The expiry worker iterates registered adapters and isolates exceptions per
provider. The outbox worker polls the shared store; provider event queues are
only wake hints. Spool drain validates `agent`, event, and safe fields. Health
returns independent `codex`, `claude`, and `lark` states. A Claude SDK startup
failure must not stop Codex or Hook/outbox delivery.

Build one `SQLiteAgentStore`, the Codex compatibility facade, Codex
orchestrator, Claude manager, registry, dispatcher, Lark router, and Lark client.
Close each owned resource exactly once.

- [x] **Step 5: Run API, daemon, storage, and Codex compatibility tests**

Run:

```powershell
python -m pytest tests/test_agent_api.py tests/test_daemon_core.py tests/test_agent_storage.py tests/test_cli_codex.py tests/test_codex_orchestrator.py -q
```

Expected: all pass.

- [x] **Step 6: Commit daemon dispatch**

```powershell
git add src/lark_bot/server/daemon tests/test_agent_api.py tests/test_daemon_core.py
git commit -m "功能: 增加多模型服务接口"
```

### Task 8: Complete The CLI And Native Launcher

**Files:**

- Create: `src/lark_bot/modules/claude/claude_tui.py`
- Modify: `src/lark_bot/commands/app.py`
- Modify: `src/lark_bot/cli.py`
- Modify: `src/lark_bot/modules/claude/__init__.py`
- Create: `tests/test_cli_claude.py`
- Modify: `tests/test_claude_hooks.py`
- Modify: `tests/test_cli_codex.py`

- [x] **Step 1: Add failing CLI parity tests**

Assert native arguments including `--resume`, `--continue`, `--model`, and
permission mode pass through; `--no-lark` avoids daemon and sets
`LARK_BOT_CLAUDE_HOOK_DISABLED=1`; job start supports prompt stdin and
`--resume`; list/show/cancel use agent routes; hooks commands call the installer;
callback reads bounded stdin, uses a 0.25-second HTTP timeout, spools safely, and
always exits zero.

- [x] **Step 2: Verify CLI tests fail**

Run:

```powershell
python -m pytest tests/test_cli_claude.py tests/test_cli_codex.py -q
```

Expected: missing Claude Typer group and callback command.

- [x] **Step 3: Implement native launcher and Typer namespaces**

```python
@dataclass(frozen=True)
class ClaudeTuiOptions:
    args: list[str]
    claude_path: str = "claude"
    env: Mapping[str, str] | None = None


class ClaudeTuiLauncher:
    def run(self, options: ClaudeTuiOptions) -> int:
        completed = subprocess.run(
            [options.claude_path, *options.args],
            env=dict(options.env) if options.env is not None else None,
            check=False,
        )
        return int(completed.returncode)
```

Add `claude_app`, `claude_job_app`, and `claude_hooks_app`. Do not auto-edit
settings from the launcher; explicit `claude hooks install` owns configuration.
For `--no-lark`, pass the current environment plus the disable variable so an
already installed owned Hook becomes a no-op without disabling user Hooks.

Generalize daemon HTTP helper to accept `agent`; retain `_daemon_request()` as a
Codex compatibility wrapper. `claude-hook` calls the safe adapter and generic
Hook endpoint, swallows callback failures, and never chains arbitrary commands.

- [x] **Step 4: Run CLI and Hook regressions**

Run:

```powershell
python -m pytest tests/test_cli_claude.py tests/test_claude_hooks.py tests/test_claude_hook_installer.py tests/test_cli_codex.py -q
```

Expected: all pass.

- [x] **Step 5: Commit CLI parity**

```powershell
git add src/lark_bot/commands src/lark_bot/cli.py src/lark_bot/modules/claude tests/test_cli_claude.py tests/test_claude_hooks.py tests/test_cli_codex.py
git commit -m "功能: 增加第二终端入口"
```

### Task 9: Configuration, Documentation, And Completion Audit

**Files:**

- Modify: `src/lark_bot/core/config.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `tests/test_config.py`
- Modify: `tests/test_package_structure.py`
- Modify: `docs/superpowers/plans/2026-07-19-claude-parity.md`

- [x] **Step 1: Add failing safe configuration checks**

Add `claude_path: str = "claude"` and checks for executable discovery and SDK
package availability. Tests monkeypatch discovery/import metadata and assert no
path, token, credential, or exception detail leaks through `config --json`.

- [x] **Step 2: Verify config tests fail**

Run:

```powershell
python -m pytest tests/test_config.py tests/test_package_structure.py -q
```

Expected: missing Claude path/SDK checks and new package exports.

- [x] **Step 3: Implement diagnostics and usage documentation**

Document:

```powershell
lark-bot claude hooks install --project .
lark-bot claude
lark-bot claude job start --name build --cwd . "run tests"
lark-bot claude job start --resume <provider-session-id> "continue"
lark-bot claude job list --json
lark-bot claude job cancel <local-session-id>
claude --version
```

Explain ordinary Hook notifications versus managed bidirectional sessions,
explicit install/uninstall, fail-closed approval timeouts, no-cost automated
tests, and the no-model-call version smoke check. Correct the current README
claim that Claude managed sessions already exist before this branch.

- [x] **Step 4: Run the full verification matrix**

Run:

```powershell
python -m pytest
git diff --check
rg -n "lark_bot\.modules\.(codex|claude)" src/lark_bot/modules/agent src/lark_bot/modules/task src/lark_bot/modules/notification src/lark_bot/modules/lark
claude --version
```

Expected: all tests pass; `git diff --check` passes; provider-import search has
no results; version reports the installed Claude Code without a model request.

- [x] **Step 5: Run requirement-by-requirement completion audit**

Record evidence in the implementation plan checkboxes for every parity-matrix
row: native launch, Hook notify, installer, structured event CLI, managed CRUD,
resume, approval/input, shared persistence/outbox, Lark routing, and health.
Treat missing or indirect evidence as incomplete and fix it before closing.

- [x] **Step 6: Commit final docs and diagnostics**

```powershell
git add src/lark_bot/core/config.py .env.example README.md tests/test_config.py tests/test_package_structure.py docs/superpowers/plans/2026-07-19-claude-parity.md
git commit -m "文档: 完善多模型使用说明"
```

## Final Review Gate

After all tasks are green:

1. dispatch a spec-compliance reviewer against
   `docs/superpowers/specs/2026-07-19-claude-parity-design.md`;
2. fix and re-review every missing or extra behavior;
3. dispatch a code-quality/security reviewer over the full branch diff;
4. fix and re-review all critical and important findings;
5. rerun `python -m pytest`, `git diff --check`, provider-boundary search, and
   `claude --version`;
6. preserve the branch and worktree; do not push, open, or merge a PR without
   explicit permission.

## Completion Evidence

- Native launch and wrapper isolation: `tests/test_cli_claude.py` covers native
  argument passthrough and `--no-lark` before or after provider arguments.
- Hook notification and installer: `tests/test_claude_hooks.py` and
  `tests/test_claude_hook_installer.py` cover bounded safe payloads, async
  delivery, sanitized spool fallback, ownership, atomicity, and uninstall.
- Managed CRUD and resume: `tests/test_agent_api.py`, `tests/test_cli_claude.py`,
  and `tests/test_claude_sessions.py` cover authenticated create/list/show/
  cancel, provider resume IDs, and prompt exclusion from API responses.
- Approval and user input: `tests/test_claude_sessions.py`,
  `tests/test_lark_control.py`, and `tests/test_session_concurrency.py` cover
  allow/deny, ephemeral answers, timeout/shutdown denial, and first-winner
  resolution.
- Shared persistence and outbox: `tests/test_agent_storage.py` covers canonical
  Agent tables, legacy Codex migration, provider isolation, dedupe, audit, and
  privacy-preserving outbox rows.
- Provider-neutral Lark routing: `tests/test_lark_control.py` and
  `tests/test_lark_render.py` cover stored-session dispatch and provider-aware
  actionable messages.
- Runtime and health isolation: `tests/test_agent_api.py` and
  `tests/test_daemon_core.py` cover provider routes, strict Hook ingress,
  shared worker behavior, resource ownership, and independent degradation.
- Final verification: full `python -m pytest -q` passed with one environment
  skip; `git diff --check` passed; provider-boundary search returned zero
  imports; `claude --version` returned `2.1.214 (Claude Code)` without a model
  request; `git ls-files --eol` reported zero `w/crlf` or `w/mixed` files.
