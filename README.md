# Lark Bot

Lark Bot is a local companion service for code agents and command-line jobs. It does not write code, make decisions, or run as an independent agent. It watches a task, summarizes the result, redacts sensitive output, and sends a Lark/Feishu mobile notification through a self-built app Bot.

The original MVP follows the same practical shape as `ntfy done`: wrap a command, wait for it to finish, then notify. The Codex automation daemon extends that foundation with managed app-server sessions and a bidirectional Lark approval/input loop.

## Features

- Run a subprocess with `lark-bot run --name "codex task" -- codex`.
- Send Codex-specific event payloads with `lark-bot codex-event`.
- Capture stdout, stderr, exit code, and runtime.
- Detect likely manual-intervention output such as approval prompts, permission prompts, and waiting-for-input messages.
- Send only a short redacted tail summary, never the full log by default.
- Send Lark/Feishu messages through app ID and app secret using tenant access tokens.
- Cache access tokens before expiry.
- Suppress duplicate notifications within a configurable cooldown using SQLite.
- Diagnose local configuration with `lark-bot config`.
- Provide FastAPI health, Lark challenge, and structured agent event endpoints.
- Launch the native interactive Codex TUI through a local authenticated companion gateway.
- Keep unattended Codex jobs available under an explicit `codex job` namespace.
- Approve or deny Codex requests with Lark reactions and answer questions by replying to the notification.
- Install auditable Codex notify fragments for one-way fallback notifications.

## Lark/Feishu App Setup

1. Create a self-built app in the Lark/Feishu developer console.
2. Enable Bot capability for the app.
3. Copy the app ID and app secret into a local `.env` file.
4. Add the Bot to the target chat if you send group messages.
5. Configure the receive ID:
   - Use `chat_id` for group/private chat targets such as `oc_xxx`.
   - Use `user_id` or `open_id` for direct user targets.
6. Grant message permissions, including the relevant `im:message` and `im:message:send_as_bot` permissions for sending as the Bot.
7. For daemon control, enable the `im.message.receive_v1` and `im.message.reaction.created_v1` events and the matching message/reaction read scopes. Group replies must mention the Bot.
8. Publish or install the app to the tenant as required by your organization.

Never commit `.env`, app secrets, tenant access tokens, webhook secrets, or copied production logs.

## Configuration

Copy `.env.example` to `.env` and replace the placeholders:

```env
LARK_BOT_LARK_APP_ID=cli_xxx
LARK_BOT_LARK_APP_SECRET=replace-with-app-secret
LARK_BOT_LARK_RECEIVE_ID_TYPE=chat_id
LARK_BOT_LARK_RECEIVE_ID=oc_xxx
LARK_BOT_SQLITE_PATH=.lark-bot/lark_bot.sqlite3
LARK_BOT_COOLDOWN_SECONDS=300
LARK_BOT_OUTPUT_TAIL_LINES=40
LARK_BOT_HTTP_TIMEOUT_SECONDS=10
LARK_BOT_LOG_LEVEL=INFO
LARK_BOT_DAEMON_HOST=127.0.0.1
LARK_BOT_DAEMON_PORT=8787
LARK_BOT_DAEMON_TOKEN_PATH=.lark-bot/daemon.token
LARK_BOT_CODEX_PATH=codex
LARK_BOT_INTERACTION_TIMEOUT_SECONDS=1800
LARK_BOT_INTERACTION_EXPIRY_POLL_SECONDS=1
LARK_BOT_OUTBOX_POLL_SECONDS=0.5
LARK_BOT_NOTIFICATION_DELAY_SECONDS=5.0
LARK_BOT_LARK_EVENT_QUEUE_CAPACITY=100
```

## Usage

Install in editable mode for local development:

```bash
python -m pip install -e ".[dev]"
```

Send a local smoke-test message:

```bash
lark-bot send-test --message "lark-bot smoke test"
```

Send a Codex event from a JSON file or stdin:

```bash
lark-bot codex-event --file codex-event.json
Get-Content codex-event.json | lark-bot codex-event
```

Example Codex event payload:

```json
{
  "task_name": "codex task",
  "status": "approval_required",
  "command": ["codex"],
  "duration_seconds": 42.5,
  "output_tail": ["Do you want to allow this command?"],
  "stderr_tail": []
}
```

Supported terminal status aliases include `completed`, `success`, `failed`, `error`, `needs_input`, `approval_required`, and `permission_required`. Intermediate values such as `running` or `in_progress` are rejected instead of being treated as failures. When `exit_code` is omitted, waiting/success default to `0` and failures default to `1`. A success alias with a non-zero `exit_code` is treated as failed.

