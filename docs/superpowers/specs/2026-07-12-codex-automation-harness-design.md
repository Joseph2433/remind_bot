# Codex Automation Harness Design

## Goal

Build a local Codex automation control plane that provides a bidirectional human-in-the-loop workflow through Lark/Feishu while preserving one-way notifications for ordinary Codex sessions.

## Architecture

- `lark-bot daemon` owns the local SQLite state, a Codex app-server child process, and the Lark event stream.
- Managed sessions are created through the daemon and use app-server JSON-RPC for structured approval, user-input, cancellation, and completion events.
- Ordinary Codex sessions use project hooks for `SessionStart`, `PermissionRequest`, and `Stop`; hook delivery is notification-only.
- Lark approval uses reactions on the exact notification message: thumbs-up approves and thumbs-down denies. Text input must reply to the exact notification and mention the Bot in group chats.
- The daemon binds to loopback and requires a generated local bearer token.

## Safety Boundaries

- Managed sessions always use `approvalPolicy=on-request`.
- Supported sandboxes are `read-only` and `workspace-write`; `danger-full-access` is excluded.
- Approval applies only to the current request or turn. No persistent policy or session-wide approval is created.
- The first valid response wins. Duplicate, expired, or unrelated Lark events cannot drive Codex.
- Full prompts, full logs, access tokens, and secrets are not persisted. Stored summaries pass through redaction and length limits.

## Failure Semantics

- Pending interactions expire after 30 minutes by default. Approval timeout denies the request; user-input timeout interrupts the turn.
- If the daemon restarts, previously active sessions become `interrupted`; v1 does not reconnect to old processes.
- If app-server exits, associated sessions become `interrupted`, and the supervisor may restart it for new work.
- Hook delivery never blocks ordinary Codex. When the daemon is unavailable, redacted events are spooled under `.lark-bot/spool/`.
- Outbound notifications use a persistent outbox with bounded retry.

## Acceptance Criteria

1. A managed command approval continues after a thumbs-up reaction.
2. A thumbs-down reaction denies the request without creating an approval rule.
3. A reply to a user-input notification is returned to the same Codex turn.
4. Simultaneous or duplicate responses resolve the request exactly once.
5. Daemon restart marks active sessions interrupted and accepts new sessions.
6. Ordinary Codex hooks deliver one-way permission and stop notifications.
7. Unit and integration tests use fake Codex and Lark transports; no real network is required.
