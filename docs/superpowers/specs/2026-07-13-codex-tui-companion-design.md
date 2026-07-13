# Codex TUI Companion Design

## Goal

Make `lark-bot codex` launch the native interactive Codex CLI while a local companion observes the same structured app-server session, delays every Lark notification by five seconds, and lets either the terminal or Lark resolve approvals and user-input requests. The first valid response wins and the losing surface is informed without executing the response twice.

## User Experience

The primary entry point is interactive:

```powershell
lark-bot codex
lark-bot codex "Inspect the repository"
lark-bot codex --model MODEL --sandbox workspace-write
lark-bot codex resume --last
```

The command keeps the native Codex TUI attached to the current console. Full prompts, streaming output, terminal history, keyboard shortcuts, and resume behavior remain Codex responsibilities rather than being reimplemented by Lark Bot.

The existing unattended session API remains available under an explicit namespace:

```powershell
lark-bot codex job start "Run the migration"
lark-bot codex job list
lark-bot codex job show SESSION_ID
lark-bot codex job cancel SESSION_ID
```

## Phased Delivery

The feature is validated in two independently useful stages.

### Phase A: native TUI with a hook/notify sidecar

`lark-bot codex` launches the installed `codex` executable with inherited console streams. A Codex-native hook or `notify` callback sends structured lifecycle payloads to `lark-bot codex-hook`; the callback returns immediately and never owns the terminal. This phase proves that the normal TUI remains intact, Windows invocation is reliable, redacted events reach the daemon, and every Lark delivery observes the five-second delay.

Codex 0.144.1 capability detection selects only a callback surface verified on the installed CLI. The installer must not claim success merely because it wrote `.codex/hooks.json`; its manifest shape and event names must match Codex. Where the native `notify` callback is the verified surface, the installer merges project `.codex/config.toml` without replacing unrelated settings.

Phase A is a notification-only fallback and does not claim that Lark can resolve approvals.

### Phase B: shared app-server arbitration

After Phase A passes a real interactive smoke, the companion attaches to a shared loopback app-server to receive approval and user-input requests and apply first-response-wins arbitration.

## Target Architecture

`lark-bot codex` starts or reuses a loopback-only Codex app-server endpoint, attaches a companion connection, and launches the native TUI with `codex --remote <endpoint>`. Codex remains the source of truth for thread state and rendering. The companion consumes structured server requests and notifications instead of parsing terminal escape sequences.

Phase B validates the installed Codex protocol with a real two-client smoke test. The preferred topology is a shared app-server daemon with the native TUI and companion as separate clients. If approval requests are routed to only one connection, Lark Bot uses a loopback JSON-RPC gateway in front of the upstream app-server so arbitration still occurs on structured envelopes. Terminal text scraping and simulated keystrokes are explicitly excluded.

Components:

- `codex_tui.py`: transparent native process lifecycle plus Phase B endpoint discovery, argument forwarding, and exit-code propagation.
- `codex_hook_adapter.py`: verified callback payload normalization and non-blocking daemon forwarding.
- `codex_companion.py`: thread observation, structured event normalization, delayed notification scheduling, and interaction arbitration.
- Existing `codex_storage.py`: durable interaction CAS, message correlation, audit events, and outbox state.
- Existing Lark long connection: reaction and reply ingestion.
- Existing daemon API: local authentication and unattended jobs; interactive companion registration is added without storing full prompts or output.

## Event and Delay Rules

Every outbound Lark notification is enqueued with `not_before = event_time + 5 seconds`.

- Completion, failure, interruption, and ordinary status notifications are sent when due.
- Approval and input notifications are sent only if the interaction is still pending when due.
- If the terminal resolves an interaction during the five-second window, the pending Lark notification is cancelled.
- If the notification was already sent, it is updated or followed by a compact resolution message identifying `terminal` or `lark` as the winner.

The delay is configurable through `LARK_BOT_NOTIFICATION_DELAY_SECONDS` and defaults to `5.0`.

## First-Response-Wins Arbitration

Each structured approval or input request creates one durable interaction row. Both response paths call the same compare-and-swap claim operation:

```text
pending -> resolved(actor=terminal|lark, decision, resolved_at)
```

Only the winner forwards a protocol response to Codex. A late terminal or Lark answer receives an already-resolved result and is never forwarded. Process-local locks may reduce races but SQLite CAS is the correctness boundary.

Terminal resolutions are detected from the shared app-server protocol stream rather than terminal output. Lark resolutions continue to use exact message correlation, reactions for approval, and replies for user input.

## Failure Handling

- If the companion cannot attach, `lark-bot codex` fails clearly before starting a misleading unmanaged session unless `--no-lark` is supplied.
- If Lark connectivity fails after the TUI starts, Codex remains usable in the terminal and delayed events remain in the durable outbox for retry.
- If the TUI exits, the companion detaches without terminating a reusable app-server daemon owned by another process.
- If the app-server exits, the TUI exit status is propagated and the session is marked interrupted.
- Loopback endpoints and bearer material are never exposed in logs; non-loopback remote endpoints are rejected in this version.

## Security and Privacy

- Full prompts, streaming output, raw replies, and complete command logs are not persisted.
- Lark receives redacted summaries and the minimum approval/input context needed to respond.
- The shared endpoint is loopback-only and uses Codex-supported authentication when required.
- Persistent or session-wide approval amendments are not created from Lark; Lark approvals apply only to the current request.

## Compatibility

The first supported runtime is the installed Codex CLI `0.144.1`. Startup performs capability detection for `--remote` and the required app-server methods. Unsupported versions fail with an actionable message instead of silently falling back to screen parsing.

## Verification

- Phase A first proves `lark-bot codex` displays the real native TUI and a real Codex lifecycle callback reaches the local spool or daemon after five seconds.
- Unit tests cover argument forwarding, five-second scheduling, cancellation, CAS arbitration, late responses, and exit-code propagation.
- Protocol integration tests use fake WebSocket/app-server peers for deterministic races.
- A real smoke test launches the installed Codex app-server and native TUI connection without submitting a model turn.
- The full pytest suite remains green.
