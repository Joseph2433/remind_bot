# Claude Code Capability Parity Design

## Goal

Make Claude Code a first-class provider with the same user-facing capability
classes as Codex while preserving each provider's native protocol. The result
must support native terminal launch, structured event notifications, auditable
project Hook installation, durable managed jobs, cancellation, session
resumption, Lark approval and user-input handling, and provider-aware daemon
health and storage.

Capability parity does not mean copying the Codex app-server implementation.
Codex continues to use its JSON-RPC app-server and loopback gateway. Claude
uses Claude Code Hooks for ordinary terminal sessions and the official Python
Agent SDK for managed sessions and bidirectional permission handling.

## Verified Provider Surfaces

The design targets the locally installed Claude Code `2.1.214` and the official
Claude Code Hook and Python Agent SDK documentation current on 2026-07-19.

Claude Code provides:

- command Hooks configured in `.claude/settings.json`, including asynchronous
  handlers that do not block the terminal session;
- lifecycle events `SessionStart`, `Notification`, `PermissionRequest`, `Stop`,
  `StopFailure`, and `SessionEnd` with a stable `session_id`;
- `ClaudeSDKClient` continuous conversations, resume, explicit session IDs,
  streaming messages, interrupt, and result messages;
- `can_use_tool`, which replaces the interactive permission prompt for managed
  SDK sessions and returns an explicit allow or deny result;
- local session discovery through `list_sessions()` and `get_session_info()`.

The SDK is the supported managed control surface. The stream-json CLI protocol
is not reimplemented directly because doing so would duplicate SDK behavior and
couple this project to an internal wire format.

## Parity Matrix

| Capability | Codex implementation | Claude implementation |
| --- | --- | --- |
| Native terminal launch | Codex TUI launcher and gateway | Native `claude` launcher with argument passthrough |
| One-way terminal notifications | Codex notify callback | Async Claude command Hooks |
| Hook management | Codex TOML fragment | Atomic merge of owned entries in `.claude/settings.json` |
| Structured event CLI | `codex-event` | `claude-event` with real Hook schemas |
| Managed job start/list/show/cancel | Codex app-server orchestrator | Claude Agent SDK session manager |
| Resume | Codex thread ID | SDK `resume` session ID |
| Approval and user input | Codex gateway server requests | SDK `can_use_tool`, including `AskUserQuestion` |
| Durable session/interaction state | Codex SQLite tables | Shared provider-neutral Agent tables |
| Durable notification delivery | Codex outbox | Shared provider-neutral outbox |
| Lark replies/reactions | Codex orchestrator routing | Provider-neutral router dispatch to Claude manager |
| Health/degraded state | Codex app-server status | Provider-specific readiness in shared daemon health |

Native Claude TUI Hooks remain notification-only. Bidirectional Lark approval
is available for daemon-managed jobs, where the SDK exposes a supported
permission callback. This matches the capability set without pretending that a
Claude terminal process exposes Codex's remote app-server gateway.

## Architecture

```text
ordinary Claude terminal
  -> async project Hooks
  -> lark-bot claude-hook
  -> sanitize and bound payload
  -> authenticated daemon Hook endpoint
  -> shared durable outbox
  -> Lark

managed Claude job
  -> provider-neutral daemon session API
  -> ClaudeSessionManager
  -> official Claude Agent SDK
  -> stream/result callbacks
  -> shared Agent session and interaction store
  -> shared durable outbox
  -> Lark
  -> reply/reaction router
  -> pending SDK can_use_tool callback
```

Shared modules must not import `modules.codex` or `modules.claude`. Provider
modules may depend on shared Agent, notification, task, and Lark contracts.

## Shared Agent Runtime

The current `AgentAdapter` contract is extended only where both providers need
the same operation: list/get sessions, resume-aware creation, cancel, and
interaction resolution. Provider-specific options remain dictionaries owned by
the provider adapter.

`modules/agent` owns:

- provider-neutral session, status, interaction, and outbox models;
- SQLite persistence for agent sessions, interactions, event dedupe, outbox,
  and audit records;
- session-level serialization and first-response-wins interaction resolution;
- provider dispatch through `AgentRegistry`;
- provider-neutral daemon request/response models and service methods.

Existing Codex data remains readable. The migration adds new Agent tables and
copies existing Codex session, interaction, outbox, dedupe, and audit rows in a
transaction. Compatibility methods in `SQLiteCodexStore` delegate to the shared
store so existing Codex behavior and tests remain stable during the migration.
No provider imports are introduced in the shared store.

## Claude Hook Integration

### Supported events

- `SessionStart`: create or refresh session identity and emit a started/resumed
  notification.
- `Notification`: map `permission_prompt`, `idle_prompt`,
  `agent_needs_input`, and `agent_completed` to explicit notification states.
- `PermissionRequest`: emit an action-required notification for ordinary TUI
  sessions. It is observational because asynchronous Hooks cannot decide.
- `Stop`: mark the turn complete, but do not invent a process success result.
- `StopFailure`: map official error values to failure with a safe summary.
- `SessionEnd`: mark the session ended with the official reason.

`UserPromptSubmit` is not treated as waiting for input. It occurs after a user
has submitted a prompt and therefore cannot represent an unanswered request.

### Safe callback payload

The callback accepts at most 64 KiB and ignores non-object JSON. It keeps only:

- `hook_event_name`;
- `session_id` and optional `prompt_id`;
- bounded `source`, `reason`, `notification_type`, `title`, and `error` values;
- a generated event identity derived from safe fields when Claude does not
  provide one.

It never spools `transcript_path`, `cwd`, `tool_input`, `permission_suggestions`,
`last_assistant_message`, raw prompt text, or full assistant output. Notification
text is separately redacted and bounded before entering the outbox.

### Installer

