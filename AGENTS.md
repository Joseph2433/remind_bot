# Repository Guidelines

## Project Structure & Module Organization

This repository is a Python 3.11+ `src/`-layout project for Lark Bot, a local Lark/Feishu notification companion for code-agent tasks.

- `src/lark_bot/cli.py`: Typer CLI entrypoints such as `run`, `send-test`, and `serve`.
- `src/lark_bot/runner.py`: subprocess execution and stdout/stderr tail capture.
- `src/lark_bot/detector.py`: output status and intervention detection.
- `src/lark_bot/redaction.py`: sensitive output masking.
- `src/lark_bot/adapters/`: agent-specific event adapters such as Codex.
- `src/lark_bot/notifier/`: notification interfaces and Lark OpenAPI client.
- `src/lark_bot/storage/`: SQLite cooldown/dedupe storage plus future backend interfaces.
- `src/lark_bot/server/`: FastAPI health, Lark challenge, and structured agent event callbacks.
- `tests/`: pytest coverage for core behavior.

## Build, Test, and Development Commands

Install locally:

```bash
python -m pip install -e ".[dev]"
```

Run tests:

```bash
python -m pytest
```

Check configuration safely:

```bash
lark-bot config --json
```

Send a Codex event payload:

```bash
lark-bot codex-event --file codex-event.json
```

Run the CLI without installing:

```bash
$env:PYTHONPATH="src"; python -m lark_bot --help
```

Smoke-test a wrapped command:

```bash
lark-bot run --name "success smoke" -- python -c "print('ok')"
```

Start the optional API server:

```bash
lark-bot serve --host 127.0.0.1 --port 8787
```

## Coding Style & Naming Conventions

Use 4-space indentation, type hints, and focused modules with single responsibilities. Prefer small functions and explicit Pydantic models for external-facing data. Module and function names use `snake_case`; classes use `PascalCase`; constants use `UPPER_SNAKE_CASE`. Keep security-sensitive code explicit and easy to audit.

## Testing Guidelines

Tests use `pytest` and live in `tests/`. Name files `test_<module>.py` and tests `test_<behavior>()`. Add or update tests for every behavior change, especially detection, redaction, notification payloads, storage cooldowns, and CLI behavior. Avoid real network calls in tests; inject clients or test payload construction directly.

## Commit & Pull Request Guidelines

History currently uses concise Conventional Commit-style messages, for example `feat: implement Lark Bot mvp` and `test: define Lark Bot mvp behavior`. Keep commits scoped and descriptive. Pull requests should include a short summary, test results, configuration impacts, and any security considerations. Link related issues when available.

## Git Development Workflow

Use `dev` as the integration branch for new work. Before starting a feature, update `dev` and create a focused branch from it:

```bash
git checkout dev
git pull origin dev
git checkout -b feat/<short-topic>
```

Commit each completed step locally. When the feature is complete, push the feature branch, but do not open or merge the pull request yourself:

```bash
git push -u origin feat/<short-topic>
```

The repository owner will create, review, and merge the PR. Keep `master` for stable releases and avoid committing feature work directly to `master`.

## Agent Workflow

For future agent work in this repository, follow this sequence:

1. Read and understand the current implementation before proposing changes.
2. Restate or infer the user's concrete requirement from the latest request.
3. Plan the next execution step, including files to inspect or edit and expected verification.
4. Break the plan into small executable steps.
5. After each completed step, run the relevant verification and create a commit before continuing.

Keep each commit focused on one completed step. If a step is documentation-only, a content review is enough verification; for code changes, run the targeted tests or full `python -m pytest` when practical.

## Multi-Agent Workflow

The available models are `gpt-5.6-sol`, `gpt-5.6-luna`, and `gpt-5.6-terra`. Use `gpt-5.6-sol` as the main thread for every task. Sol is the single accountable owner and is responsible for understanding requirements, inspecting the repository, planning, architecture decisions, task decomposition, delegation, integration, final verification, commits, and the final user-facing report.

Use the other models as subagents with default specialties:

- `gpt-5.6-luna` is the default implementation subagent. Assign Luna scoped coding, local refactors, straightforward bug fixes, and other changes with explicit file and acceptance boundaries.
- `gpt-5.6-terra` is the default verification subagent. Assign Terra tests, static checks, regression analysis, code review, and documentation updates.

These specialties are defaults, not rigid restrictions. Sol may dynamically reassign Luna or Terra based on task fit, workload, independence, and available context. Sol must retain architecture decisions, security-sensitive work, ambiguous requirements, cross-module integration, conflict resolution, and final acceptance.

When delegating work, Sol must provide:

1. A concrete objective and expected deliverable.
2. The allowed scope, relevant files, and prohibited changes.
3. Repository conventions and task-specific constraints.
4. Exact verification or acceptance criteria.
5. The required handoff format.

Delegate only bounded tasks that can be completed and verified independently. Subagents must not broaden their scope or make product-level decisions. If a task reveals ambiguity, overlapping ownership, or required work outside its boundary, the subagent must stop that part of the work and report it to Sol.

Before delegation, Sol must inspect the working tree and preserve unrelated user changes. Parallel tasks should use disjoint files or clearly separated regions. Do not let Luna and Terra edit the same file concurrently unless Sol explicitly partitions ownership and accepts responsibility for resolving conflicts.

Each subagent handoff must include:

- A concise summary of completed work.
- Files changed or inspected.
- Verification commands and their results.
- Assumptions, risks, and unresolved issues.
- Suggested follow-up work, without performing work outside the assigned scope.

Subagents should not create final integration commits unless Sol explicitly delegates a self-contained commit. Sol must review every resulting diff, resolve conflicts, confirm repository conventions, run the final relevant verification, and create focused commits following the repository's existing Git workflow.

For each task, follow this execution sequence:

1. Sol reads the implementation and working-tree state.
2. Sol restates the requirement and creates a small-step plan.
3. Sol keeps high-risk and integrative work in the main thread and delegates suitable bounded tasks.
4. Luna and Terra complete their assignments and return structured handoffs.
5. Sol reviews and integrates the results, then runs targeted or full verification as appropriate.
6. Sol creates a focused commit for each completed step.
7. Sol reports the integrated outcome, verification status, risks, and remaining work to the user.

## Security & Configuration Tips

Never commit `.env`, app secrets, tenant access tokens, webhook secrets, or full task logs. Keep `.env.example` placeholder-only. Notifications must send redacted summaries, not complete stdout/stderr. When changing Lark API behavior, preserve timeouts, safe logging, and token redaction.
