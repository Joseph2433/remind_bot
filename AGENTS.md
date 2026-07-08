# Repository Guidelines

## Project Structure & Module Organization

This repository is a Python 3.11+ `src/`-layout project for Lack Bot, a local Lark/Feishu notification companion for code-agent tasks.

- `src/lack_bot/cli.py`: Typer CLI entrypoints such as `run`, `send-test`, and `serve`.
- `src/lack_bot/runner.py`: subprocess execution and stdout/stderr tail capture.
- `src/lack_bot/detector.py`: output status and intervention detection.
- `src/lack_bot/redaction.py`: sensitive output masking.
- `src/lack_bot/notifier/`: notification interfaces and Lark OpenAPI client.
- `src/lack_bot/storage/`: SQLite cooldown/dedupe storage plus future backend interfaces.
- `src/lack_bot/server/`: FastAPI health, Lark challenge, and structured agent event callbacks.
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
lack-bot config --json
```

Run the CLI without installing:

```bash
$env:PYTHONPATH="src"; python -m lack_bot --help
```

Smoke-test a wrapped command:

```bash
lack-bot run --name "success smoke" -- python -c "print('ok')"
```

Start the optional API server:

```bash
lack-bot serve --host 127.0.0.1 --port 8787
```

## Coding Style & Naming Conventions

Use 4-space indentation, type hints, and focused modules with single responsibilities. Prefer small functions and explicit Pydantic models for external-facing data. Module and function names use `snake_case`; classes use `PascalCase`; constants use `UPPER_SNAKE_CASE`. Keep security-sensitive code explicit and easy to audit.

## Testing Guidelines

Tests use `pytest` and live in `tests/`. Name files `test_<module>.py` and tests `test_<behavior>()`. Add or update tests for every behavior change, especially detection, redaction, notification payloads, storage cooldowns, and CLI behavior. Avoid real network calls in tests; inject clients or test payload construction directly.

## Commit & Pull Request Guidelines

History currently uses concise Conventional Commit-style messages, for example `feat: implement lack bot mvp` and `test: define lack bot mvp behavior`. Keep commits scoped and descriptive. Pull requests should include a short summary, test results, configuration impacts, and any security considerations. Link related issues when available.

## Agent Workflow

For future agent work in this repository, follow this sequence:

1. Read and understand the current implementation before proposing changes.
2. Restate or infer the user's concrete requirement from the latest request.
3. Plan the next execution step, including files to inspect or edit and expected verification.
4. Break the plan into small executable steps.
5. After each completed step, run the relevant verification and create a commit before continuing.

Keep each commit focused on one completed step. If a step is documentation-only, a content review is enough verification; for code changes, run the targeted tests or full `python -m pytest` when practical.

## Security & Configuration Tips

Never commit `.env`, app secrets, tenant access tokens, webhook secrets, or full task logs. Keep `.env.example` placeholder-only. Notifications must send redacted summaries, not complete stdout/stderr. When changing Lark API behavior, preserve timeouts, safe logging, and token redaction.
