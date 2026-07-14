# Codex Remote Resume Picker Design

## Goal

Make the native Codex TUI session picker work when Codex is launched through
`lark-bot codex`, without weakening Lark approval arbitration, misrouting
JSON-RPC messages, or leaking one terminal client's responses to another.

This design supersedes the assumption in the earlier TUI companion design that
all resume behavior works transparently through the initial single-client
gateway. Direct resume by `--last` or session ID already works; the broken case
is the picker opened from a remote TUI, including the in-session `/resume`
command.

## Current Failure

`lark-bot codex` creates one daemon-managed interactive session, starts one
loopback `codex app-server`, places `CodexGateway` in front of it, and launches
the native TUI with `codex --remote <gateway-endpoint>`.

The gateway currently owns one upstream WebSocket, one outbound queue, one set
of JSON-RPC correlation maps, and one terminal WebSocket. A second terminal is
closed with WebSocket status `1008` and the reason `only one terminal client is
allowed`. Codex's resume picker opens an additional remote client, so the
gateway rejects it and Codex reports that it failed to connect to the remote
app server.

Removing the single-client check is unsafe. With the current shared queue and
global request maps, two terminals would compete for messages, reuse common
JSON-RPC IDs, and potentially answer each other's approval requests.

## Delivery Strategy

Delivery is gated by a real protocol probe against the supported Codex CLI
version. The WebSocket app-server transport is experimental, and documentation
does not guarantee the exact multi-client ownership behavior needed by the
picker.

1. Start one real loopback `codex app-server`.
2. Establish two authenticated or loopback WebSocket clients.
3. Initialize both clients independently.
4. Keep the primary connection open while the second performs `thread/list`.
5. Resume a selected thread and record which connection owns subsequent TUI
   activity and notifications.
6. Close the picker connection and verify the primary connection remains
   usable.

If the installed Codex version cannot support this workflow, Lark Bot will keep
the single-client gateway and deliver the explicit degradation described below
instead of implementing a protocol multiplexer.

## Considered Approaches

### Per-client upstream connection

Each downstream terminal WebSocket receives its own upstream WebSocket to the
same daemon-owned app-server process. Per-client queues and JSON-RPC state are
isolated. This is the preferred complete solution because the app-server sees
real independent clients and no wire-level request-ID rewriting is required.

### Shared upstream multiplexer

Multiple terminals share one upstream connection while the gateway rewrites
request IDs and attempts to route notifications. This is rejected. App-server
initialization is connection-scoped, server requests contain both top-level and
nested correlation IDs, and notifications without request IDs cannot be routed
reliably before a thread owner is known.

### Explicit picker degradation

Keep one terminal client and reject picker-dependent invocations with an
actionable message. Users can resume with `--last` or an explicit session ID,
or use direct Codex/`--no-lark` for an interactive picker. This is the fallback
when the real protocol probe does not validate the per-client topology.

## Target Architecture

`CodexGateway` becomes a listener and connection-lifecycle owner. Each accepted
terminal creates one private connection pair:

```text
terminal websocket <-> GatewayConnection <-> app-server websocket
```

`GatewayConnection` owns:

- a stable random client ID;
- one downstream terminal WebSocket;
- one upstream app-server WebSocket;
- one bounded downstream queue;
- one upstream reader task and one downstream sender task;
- intercepted, responded, and terminal-request correlation maps;
- active thread/turn state for that connection;
- callback tasks created for Lark-side arbitration.

`CodexGateway` owns:

- loopback listener and bearer authentication;
- the set of live connection pairs;
- admission and shutdown locking;
- upstream connection configuration;
- closing every pair when the daemon interactive session is deleted.

One picker disconnect closes only its connection pair. It does not close the
gateway, app-server process, or logical interactive session. The CLI's existing
`DELETE /interactive-sessions/{id}` remains the authoritative whole-session
cleanup operation.

## Interaction Correlation

