# Codex remote resume probe

Date: 2026-07-13
Installed Codex: `codex-cli 0.144.1`

## Result

The approved real run produced this public JSON exactly:

```json
{"both_initialized": true, "both_listed_threads": true, "codex_version": "codex-cli 0.144.1", "error_type": "TimeoutError", "multi_client": true, "primary_survived": false, "resume_attempted": true, "resume_succeeded": true}
```

One app-server accepted two initialized WebSocket clients, both listed threads, and the secondary client resumed an existing thread. After the secondary client and picker closed, the primary-client recheck timed out. The required picker handoff semantics are therefore not supported or reliable in this version.

The first sandboxed run could not start the app-server because Codex could not initialize its state under `CODEX_HOME`. That was an execution-environment permission failure, not a protocol decision.

## Decision

`IMPLEMENT_EXPLICIT_PICKER_DEGRADATION`

Do not treat the multi-client gateway as safe.

## Required implementation follow-up

Preflight `lark-bot codex resume` when invoked without an ID or `--last`. Preserve `resume --last`, explicit-ID resume, and `--no-lark resume`. Document the in-TUI `/resume` limitation.
