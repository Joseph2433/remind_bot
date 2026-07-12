# Global Codex Subagents Design

## Goal

Create two persistent Codex subagents in the user's global Codex configuration:

- `code-worker`, backed by `gpt-5.6-luna`, for scoped implementation work.
- `code-reviewer`, backed by `gpt-5.6-terra`, for read-only code review.

The existing `gpt-5.6-sol` main-thread configuration remains unchanged.

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
- `sandbox_mode = "workspace-write"`
- A description that limits the role to bounded implementation tasks.
- Developer instructions requiring minimal scoped changes, preservation of unrelated work, relevant verification, and a structured handoff.

The worker must report ambiguity or scope expansion to Sol instead of making product or architecture decisions independently.

## Code Reviewer

`~/.codex/agents/code-reviewer.toml` defines:

- `name = "code-reviewer"`
- `model = "gpt-5.6-terra"`
- `sandbox_mode = "read-only"`
- A description focused on correctness, regressions, security, and missing tests.
- Developer instructions requiring evidence-backed findings ordered by severity, with file references and reproduction or verification details where possible.

The reviewer must not edit files or broaden the review beyond the requested scope.

## Safety and Compatibility

Only the `[agents]` namespace is added to the existing global configuration. Provider, authentication, notification, project trust, MCP, plugin, and desktop settings remain unchanged. Sensitive values in the existing configuration must never be copied into repository files, logs, or user-facing reports.

The configuration follows the current Codex configuration reference for `agents.<name>.description` and `agents.<name>.config_file`, plus the custom-agent schema requiring `name`, `description`, and `developer_instructions`. Role files may also set normal session keys such as `model` and `sandbox_mode`.

Official references:

- https://developers.openai.com/codex/config-reference
- https://developers.openai.com/codex/subagents

## Verification

1. Parse the updated global TOML without printing sensitive values.
2. Run Codex with strict configuration validation.
3. Confirm the registered role names, model assignments, sandbox modes, and referenced files programmatically.
4. Preserve all unrelated repository and global configuration content.
