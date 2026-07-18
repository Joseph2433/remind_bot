# Shared Agent Adapter Design

## Goal

Extract the provider-neutral path used after Codex or Claude has interpreted its
own raw event. Provider modules keep raw protocol knowledge; shared modules own
notification construction, event JSON validation, and reliable delivery of
already-sanitized Hook payloads.

## Boundary

```text
Codex payload  -> Codex status/event mapping  --+
                                               +-> normalized notification input
Claude payload -> Claude status/event mapping -+       -> task detection
                                                       -> notification request
                                                       -> Lark delivery
```

The shared layer must never import `modules.codex` or `modules.claude`.
Provider modules may import the shared `agent`, `task`, and `notification`
modules.

## Shared Components

### Normalized notification input

`modules/notification/notification_model.py` defines
`AgentNotificationInput`. It contains only provider-neutral values: agent,
task/session identity, normalized `TaskStatus`, command and output tails,
optional event name, exit code, duration, summary, and tags.

`modules/notification/notification_builder.py` converts this input into a
`NotificationRequest`. It owns these common rules:

- failed status defaults to exit code 1; other statuses default to 0;
- a non-zero exit code changes an explicit success to failure;
- summary is used only when no output tail is available;
- output-based waiting detection may elevate a terminal status;
- provider, event, custom, and detected tags are deduplicated, while an
  explicitly normalized waiting tag takes precedence over phrase tags;
- notification context is present only when a session ID is available.

Codex and Claude continue to own supported raw event names and status aliases.

### Event JSON validation

`modules/agent/agent_event.py` exposes a generic Pydantic event parser. It
requires a JSON object, reports provider-specific validation errors, and does
not know any provider event schema.

### Hook transport primitives

`modules/agent/agent_hook.py` owns bounded JSON-object parsing, safe stdin
selection, and delivery-or-spool behavior for sanitized dictionaries. It does
not decide which fields are safe. Codex and Claude must sanitize their raw
payloads before calling the delivery function.

## Provider-Owned Components

Codex retains notify payload aliases, `agent-turn-complete` normalization,
existing notify chaining, TOML configuration, app-server behavior, and TUI
integration.

Claude retains Hook event schemas, Hook event-to-status mapping, and the future
`.claude/settings.json` installer. This extraction does not claim that Claude
managed sessions or interactive approvals exist.

## Compatibility And Security

Existing CLI commands, public imports, status results, tags, dedupe keys, and
notification rendering remain unchanged. Hook spool files contain only the
sanitized mapping supplied by the provider adapter. Raw prompts and assistant
output must never be passed to the shared spool function.

## Verification

- new unit tests prove the normalized builder behavior;
- new unit tests prove bounded Hook parsing and sanitized spooling;
- existing Codex and Claude adapter tests remain unchanged and pass;
- shared modules contain no imports from provider modules;
- the full pytest suite and `git diff --check` pass.
