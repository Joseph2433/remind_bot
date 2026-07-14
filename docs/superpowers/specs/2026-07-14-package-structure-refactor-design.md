# Package Structure Refactor Design

## Goal

Reorganize `src/lark_bot` around functional boundaries and split the largest modules without changing observable behavior. The refactor must make ownership obvious from the directory tree, reduce the amount of implementation code at the package root, and leave major units independently understandable and testable.

The work starts from `dev` on `refactor/package-structure`.

## Scope

This refactor includes:

- moving root-level modules into functional packages;
- splitting `cli.py`, `codex_orchestrator.py`, `storage/codex_sqlite.py`, `codex_app_server.py`, and `lark_control.py` by responsibility;
- updating imports, tests, documentation, and executable entry points;
- adding structural and compatibility tests;
- preserving runtime behavior, database schema, command syntax, environment variables, and network protocols.

This refactor does not include:

- new Lark or Codex features;
- changes to approval, resume, notification, retry, or timeout semantics;
- database migrations or schema redesign;
- replacement of Typer, FastAPI, SQLite, websockets, or the Lark SDK;
- a strict domain-driven or clean-architecture rewrite.

## Approaches Considered

### 1. Shallow move with legacy facades

Move implementation into packages but retain a facade at every old module path. This minimizes import breakage, but it leaves the package root visually crowded and creates two names for most modules. Mutable module globals and monkeypatch targets also make some facades unreliable.

### 2. Functional packages with focused compatibility

Organize code by product function, split large files, preserve stable executable entry points, and update internal/private imports to canonical paths. Keep compatibility only where an entry point is part of normal runtime behavior. This delivers the requested structure without turning the change into an architecture rewrite.

This is the selected approach.

### 3. Strict layered architecture

Separate domain, application, infrastructure, and interface layers across the entire project. This offers the strongest dependency rules, but it would require redesigning existing service contracts and persistence interfaces. The behavioral risk and scope are too high for a structure-focused refactor.

## Target Structure

```text
src/lark_bot/
├── __init__.py
├── __main__.py
├── cli.py                       # stable console entry point and thin app assembly
├── config.py                    # shared settings contract
├── models.py                    # shared notification/task contracts
├── redaction.py                 # shared security boundary
├── commands/
│   ├── common.py                # logging, output, validation helpers
│   ├── tasks.py                 # run, send-test, config, codex-event, serve
│   ├── codex.py                 # TUI and background-job commands
│   ├── daemon.py                # daemon command and local daemon HTTP client
│   └── hooks.py                 # hook install/check/uninstall/callback commands
├── tasks/
│   ├── detector.py
│   └── runner.py
├── notifications/
│   ├── base.py
│   └── adapters/
│       └── codex.py
├── lark/
│   ├── client.py                # OpenAPI HTTP client and token cache
│   ├── events.py                # reaction/message normalization models
│   ├── router.py                # approval and user-input reply routing
│   └── connection.py            # multiprocessing long connection
├── codex/
│   ├── models.py
│   ├── gateway.py
│   ├── interactive.py
│   ├── tui.py
│   ├── hooks.py                 # notify-fragment management
│   ├── hook_adapter.py          # callback parsing, forwarding, and spool fallback
│   ├── probe.py                 # remote client diagnostics
│   ├── app_server/
│   │   ├── client.py            # subprocess and JSON-RPC lifecycle
│   │   ├── messages.py          # request/notification value objects
│   │   └── responses.py         # approval/input response payload builders
│   └── orchestration/
│       ├── service.py           # CodexOrchestrator public service
│       ├── events.py            # orchestrator event contracts
│       ├── interactions.py      # approval/input handling helpers
│       └── summaries.py         # safe request/turn summary helpers
├── storage/
│   ├── base.py
│   ├── sqlite.py                # notification cooldown/dedupe store
│   ├── redis.py
│   └── codex/
│       ├── store.py             # SQLiteCodexStore public facade and transactions
│       ├── schema.py            # DDL and schema initialization
│       ├── sessions.py          # session persistence operations
│       ├── interactions.py      # interaction persistence operations
│       ├── outbox.py            # notification outbox operations
│       ├── audit.py             # audit persistence operations
│       └── mappers.py           # row-to-model and serialization helpers
└── server/
    ├── app.py                   # stable public FastAPI target
    ├── agent_events.py
    ├── lark_events.py
    └── daemon/
        ├── app.py               # authenticated daemon endpoints
        ├── runtime.py           # workers and dependency composition
        └── auth.py              # token creation and request authentication
```

`config.py`, `models.py`, and `redaction.py` remain at the package root because they are small, shared contracts used across several functional domains. Moving them into a generic `core` package would make the tree look tidier but would make ownership less precise.

## Component Boundaries

### CLI and commands

`lark_bot.cli:app` remains the console-script target, and `lark_bot.__main__` continues to invoke it. `cli.py` owns only Typer application construction, fallback group behavior, and command registration. Command implementations live in `commands/` and expose explicit registration functions.

The command modules call application services through imports rather than hiding global service locators. Tests patch dependencies at the canonical command module path. Private helpers currently imported from `lark_bot.cli` may be re-exported temporarily only when doing so is inexpensive; they are not treated as a supported public API.

### Codex subsystem

The `codex` package owns all Codex-specific transport, runtime, domain, orchestration, hook, TUI, and diagnostic code.

