# Codex TUI Companion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `lark-bot codex` launch the native Codex TUI with a structured Lark companion that delays notifications by five seconds and arbitrates terminal/Lark responses with first-response-wins semantics.

**Architecture:** Deliver in two stages. Phase A makes `lark-bot codex` a transparent native-TUI launcher and uses a verified Codex hook/notify sidecar for delayed one-way notifications. Phase B reuses a loopback Codex app-server for structured approvals and input, persists interactions in SQLite, and uses the existing CAS transition as the single response winner. Existing unattended sessions move under `lark-bot codex job`.

**Tech Stack:** Python 3.11, Typer, asyncio, Codex app-server JSON-RPC/WebSocket protocol, SQLite, Pydantic, Lark long connection, pytest.

---

## File Map

- Create `src/lark_bot/codex_tui.py`: transparent native Codex process lifecycle and later loopback app-server attachment.
- Create `src/lark_bot/codex_hook_adapter.py`: normalize verified Codex callback payloads without blocking the TUI.
- Create `src/lark_bot/codex_companion.py`: structured event observation, delay scheduling, and response arbitration.
- Modify `src/lark_bot/cli.py`: make bare `lark-bot codex` interactive and move unattended commands under `codex job`.
- Modify `src/lark_bot/config.py`: add the five-second notification delay setting.
- Modify `src/lark_bot/codex_storage.py`: add outbox cancellation and actor-aware interaction claims if the current interfaces are insufficient.
- Modify `src/lark_bot/codex_orchestrator.py`: reuse normalized interaction and response mapping without owning the TUI thread.
- Modify `src/lark_bot/daemon.py`: register/deregister interactive companion sessions and accept terminal-resolution events.
- Modify `README.md` and `.env.example`: document the corrected primary workflow.
- Create `tests/test_codex_tui.py` and `tests/test_codex_companion.py`; update existing CLI, storage, orchestrator, and daemon tests.

### Task 1: Implement the native TUI launcher and callback sidecar

**Files:**
- Create: `src/lark_bot/codex_tui.py`
- Create: `src/lark_bot/codex_hook_adapter.py`
- Create: `tests/test_codex_tui.py`
- Create: `tests/test_codex_hook_adapter.py`
- Modify: `src/lark_bot/hooks.py`
- Modify: `src/lark_bot/cli.py`

- [ ] **Step 1: Write failing lifecycle and argument-forwarding tests**

Define a fake process collaborator and assert that Phase A `CodexTuiLauncher.run()` invokes the resolved Codex executable directly, forwards prompt/model/sandbox/resume arguments, inherits console streams, and returns the child exit code. Add callback tests for argv and stdin payload forms, size limits, redaction, and immediate return when the daemon is unavailable.

- [ ] **Step 2: Run the focused tests and verify the missing module failure**

Run: `python -m pytest tests/test_codex_tui.py -q`

Expected: collection fails because `lark_bot.codex_tui` and `lark_bot.codex_hook_adapter` do not exist.

- [ ] **Step 3: Implement the minimal launcher and capability checks**

Add typed `CodexTuiOptions` and `CodexTuiLauncher` units. Resolve the Windows command shim with `shutil.which`, invoke native `codex`, and keep stdin/stdout/stderr inherited by omitting pipe arguments. Normalize only verified callback payload fields and forward them through the existing bounded hook/spool path.

- [ ] **Step 4: Run a real hook/notify smoke**

Install the callback into a disposable project configuration, launch native Codex without submitting a model turn where possible, and verify that the installed Codex version accepts the configuration. Then run one minimal lifecycle callback payload through the real executable and confirm it reaches the daemon or spool without blocking.

- [ ] **Step 5: Run focused tests and commit**

Run: `python -m pytest tests/test_codex_tui.py -q`

Commit: `feat: add native codex tui sidecar`

### Task 2: Validate and model the shared app-server topology

**Files:**
- Modify: `src/lark_bot/codex_tui.py`
- Create: `tests/test_codex_tui_protocol.py`

- [ ] **Step 1: Generate the installed Codex app-server schema**

Generate the Codex 0.144.1 experimental schema into ignored `.tmp/` storage and assert the required approval and `item/tool/requestUserInput` server requests exist.

- [ ] **Step 2: Run a real two-client no-turn smoke**

Start the app-server on an ephemeral loopback endpoint, attach the companion and native TUI transport, initialize, and close without starting a model turn. Record whether shared-client routing is supported; otherwise select the structured JSON-RPC gateway topology.

- [ ] **Step 3: Add endpoint and capability models**

Add typed `CodexEndpoint` and capability validation without changing the Phase A native fallback.

- [ ] **Step 4: Run focused tests and commit**

Run: `python -m pytest tests/test_codex_tui.py tests/test_codex_tui_protocol.py -q`

Commit: `feat: validate shared codex app server`

### Task 3: Add delayed companion event scheduling

**Files:**
- Create: `src/lark_bot/codex_companion.py`
- Create: `tests/test_codex_companion.py`
- Modify: `src/lark_bot/config.py`
- Modify: `.env.example`

