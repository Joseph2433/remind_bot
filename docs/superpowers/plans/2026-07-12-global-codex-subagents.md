# Global Codex Subagents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Register global `code-worker` and `code-reviewer` Codex subagents using Luna and Terra with appropriate write and review permissions.

**Architecture:** Add global concurrency and role registration under `[agents]` in the existing user `config.toml`. Store each role's model, sandbox, description, and developer instructions in a dedicated TOML file under the global Codex agents directory, then validate the result without printing unrelated or sensitive configuration values.

**Tech Stack:** TOML, Codex CLI, Python 3.11 `tomllib`, PowerShell

---

### Task 1: Create the global agent role files

**Files:**
- Create: `C:\Users\na'na'ba\.codex\agents\code-worker.toml`
- Create: `C:\Users\na'na'ba\.codex\agents\code-reviewer.toml`

- [ ] **Step 1: Create the agents directory if necessary**

```powershell
New-Item -ItemType Directory -Force -Path "C:\Users\na'na'ba\.codex\agents"
```

- [ ] **Step 2: Create the worker configuration**

Write this exact TOML to `C:\Users\na'na'ba\.codex\agents\code-worker.toml`:

```toml
name = "code-worker"
description = "Implementation subagent for scoped coding, local refactors, and straightforward fixes."
model = "gpt-5.6-luna"
sandbox_mode = "workspace-write"
developer_instructions = """
Implement only the concrete, bounded task delegated by the parent agent.
Make the smallest defensible change and preserve unrelated user work.
Follow repository instructions, coding conventions, and security requirements.
Do not make architecture or product decisions, broaden scope, or modify files outside the assignment.
Run the relevant targeted verification after editing.
Return a concise handoff with the work completed, files changed, verification commands and results, assumptions, risks, and unresolved issues.
"""
```

- [ ] **Step 3: Create the reviewer configuration**

Write this exact TOML to `C:\Users\na'na'ba\.codex\agents\code-reviewer.toml`:

```toml
name = "code-reviewer"
description = "Read-only reviewer for correctness, regressions, security, and missing tests."
model = "gpt-5.6-terra"
sandbox_mode = "read-only"
developer_instructions = """
Review only the scope delegated by the parent agent and do not edit files.
Prioritize correctness defects, behavior regressions, security risks, data-loss risks, and missing test coverage.
Support every finding with concrete evidence, including file and line references plus reproduction or verification details when possible.
Order findings by severity and distinguish confirmed defects from residual risks or questions.
Avoid style-only comments unless they obscure correctness or maintainability risks.
Return findings first, followed by verification performed, assumptions, and any remaining gaps.
"""
```

### Task 2: Register the roles in global configuration

**Files:**
- Modify: `C:\Users\na'na'ba\.codex\config.toml`

- [ ] **Step 1: Preserve a private backup**

Create a timestamped backup beside the original global configuration. Do not copy the backup into the repository or print its contents.

- [ ] **Step 2: Add the global agent tables**

Insert the following block before `[model_providers]`, preserving every existing setting:

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

If an `[agents]` namespace already exists at execution time, merge these values without duplicating tables or deleting unrelated agent settings.

### Task 3: Validate the global configuration

**Files:**
- Verify: `C:\Users\na'na'ba\.codex\config.toml`
- Verify: `C:\Users\na'na'ba\.codex\agents\code-worker.toml`
- Verify: `C:\Users\na'na'ba\.codex\agents\code-reviewer.toml`

- [ ] **Step 1: Parse and assert selected TOML values**

Use Python 3.11 `tomllib` to assert, without printing the full files:

```python
from pathlib import Path
import tomllib

home = Path.home() / ".codex"
with (home / "config.toml").open("rb") as stream:
    config = tomllib.load(stream)
with (home / "agents" / "code-worker.toml").open("rb") as stream:
    worker = tomllib.load(stream)
with (home / "agents" / "code-reviewer.toml").open("rb") as stream:
    reviewer = tomllib.load(stream)

assert config["agents"]["max_threads"] == 3
assert config["agents"]["max_depth"] == 1
assert config["agents"]["code-worker"]["config_file"] == "agents/code-worker.toml"
assert config["agents"]["code-reviewer"]["config_file"] == "agents/code-reviewer.toml"
assert worker["name"] == "code-worker"
assert worker["model"] == "gpt-5.6-luna"
assert worker["sandbox_mode"] == "workspace-write"
assert reviewer["name"] == "code-reviewer"
assert reviewer["model"] == "gpt-5.6-terra"
assert reviewer["sandbox_mode"] == "read-only"
print("global subagent configuration verified")
```

Expected: `global subagent configuration verified`.

- [ ] **Step 2: Run Codex strict configuration validation**

Run:

```powershell
codex --strict-config features list
```

Expected: exit code 0 with no unknown-field or TOML errors. If validation fails, restore the private backup before reporting the failure.

### Task 4: Record completion

**Files:**
- Verify: `docs/superpowers/specs/2026-07-12-global-codex-subagents-design.md`
- Verify: `docs/superpowers/plans/2026-07-12-global-codex-subagents.md`

- [ ] **Step 1: Confirm repository status**

Run `git status --short` and ensure no global configuration or backup file appears in repository changes.

- [ ] **Step 2: Commit the implementation plan**

```bash
git add docs/superpowers/plans/2026-07-12-global-codex-subagents.md
git commit -m "docs: plan global codex subagents"
```