The app-server split separates wire value objects and response construction from subprocess lifecycle management. The gateway depends on app-server message contracts, while interactive session management composes the app-server client and gateway.

The orchestrator retains a single public `CodexOrchestrator` service. Event contracts, pure interaction-response helpers, and summary formatting move into focused modules. Stateful session coordination remains in the service so the refactor does not introduce a new distributed state model.

### Lark subsystem

The Lark HTTP client, inbound event normalization, reply routing, and long-lived SDK connection become separate modules. The multiprocessing worker remains a module-level importable function in `lark.connection`, and process construction uses that canonical target so Windows spawn behavior remains valid.

### Codex persistence

`SQLiteCodexStore` remains the single public storage abstraction and retains its current method signatures. The implementation is split into focused operation modules grouped by aggregate. `store.py` owns connection creation, locking, transactions, and delegation. Schema creation and row mapping are independent modules.

The split may use private repository helper classes or narrowly scoped mixins when necessary to preserve transaction and lock semantics. These helpers are implementation details and must not expose new public storage APIs. The existing tables, columns, indexes, ordering, idempotency, and redaction behavior remain unchanged.

### Servers and daemon

The existing public callback server stays at `lark_bot.server.app:app`. Daemon-specific FastAPI endpoints, authentication, and runtime workers move under `server/daemon/`. This keeps web-facing code together while distinguishing the small public event receiver from the managed Codex daemon.

## Dependency Direction

The intended dependency direction is:

```text
cli/commands and server adapters
        ↓
codex orchestration and task services
        ↓
codex transport, lark integration, notifications, storage
        ↓
shared models, config, and redaction
```

Imports must not point from shared modules back into CLI, server, or orchestration code. Storage code may depend on domain models but domain models must not depend on storage. Transport modules must not import command modules.

## Compatibility Policy

The following runtime contracts remain stable:

- the `lark-bot` console command;
- `python -m lark_bot`;
- the `lark_bot.cli:app` console-script target;
- the `lark_bot.server.app:app` Uvicorn target;
- all documented CLI command names, arguments, options, output shapes, and exit behavior;
- environment variable names and settings defaults;
- SQLite database schema and persisted data compatibility;
- Codex app-server and gateway message behavior;
- Lark notification and reply behavior.

Old internal module paths such as `lark_bot.codex_orchestrator` and `lark_bot.storage.codex_sqlite` are not preserved merely because tests imported them. Tests and internal imports move to canonical functional paths. A small compatibility facade is allowed only for a documented executable module or a low-cost public symbol, and every retained facade must have a removal rationale.

The remote probe's documented invocation is updated to its canonical module path. If the old `python -m lark_bot.codex_remote_probe` invocation must remain during the transition, its facade must explicitly call `main()` when executed.

## Migration Strategy

The refactor proceeds in behavior-preserving slices:

1. Add structural tests and establish stable entry-point checks.
2. Move leaf task, notification, and Codex model modules.
3. Split Codex app-server messages/responses from its client.
4. Move gateway, interactive runtime, TUI, hooks, callback adapter, and probe.
5. Split Lark inbound events, routing, and long connection.
6. Split Codex persistence while preserving the public store API and schema.
7. Split the orchestrator around events, interactions, summaries, and the stateful service.
8. Split daemon endpoints, authentication, and runtime composition.
9. Split CLI commands last, then reduce `cli.py` to the stable composition root.
10. Update documentation, remove obsolete modules, build the wheel, and run full verification.

Each slice receives its own focused commit after its targeted tests pass. The working tree's unrelated `AGENTS.md` modification and untracked `CLAUDE.md` are never staged.

## Error Handling and Security

The refactor preserves existing exception types and boundary behavior wherever callers rely on them. Extracted modules must not broaden exception catching or log additional payload data.

Security-sensitive rules remain explicit:

- secrets and tokens are never logged or added to exceptions;
- notification and audit text continues through redaction;
- daemon authentication remains loopback-oriented and bearer-token protected;
- hook input remains bounded before JSON parsing;
- WebSocket authentication and close codes remain unchanged;
- SQLite writes preserve locking and transactional behavior.

## Testing and Verification

Required verification includes:

- targeted tests for each moved or split subsystem;
- import tests for the new canonical package structure;
- CLI help and representative command tests through `lark_bot.cli:app`;
- `PYTHONPATH=src python -m lark_bot --help`;
- direct resolution of `lark_bot.server.app:app`;
- storage tests against an existing database initialized by the pre-refactor schema;
- Windows-safe multiprocessing tests for the Lark connection target where practical;
- the full `python -m pytest` suite;
- `git diff --check`;
- wheel build and import/entry-point smoke tests.

Existing behavior tests remain authoritative. Tests that patch private module globals are updated to patch the canonical owning module rather than forcing obsolete module layout.

## Acceptance Criteria

The refactor is complete when:

- package-root implementation files are reduced to stable entry points and genuinely shared contracts;
- every moved module has a clear functional owner;
- `cli.py`, the orchestrator service, and the Codex SQLite store are materially smaller and delegate focused responsibilities;
- no obsolete duplicate implementation modules remain;
- runtime entry points and documented CLI behavior are unchanged;
- existing databases open without migration;
- all targeted tests, the full test suite, entry-point smokes, and build verification pass;
- no secrets, generated databases, caches, or unrelated user files are committed.
