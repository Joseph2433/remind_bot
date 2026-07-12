# Global Codex Multi-Agent V2 Workflow Design

## Goal

Enable the experimental Codex Multi-agent V2 workflow globally and expose the
spawn metadata required to select named agent types, models, and reasoning
effort. Keep three explicit roles:

- The main thread uses `gpt-5.6-sol` with `high` reasoning effort.
- `code-worker` uses `gpt-5.6-luna` with `high` reasoning effort for scoped
  implementation work.
- `code-reviewer` uses `gpt-5.6-terra` with `xhigh` reasoning effort for
  read-only code review.

The existing Sol main-thread configuration remains unchanged. The feature
configuration restores the metadata hidden by recent Codex defaults so the
root agent can select the configured Luna and Terra roles.

## Feature Configuration

Add the following global feature table without changing unrelated provider,
authentication, notification, MCP, plugin, or project settings:

```toml
[features.multi_agent_v2]
hide_spawn_agent_metadata = false
tool_namespace = "agents"
max_concurrent_threads_per_session = 3
```

`hide_spawn_agent_metadata = false` exposes `agent_type`, `model`, and
`reasoning_effort` on the spawn tool. `tool_namespace = "agents"` keeps the
collaboration tools grouped consistently. The concurrency limit allows Sol,
Luna, and Terra to run together while matching the existing three-thread
workflow.

Multi-agent V2 is an under-development Codex feature. Configuration validation
must therefore be paired with a post-restart schema probe. If the installed
Codex build rejects the table, the private backup is restored and the failure
is reported without leaving a partial global configuration.

## Configuration Structure

Register both roles in the global `~/.codex/config.toml` under the `[agents]` namespace:

```toml
[agents]
max_threads = 3
max_depth = 1

[agents.code-worker]
description = "Implementation subagent for scoped coding and straightforward fixes."
config_file = "agents/code-worker.toml"

[agents.code-reviewer]
description = "Read-only reviewer for correctness, regressions, security, and missing tests."
config_file = "agents/code-reviewer.toml"
```

`max_threads = 3` permits the Sol main thread plus the two configured subagents to be active concurrently. `max_depth = 1` allows Sol to spawn direct children while preventing recursive delegation by those children.

The role-specific settings live in standalone files under `~/.codex/agents/`. Relative `config_file` paths resolve from the global configuration file.

## Code Worker

`~/.codex/agents/code-worker.toml` defines:

- `name = "code-worker"`
- `model = "gpt-5.6-luna"`
- `model_reasoning_effort = "high"`
- `sandbox_mode = "workspace-write"`
- A description that limits the role to bounded implementation tasks.
- Developer instructions requiring minimal scoped changes, preservation of unrelated work, relevant verification, and a structured handoff.

The worker must report ambiguity or scope expansion to Sol instead of making product or architecture decisions independently.

## Code Reviewer

`~/.codex/agents/code-reviewer.toml` defines:

- `name = "code-reviewer"`
- `model = "gpt-5.6-terra"`
- `model_reasoning_effort = "xhigh"`
- `sandbox_mode = "read-only"`
- A description focused on correctness, regressions, security, and missing tests.
- Developer instructions requiring evidence-backed findings ordered by severity, with file references and reproduction or verification details where possible.

The reviewer must not edit files or broaden the review beyond the requested scope.

## Safety and Compatibility

Only the Multi-agent V2 feature table and the selected reasoning-effort keys are
added to the existing global configuration. Provider, authentication,
notification, project trust, MCP, plugin, and desktop settings remain
unchanged. Sensitive values in the existing configuration must never be copied
into repository files, logs, or user-facing reports.

The configuration follows the current Codex configuration reference for `agents.<name>.description` and `agents.<name>.config_file`, plus the custom-agent schema requiring `name`, `description`, and `developer_instructions`. Role files may also set normal session keys such as `model` and `sandbox_mode`.

Official references:

- https://developers.openai.com/codex/config-reference
- https://developers.openai.com/codex/subagents

## Verification

1. Parse the updated global TOML without printing sensitive values.
2. Run Codex with strict configuration validation.
3. Confirm the feature values, registered role names, model assignments,
   reasoning efforts, sandbox modes, and referenced files programmatically.
4. Restart Codex before checking the live tool schema because the current
   session cannot reload its injected collaboration tool definition.
5. Confirm that a fresh session exposes spawn metadata and run minimal Luna and
   Terra probes that report their configured role without modifying files.
6. Preserve all unrelated repository and global configuration content.

## Workflow

Sol remains the accountable owner. It inspects the task, makes architecture and
security-sensitive decisions, delegates bounded implementation work to Luna,
and delegates independent verification to Terra. Luna may edit only its
assigned scope and returns a structured handoff. Terra remains read-only and
returns evidence-first findings. Sol reviews all results, integrates changes,
runs final verification, creates focused commits, and reports the outcome.