`lark-bot claude hooks install --project PATH` atomically merges owned command
handlers into `.claude/settings.json`. It preserves unrelated settings and Hook
handlers, refuses to replace a symlink, refuses malformed JSON, and writes only
after validation. Installed notification handlers use `"async": true` so a
daemon or network failure never blocks Claude Code.

`check` distinguishes `installed`, `missing`, `modified`, and `malformed`.
`uninstall` removes only exact owned handlers and removes empty owned containers
without deleting unrelated settings. Repeated install and uninstall operations
are idempotent.

## Managed Claude Sessions

`ClaudeSessionManager` wraps an injected SDK client factory. Production uses
`claude_agent_sdk.ClaudeSDKClient`; tests use protocol-conforming fakes and make
no model or network calls.

For each session the manager:

1. creates a durable `STARTING` record before launching the SDK client;
2. supplies name, working directory, model, permission mode, and optional
   resume ID through `ClaudeAgentOptions`;
3. updates the durable record to `RUNNING` after connection;
4. consumes SDK messages until `ResultMessage`;
5. maps `subtype`, `is_error`, duration, and result text into shared statuses
   and redacted summaries;
6. calls `interrupt()` for cancellation and drains the terminal result;
7. closes clients during daemon shutdown and reconciles interrupted sessions
   on the next startup.

Session prompts are passed directly to the SDK and are never persisted. Stored
session data contains identity, provider, name, working directory, selected
model, status, redacted summary, and timestamps only.

## Approval And User Input

Managed sessions set `can_use_tool` without auto-allowing gated tools. When the
SDK requests permission, the callback creates a durable interaction and waits
for the shared interaction service.

- tool permissions accept allow or deny;
- `AskUserQuestion` accepts the exact Lark reply as user input;
- Lark yes/no replies and positive/negative reactions retain existing behavior;
- the first valid terminal or Lark response wins;
- late replies are ignored and audited;
- timeout, daemon shutdown, missing session, or delivery failure resolves to
  deny, never allow;
- no tool input is stored or rendered; the interaction contains a bounded tool
  name and a safe provider-generated description only.

The timeout remains configurable through the existing interaction timeout
setting. Claude and Codex use the same shared expiry worker.

## CLI And Daemon API

The CLI adds:

- `lark-bot claude [--no-lark] [native arguments...]`;
- `lark-bot claude job start/list/show/cancel`;
- `lark-bot claude hooks install/check/uninstall`;
- hidden callback command `lark-bot claude-hook`.

`--no-lark` launches Claude directly without managed callback configuration.
Native `--resume`, `--continue`, model, permission-mode, and other Claude
arguments pass through unchanged.

The daemon adds provider-neutral endpoints under
`/api/v1/agents/{agent}/sessions` and `/api/v1/agents/{agent}/hooks`. Existing
`/api/v1/codex/*` endpoints remain compatibility aliases. Authentication,
payload bounds, retry, and safe error behavior are unchanged.

Health output reports Codex, Claude, and Lark independently. A failed Claude SDK
startup degrades Claude managed jobs without stopping Codex or ordinary Hook
delivery.

## Dependency And Failure Behavior

`claude-agent-sdk` is a regular project dependency because the installed Claude
managed-job commands are part of the supported product, not an optional hidden
feature. Startup imports are lazy enough that `lark-bot config`, generic command
wrapping, and Codex-only use can report a clear configuration error instead of
failing module import when an environment is incompletely installed.

The SDK still requires a compatible local Claude Code executable and valid
Claude authentication. `config --json` gains non-secret checks for the CLI and
SDK. Tests never require authentication.

## Testing Strategy

Tests use red-green-refactor and cover:

- official sanitized fixtures for every supported Hook event;
- repeated permission events producing distinct dedupe identities;
- correct `Stop`, `StopFailure`, and `UserPromptSubmit` semantics;
- installer preservation, idempotence, malformed JSON, symlink refusal, and
  exact uninstall;
- callback byte bounds, short daemon timeout, offline spool, replay, dedupe,
  and proof that sensitive Hook fields never reach disk;
- SDK session create, resume, result mapping, cancellation, shutdown, and
  startup reconciliation through fakes;
- Lark approval, denial, input, timeout, race, late-response, and fail-closed
  behavior for Claude sessions;
- provider-neutral API authentication and compatibility Codex routes;
- native launcher argument passthrough and `--no-lark` behavior;
- migration of existing Codex rows without changing Codex behavior;
- full package boundary checks, full pytest, and `git diff --check`.

A live model request is not part of automated verification because it would
consume credentials and money. The final report includes a no-cost CLI/version
check and a documented optional manual smoke command.

## Commit Boundaries

Implementation is split into independently verified local commits:

1. design and implementation plan;
2. real Hook models and notification mapping;
3. Hook installer and nonblocking callback delivery;
4. shared durable Agent storage and migration;
5. managed SDK session lifecycle;
6. shared daemon API and CLI job commands;
7. Lark approval and user-input resolution;
8. native launcher, configuration diagnostics, and documentation;
9. final regression fixes discovered by review.

Commit messages use short Chinese descriptions and do not name coworker agents.

## Acceptance Criteria

The work is complete only when:

- all parity-matrix rows have a tested Claude implementation or an explicitly
  documented provider-native equivalent;
- native Claude sessions receive nonblocking Lark notifications;
- managed Claude sessions support start, list, show, resume, cancel, terminal
  result reporting, approval, and user input through Lark;
- Hook installation is reversible and preserves user configuration;
- no raw prompt, transcript, tool input, or full output is persisted or spooled;
- existing Codex commands, APIs, storage data, and interaction behavior remain
  compatible;
- shared modules contain no provider implementation imports;
- all targeted and full tests pass and `git diff --check` is clean.
