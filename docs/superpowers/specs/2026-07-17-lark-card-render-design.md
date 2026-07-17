# Lark Interactive Card Render Design

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

1. CLI `run` / `codex-event` / `send-test` send interactive by default.
2. Daemon outbox interaction/terminal notifications send interactive by default.
3. `LARK_BOT_MESSAGE_FORMAT=text` restores previous plain-text payloads.
4. Secrets in tails/summaries still become `[REDACTED]`.
5. Outbox still attaches `message_id` for pending interactions.
6. Unit tests pass without network.