- [ ] **Step 1: Write failing tests for the uniform five-second delay**

Use an injected clock and assert that ordinary events and intervention events are enqueued with `not_before = now + timedelta(seconds=5)`. Assert that an intervention resolved before its due time is cancelled while a completion event remains due.

- [ ] **Step 2: Implement normalized events and the scheduler**

Add `CompanionEvent`, `CompanionEventKind`, and `CodexCompanion.schedule_event()`. Read `notification_delay_seconds` from settings with a default of `5.0`; persist only redacted summaries and identifiers.

- [ ] **Step 3: Add outbox cancellation by interaction ID**

Extend storage with a conditional operation that marks an unsent interaction outbox item cancelled only when the interaction is no longer pending. Do not delete audit history.

- [ ] **Step 4: Run focused tests and commit**

Run: `python -m pytest tests/test_codex_companion.py tests/test_codex_storage.py tests/test_config.py -q`

Commit: `feat: add delayed codex companion events`

### Task 4: Implement terminal/Lark first-response-wins

**Files:**
- Modify: `src/lark_bot/codex_companion.py`
- Modify: `src/lark_bot/codex_storage.py`
- Modify: `src/lark_bot/codex_orchestrator.py`
- Test: `tests/test_codex_companion.py`
- Test: `tests/test_codex_storage.py`
- Test: `tests/test_codex_orchestrator.py`

- [ ] **Step 1: Write deterministic race tests**

Run terminal and Lark claim coroutines against the same pending interaction. Assert exactly one claim succeeds, exactly one protocol response is emitted, and the stored `actor_id` identifies the winner. Cover terminal-first, Lark-first, repeated reaction, repeated reply, and response-after-expiry cases.

- [ ] **Step 2: Reuse one response mapping path**

Extract approval and user-input response construction so both terminal and Lark actors use the same request-kind validation and protocol payload mapping.

- [ ] **Step 3: Observe terminal resolution structurally**

Consume the app-server response/resolution event associated with the pending request and claim it with `actor_id="terminal"`. On a successful claim, cancel its unsent Lark outbox entry; if already sent, enqueue a compact `resolved_by_terminal` update.

- [ ] **Step 4: Forward a winning Lark response and synchronize the TUI**

Claim with the Lark actor before writing to app-server. Forward only after the claim succeeds. Let the app-server resolution stream dismiss the native TUI prompt; enqueue `resolved_by_lark` for message correlation and audit.

- [ ] **Step 5: Run focused tests and commit**

Run: `python -m pytest tests/test_codex_companion.py tests/test_codex_storage.py tests/test_codex_orchestrator.py -q`

Commit: `feat: arbitrate codex terminal and lark input`

### Task 5: Complete the interactive entry point

**Files:**
- Modify: `src/lark_bot/cli.py`
- Modify: `src/lark_bot/daemon.py`
- Test: `tests/test_cli_codex.py`
- Test: `tests/test_codex_daemon.py`

- [ ] **Step 1: Write failing CLI contract tests**

Assert that invoking `lark-bot codex` calls the TUI launcher, forwards Codex options, and propagates the exit code. Assert that `codex job start/list/show/cancel` preserve the existing unattended API behavior and that the previous ambiguous `codex start` command is no longer advertised.

- [ ] **Step 2: Add the Typer callback and job namespace**

Configure the `codex` Typer group with `invoke_without_command=True`. When no subcommand is selected, run `CodexTuiLauncher`; register the existing unattended commands on a nested `job_app`.

- [ ] **Step 3: Connect companion registration to daemon runtime**

Register the active endpoint/session before launching the TUI, start Lark event consumption, and always deregister in `finally`. Preserve terminal usability if Lark delivery fails after startup.

- [ ] **Step 4: Run focused tests and commit**

Run: `python -m pytest tests/test_cli_codex.py tests/test_codex_daemon.py -q`

Commit: `feat: launch interactive codex companion`

### Task 6: Documentation, compatibility smoke, and branch completion

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: relevant tests if the installed Codex protocol requires a documented compatibility adjustment

- [ ] **Step 1: Document the primary interactive workflow**

Lead with `lark-bot daemon` in one terminal and `lark-bot codex` in another. Explain the five-second delay, first-response-wins behavior, Lark reactions/replies, `--no-lark`, and the separate `codex job` namespace.

- [ ] **Step 2: Run the real interactive connection smoke**

Launch the installed Codex 0.144.1 app-server and connect the native TUI through the selected loopback topology without submitting a model prompt. Verify initialize, attach, detach, and shutdown behavior on Windows.

- [ ] **Step 3: Run final verification**

Run:

```powershell
python -m pytest -q
git diff --check
```

Expected: all tests pass; only the existing Starlette/httpx deprecation warning may remain.

- [ ] **Step 4: Commit and push**

Commit: `docs: document interactive codex companion`

Push `feat/codex-automation-harness` to `origin` without opening or merging a pull request.
