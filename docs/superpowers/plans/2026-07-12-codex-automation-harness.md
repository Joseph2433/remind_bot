# Codex Automation Harness Implementation Plan

## Delivery Sequence

1. Add session and interaction domain models plus transactional SQLite persistence.
2. Add a narrow Codex app-server JSON-RPC supervisor and protocol adapter.
3. Add daemon orchestration, local authenticated API, timeouts, and restart reconciliation.
4. Add Lark long-connection event routing, reaction approval, reply input, and notification outbox.
5. Add managed-session CLI commands and project hook install/check/uninstall commands.
6. Update configuration and documentation, run the complete test suite, review, and push the feature branch.

## Required Workflow

- Develop from `feat/codex-automation-harness`, based on the latest `dev`.
- Follow test-driven development for every behavior change.
- Run targeted tests after each red/green cycle and `python -m pytest` before each code commit.
- Keep commits focused and use Conventional Commit messages.
- Push the feature branch, but do not create or merge a pull request.