Check local configuration without exposing secrets:

```bash
lark-bot config
lark-bot config --json
```

### Managed Codex automation

Start the local daemon. It binds to loopback by default and creates a private bearer token under `.lark-bot/`:

```bash
lark-bot daemon
```

From another real terminal, launch the native Codex TUI with Lark assistance:

```bash
lark-bot codex
lark-bot codex "Inspect this repository"
lark-bot codex --model MODEL --sandbox workspace-write
```

The terminal remains the primary Codex interface. Full streaming output, shortcuts, prompts, and conversation history are rendered by the native Codex TUI. Lark Bot runs a loopback-only structured gateway beside it; it never parses terminal escape sequences.

All Lark notifications are delayed by five seconds. For approval and input requests, the terminal and Lark use first-response-wins semantics: the first valid response is forwarded to Codex, and a late response is ignored as already resolved. React 👍 or 👎 to the exact approval message. Reply to the exact input message; in a group chat, mention the Bot. For multiple questions, reply with one `1: answer` line per question.

If the daemon is intentionally unavailable, launch Codex without any Lark callback or gateway:

```bash
lark-bot codex --no-lark
```

Start and inspect unattended background jobs separately:

```bash
lark-bot codex job start --name "implement feature" --cwd . "Implement the requested feature"
lark-bot codex job list
lark-bot codex job show SESSION_ID
lark-bot codex job cancel SESSION_ID
```

Use `-` as an unattended-job prompt to read it from stdin. Unattended jobs always use `approvalPolicy=on-request`; supported sandboxes are `read-only` and `workspace-write`.

Install one-way hooks for ordinary Codex sessions:

```bash
lark-bot codex hooks install --project .
lark-bot codex hooks check --project .
lark-bot codex hooks uninstall --project .
```

The installer writes an auditable notify fragment without replacing `config.toml`. The normal `lark-bot codex` launcher injects the equivalent callback safely and chains an existing top-level Codex `notify` command. If the daemon is unavailable, `codex-hook` stores only a small sanitized event under `.lark-bot/spool/` for later delivery.

Wrap a successful command:

```bash
lark-bot run --name "success smoke" -- python -c "print('ok')"
```

Wrap a failing command:

```bash
lark-bot run --name "failure smoke" -- python -c "import sys; print('bad'); sys.exit(2)"
```

Run the optional API server:

```bash
lark-bot serve --host 127.0.0.1 --port 8787
```

Health check:

```bash
curl http://127.0.0.1:8787/health
```

Send a structured task event to the local API server:

```bash
curl -X POST http://127.0.0.1:8787/agent/events \
  -H "Content-Type: application/json" \
  -d '{
    "name": "claude code task",
    "source": "claude_hook",
    "status": "failed",
    "exit_code": 2,
    "duration_seconds": 12.5,
    "stdout_tail": ["Need user input"],
    "stderr_tail": ["permission required"]
  }'
```

The server also exposes `POST /lark/events` for Lark URL verification challenge responses. Lark business event handling remains minimal in this release.

## Local Lark CLI Verification

`lark-cli` is useful for validating that your app, tenant, permissions, and receive IDs are correct. It is not a Lark Bot runtime dependency. Use it separately to prove the same Bot can send a message before debugging Lark Bot configuration.

## Security Notes

- Notification text is redacted before sending.
- Only the last configured number of stdout/stderr lines is included.
- The logger avoids printing secrets, tokens, and request headers.
- Token values are cached in memory and refreshed before expiry.
- SQLite notification history stores dedupe metadata, not full command output.
- The daemon API is loopback-only by default and requires a generated bearer token.
- Full prompts, full agent output, Lark tokens, and raw user replies are not persisted.
- Approvals apply only to the current request or turn; persistent and session-wide approval rules are not created.

## Roadmap

MVP implemented:

- CLI wrapper
- Codex event adapter and `lark-bot codex-event`
- Lark/Feishu self-built app Bot messaging
- SQLite dedupe and cooldown
- Output detection and redaction
- Safe configuration diagnostics
- FastAPI health, Lark challenge, and structured agent event endpoints
- Codex app-server harness and transactional session state machine
- Lark long-connection reaction/reply control
- Authenticated local daemon, durable outbox, timeout handling, and project Codex hooks

Reserved for later:

- Interactive cards
- Direct mobile task creation and richer task detail views
- Redis or Postgres storage backends
- Dedicated adapters for Claude Code, build systems, and test runners
