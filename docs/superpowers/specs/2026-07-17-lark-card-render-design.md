# Lark Interactive Card Render Design

> **Status: Done** (2026-07-18)  
> Merged: PR #8 → `dev` (`feat/20260717-lark-card-render`, merge `906cbb4`)

## Goal

Replace plain `msg_type: text` outbound notifications with Feishu/Lark **interactive cards (schema 2.0)** so mobile clients render markdown and colored status headers. Keep `text` as a config fallback. Do not change HITL correlation.

## Non-goals

- Card action buttons for approve/deny
- Multi-bot transport or Claude adapter work
- Automatic card→text retry on API failure
- Outbox / interaction schema changes
- Changing inbound message handling (user replies remain `text`)

## Constraints

- Redact before render (`redact_text`)
- Preserve returned `message_id` for `attach_lark_message_id` and reaction/reply routing
- Cover both CLI `NotificationRequest` path and daemon outbox path
- Default format is `card`; `LARK_BOT_MESSAGE_FORMAT=text` forces plain text
- No real network in unit tests

## Architecture

```text
Event / Outbox item
  → render_* (redact + structure)
  → RenderedMessage(msg_type, content)
  → LarkBotClient.send_rendered
  → message_id
```

### Modules

| Module | Role |
|--------|------|
| `lark/messages.py` | Pure payload builders: `RenderedMessage`, `build_text_message`, `build_interactive_message` |
| `lark/render.py` | `render_task_notification`, `render_outbox_notification` → `RenderedMessage` |
| `lark/client.py` | HTTP transport; `send_rendered`; `send` / `send_text` wrappers |
| `server/daemon/app.py` | Outbox worker uses `send_rendered` |
| `config.py` | `message_format: card \| text` |

### Card envelope

```json
{
  "receive_id": "oc_xxx",
  "msg_type": "interactive",
  "content": "<stringified card json>"
}
```

### Card body (schema 2.0)

```json
{
  "schema": "2.0",
  "config": { "wide_screen_mode": true },
  "header": {
    "title": { "tag": "plain_text", "content": "..." },
    "template": "orange"
  },
  "body": {
    "elements": [
      { "tag": "markdown", "content": "..." }
    ]
  }
}
```

### Header template mapping

| Scenario | template |
|----------|----------|
| succeeded / completed | `green` |
| failed / interrupted / degraded | `red` |
| waiting / approval / input | `orange` |
| info / started / hook | `blue` |

### HITL

Unchanged: reactions and reply-to key off `lark_message_id`. Card buttons are deferred.

## Acceptance

1. CLI `run` / `codex-event` / `send-test` send interactive by default. — **met**
2. Daemon outbox interaction/terminal notifications send interactive by default. — **met**
3. `LARK_BOT_MESSAGE_FORMAT=text` restores previous plain-text payloads. — **met**
4. Secrets in tails/summaries still become `[REDACTED]`. — **met**
5. Outbox still attaches `message_id` for pending interactions. — **met**
6. Unit tests pass without network. — **met**

## Landed modules

| Path | Role |
|------|------|
| `src/lark_bot/lark/messages.py` | `RenderedMessage`, `build_text_message`, `build_interactive_message` |
| `src/lark_bot/lark/render.py` | `render_task_notification` / `render_outbox_notification` |
| `src/lark_bot/lark/client.py` | `send_rendered` |
| `src/lark_bot/config.py` | `message_format: card \| text` (`LARK_BOT_MESSAGE_FORMAT`) |
| `src/lark_bot/server/daemon/app.py` | Outbox uses `send_rendered` |
| `tests/test_lark_render.py` and related | Render / payload / config / daemon coverage |