JSON-RPC IDs are scoped to one app-server connection and can collide across
clients. Wire messages retain their original ID, but the ID exposed to the
orchestrator and persisted interaction store becomes an opaque connection-
scoped value:

```text
<client-id>:<canonical-json-rpc-id>
```

Each connection pair maintains the mapping from the scoped ID to the raw wire
ID. Its responder closure converts the scoped ID back to the raw ID before
sending a result or error upstream.

The same scoped ID is used when:

- creating a pending Lark interaction;
- resolving an interaction from the terminal;
- processing `serverRequest/resolved`;
- cancelling a losing callback after first-response-wins arbitration.

This prevents two clients using `1` or `"rpc-1"` from sharing a database row or
live interaction. No SQLite schema change is expected because request IDs are
already stored as text, but all lookup and uniqueness tests must cover scoped
IDs.

## Thread Ownership and Rebinding

The daemon continues to expose one logical Lark interactive session, not an
arbitrary multi-TUI collaboration service. Additional connections exist to
support Codex-owned picker and handoff behavior.

The real protocol trace determines which connection issues the successful
`thread/resume`. The following invariants apply regardless of that detail:

- `thread/list` and `thread/read` never bind or rebind a Lark session.
- Failed `thread/start` or `thread/resume` responses never change binding.
- Only the connection that receives a successful start/resume response can
  become the owner of that thread.
- Rebinding is rejected while the old thread has an active turn or pending
  interaction.
- A target thread already owned by another live interactive session cannot be
  silently stolen.
- Successful rebinding clears the previous `turn_id`; the next matching
  `turn/started` establishes the active turn.
- Events from a stale connection cannot overwrite the current binding or
  resolve the current owner's interactions.

If the trace shows that the picker is only an ephemeral listing client and the
primary connection performs the final `thread/resume`, no new rebinding API is
needed. If the picker connection becomes the new owner, the orchestrator gains
an explicit guarded rebind operation implementing the invariants above.

## Failure Handling

- Failure to create a private upstream closes only the new terminal with a
  server-error status and leaves existing clients active.
- An upstream protocol or transport failure closes its paired terminal and
  removes the pair from the gateway registry.
- Closing the gateway first stops accepting clients, then closes and awaits all
  connection tasks.
- A connection closing with pending interactions cancels or interrupts only
  interactions sourced from that connection.
- Authentication remains constant-time bearer comparison on a loopback-only
  listener.
- Tokens, full prompts, and raw terminal output are not logged.

## Explicit Degradation

If the real probe fails, Lark Bot will document and enforce these supported
paths:

```powershell
lark-bot codex resume --last
lark-bot codex resume <SESSION_ID>
lark-bot codex --no-lark resume
```

`lark-bot codex resume` without a session ID or `--last` will fail before
creating a daemon interactive session and explain that the picker requires
`--no-lark`. Lark Bot will not silently add `--last`, because doing so could
resume the wrong conversation. An in-TUI `/resume` cannot be preflighted, so
documentation and the gateway close reason will describe the limitation.

## Verification

The implementation must add:

- a real, version-gated app-server multi-client probe;
- two-downstream/two-upstream gateway tests proving messages never cross;
- same-RPC-ID tests proving connection-scoped interaction isolation;
- terminal-wins and Lark-wins arbitration tests for each source connection;
- picker disconnect and whole-session shutdown lifecycle tests;
- successful, failed, stale, busy, and already-owned thread-binding tests when
  guarded rebinding is required;
- CLI degradation tests for picker, `--last`, explicit ID, and `--no-lark`;
- full regression through `python -m pytest`.

## Non-goals

- Supporting multiple independently active native TUIs as one Lark session.
- Broadcasting app-server events to every terminal.
- Parsing terminal output or simulating keyboard input.
- Replacing Codex's picker UI.
- Exposing the gateway outside loopback.
- Claiming compatibility with untested Codex app-server versions.
