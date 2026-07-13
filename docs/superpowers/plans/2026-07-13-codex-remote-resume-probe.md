# Codex Remote Resume Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add and run a safe, opt-in diagnostic that proves whether the installed Codex app-server supports the two simultaneous remote clients required by the `/resume` picker.

**Architecture:** Keep the probe outside production startup paths. A small typed module launches one loopback app-server, initializes two independent WebSocket clients, lists threads on both, optionally resumes one existing thread from the secondary client, then proves the primary remains usable. Unit tests inject fake connections and process collaborators; the real probe runs only when explicitly invoked.

**Tech Stack:** Python 3.11, asyncio, websockets 15, Codex app-server JSON-RPC, pytest.

---

## File Map

- Create `src/lark_bot/codex_remote_probe.py`: process lifecycle, JSON-RPC helpers, safe structured result, and `python -m` entry point.
- Create `tests/test_codex_remote_probe.py`: deterministic tests for two-client sequencing, resume/no-thread paths, redacted output, and cleanup.
- Create `docs/superpowers/reports/2026-07-13-codex-remote-resume-probe.md`: installed-version result and the architecture decision unlocked by the probe.
- Do not modify gateway, orchestrator, storage, CLI, or daemon code in this phase.

### Task 1: Define the probe contract with failing tests

**Files:**
- Create: `tests/test_codex_remote_probe.py`
- Test: `tests/test_codex_remote_probe.py`

- [ ] **Step 1: Write the failing result and sequencing tests**

Create fakes that record requests per client and return deterministic JSON-RPC
responses. The tests must define these observable requirements:

```python
from __future__ import annotations

import asyncio
import json

from lark_bot.codex_remote_probe import (
    ProbeResult,
    probe_remote_clients,
    run_local_probe,
)


class FakeSocket:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = asyncio.Queue()
        for response in responses:
            self.responses.put_nowait(json.dumps(response))
        self.sent: list[dict[str, object]] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        return await self.responses.get()

    async def close(self) -> None:
        self.closed = True


def test_probe_initializes_two_clients_resumes_and_rechecks_primary() -> None:
    async def scenario() -> None:
        primary = FakeSocket(
            [
                {"id": 1, "result": {"userAgent": "codex-test"}},
                {"id": 2, "result": {"data": [{"id": "thread-secret"}]}},
                {"id": 3, "result": {"data": [{"id": "thread-secret"}]}},
            ]
        )
        secondary = FakeSocket(
            [
                {"id": 1, "result": {"userAgent": "codex-test"}},
                {"id": 2, "result": {"data": [{"id": "thread-secret"}]}},
                {"id": 3, "result": {"thread": {"id": "thread-secret"}}},
            ]
        )

        result = await probe_remote_clients(primary, secondary)

        assert result == ProbeResult(
            multi_client=True,
            both_initialized=True,
            both_listed_threads=True,
            resume_attempted=True,
            resume_succeeded=True,
            primary_survived=True,
            error_type=None,
        )
        assert [message.get("method") for message in primary.sent] == [
            "initialize",
            "initialized",
            "thread/list",
            "thread/list",
        ]
        assert [message.get("method") for message in secondary.sent] == [
            "initialize",
            "initialized",
            "thread/list",
            "thread/resume",
        ]
        assert "thread-secret" not in json.dumps(result.to_public_dict())

    asyncio.run(scenario())


def test_probe_without_saved_threads_still_proves_two_client_listing() -> None:
    async def scenario() -> None:
        primary = FakeSocket(
            [
                {"id": 1, "result": {}},
                {"id": 2, "result": {"data": []}},
                {"id": 3, "result": {"data": []}},
            ]
        )
        secondary = FakeSocket(
            [
                {"id": 1, "result": {}},
                {"id": 2, "result": {"data": []}},
            ]
        )

        result = await probe_remote_clients(primary, secondary)

        assert result.multi_client is True
        assert result.resume_attempted is False
        assert result.resume_succeeded is False
        assert result.primary_survived is True

    asyncio.run(scenario())
```

- [ ] **Step 2: Add cleanup and bounded-failure tests**

Add tests using injected `process_factory`, `connector`, `wait_listener`, and
`version_reader` collaborators:

```python
class FakeProcess:
    def __init__(self, *, wait_blocks_after_terminate: bool = False) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.wait_blocks_after_terminate = wait_blocks_after_terminate
        self._done = asyncio.Event()

    def terminate(self) -> None:
        self.terminated = True
        if not self.wait_blocks_after_terminate:
            self.returncode = 0
            self._done.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._done.set()

    async def wait(self) -> int:
        await self._done.wait()
        return self.returncode or 0


def test_local_probe_closes_both_clients_and_terminates_process_on_success() -> None:
    async def scenario() -> None:
        process = FakeProcess()
        primary = FakeSocket(
            [
                {"id": 1, "result": {}},
                {"id": 2, "result": {"data": []}},
                {"id": 3, "result": {"data": []}},
            ]
        )
        secondary = FakeSocket(
            [
                {"id": 1, "result": {}},
                {"id": 2, "result": {"data": []}},
            ]
        )
        sockets = iter([primary, secondary])

        result = await run_local_probe(
            which=lambda value: "C:/tools/codex.exe",
            process_factory=lambda *args, **kwargs: asyncio.sleep(0, result=process),
            connector=lambda *args, **kwargs: asyncio.sleep(0, result=next(sockets)),
            wait_listener=lambda endpoint, child: asyncio.sleep(0),
            endpoint_factory=lambda: "ws://127.0.0.1:6123",
            version_reader=lambda executable: "codex-cli test",
        )

        assert result.multi_client is True
        assert result.codex_version == "codex-cli test"
        assert primary.closed and secondary.closed
        assert process.terminated and not process.killed

    asyncio.run(scenario())


def test_local_probe_reports_connection_error_without_exposing_message_text() -> None:
    async def scenario() -> None:
        process = FakeProcess()

        async def fail_connect(*args, **kwargs):
            raise OSError("ws://127.0.0.1:6123 token=secret")

        result = await run_local_probe(
            which=lambda value: "C:/tools/codex.exe",
            process_factory=lambda *args, **kwargs: asyncio.sleep(0, result=process),
            connector=fail_connect,
            wait_listener=lambda endpoint, child: asyncio.sleep(0),
            endpoint_factory=lambda: "ws://127.0.0.1:6123",
            version_reader=lambda executable: "codex-cli test",
        )

        public = json.dumps(result.to_public_dict())
        assert result.error_type == "OSError"
        assert "6123" not in public
        assert "secret" not in public
        assert process.terminated

    asyncio.run(scenario())


def test_local_probe_kills_process_when_graceful_termination_times_out() -> None:
    async def scenario() -> None:
        process = FakeProcess(wait_blocks_after_terminate=True)

        async def fail_connect(*args, **kwargs):
            raise ConnectionError("unavailable")

        await run_local_probe(
            which=lambda value: "C:/tools/codex.exe",
            process_factory=lambda *args, **kwargs: asyncio.sleep(0, result=process),
            connector=fail_connect,
            wait_listener=lambda endpoint, child: asyncio.sleep(0),
            endpoint_factory=lambda: "ws://127.0.0.1:6123",
            version_reader=lambda executable: "codex-cli test",
            close_timeout=0.01,
        )

        assert process.terminated and process.killed

    asyncio.run(scenario())
```

The public result may contain only booleans, Codex version, and an exception
class name. It must not contain endpoint tokens, thread IDs, prompts, raw RPC
payloads, or stderr.

- [ ] **Step 3: Run the focused test and verify failure**

Run:

```powershell
python -m pytest tests/test_codex_remote_probe.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named
'lark_bot.codex_remote_probe'`.

- [ ] **Step 4: Commit the failing contract**

```powershell
git add tests/test_codex_remote_probe.py
git commit -m "test: define codex remote resume probe"
```

### Task 2: Implement the deterministic probe core

**Files:**
- Create: `src/lark_bot/codex_remote_probe.py`
- Test: `tests/test_codex_remote_probe.py`

- [ ] **Step 1: Add the safe result model and RPC helpers**

Implement the following public shape. Keep socket/process protocols structural
so tests do not require real network or subprocesses:

```python
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from websockets.asyncio.client import connect

from lark_bot.codex_interactive import _loopback_endpoint, _wait_for_listener


class ProbeSocket(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str: ...
    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ProbeResult:
    multi_client: bool = False
    both_initialized: bool = False
    both_listed_threads: bool = False
    resume_attempted: bool = False
    resume_succeeded: bool = False
    primary_survived: bool = False
    error_type: str | None = None
    codex_version: str | None = None

    def to_public_dict(self) -> dict[str, object]:
        return asdict(self)


async def _rpc(
    socket: ProbeSocket,
    request_id: int,
    method: str,
    params: dict[str, object],
    *,
    timeout: float,
) -> dict[str, Any]:
    await socket.send(json.dumps({"id": request_id, "method": method, "params": params}))
    while True:
        raw = await asyncio.wait_for(socket.recv(), timeout=timeout)
        message = json.loads(raw)
        if isinstance(message, dict) and message.get("id") == request_id:
            if "error" in message:
                raise RuntimeError("Codex app-server RPC failed")
            result = message.get("result")
            return result if isinstance(result, dict) else {}


async def _initialize(socket: ProbeSocket, name: str, *, timeout: float) -> None:
    await _rpc(
        socket,
        1,
        "initialize",
        {"clientInfo": {"name": name, "title": "Lark Bot Probe", "version": "0.1.0"}},
        timeout=timeout,
    )
    await socket.send(json.dumps({"method": "initialized", "params": {}}))
```

- [ ] **Step 2: Implement the two-client sequence**

Implement the sequence exactly as follows. Request IDs `2` and `3` are reused
independently per socket so the probe also proves that identical client-scoped
IDs are valid on separate app-server connections.

```python
def _first_thread_id(result: dict[str, Any]) -> str | None:
    rows = result.get("data")
    if not isinstance(rows, list):
        rows = result.get("items")
    if not isinstance(rows, list):
        return None
    for row in rows:
        thread_id = row.get("id") if isinstance(row, dict) else None
        if isinstance(thread_id, str) and thread_id:
            return thread_id
    return None


async def probe_remote_clients(
    primary: ProbeSocket,
    secondary: ProbeSocket,
    *,
    timeout: float = 5.0,
) -> ProbeResult:
    multi_client = False
    both_initialized = False
    both_listed_threads = False
    resume_attempted = False
    resume_succeeded = False
    primary_survived = False
    try:
        await _initialize(primary, "lark_bot_probe_primary", timeout=timeout)
        await _initialize(secondary, "lark_bot_probe_secondary", timeout=timeout)
        multi_client = True
        both_initialized = True

        primary_list = await _rpc(primary, 2, "thread/list", {}, timeout=timeout)
        secondary_list = await _rpc(secondary, 2, "thread/list", {}, timeout=timeout)
        both_listed_threads = True

        thread_id = _first_thread_id(secondary_list) or _first_thread_id(primary_list)
        if thread_id is not None:
            resume_attempted = True
            await _rpc(
                secondary,
                3,
                "thread/resume",
                {"threadId": thread_id},
                timeout=timeout,
            )
            resume_succeeded = True

        await _rpc(primary, 3, "thread/list", {}, timeout=timeout)
        primary_survived = True
        return ProbeResult(
            multi_client=multi_client,
            both_initialized=both_initialized,
            both_listed_threads=both_listed_threads,
            resume_attempted=resume_attempted,
            resume_succeeded=resume_succeeded,
            primary_survived=primary_survived,
        )
    except BaseException as error:
        return ProbeResult(
            multi_client=multi_client,
            both_initialized=both_initialized,
            both_listed_threads=both_listed_threads,
            resume_attempted=resume_attempted,
            resume_succeeded=resume_succeeded,
            primary_survived=primary_survived,
            error_type=type(error).__name__,
        )
```

- [ ] **Step 3: Implement local process lifecycle and the module entry point**

Implement the local lifecycle with this exact injectable interface and cleanup
order:

```python
def _default_version_reader(executable: str) -> str:
    completed = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    lines = completed.stdout.strip().splitlines()
    return lines[0][:200] if lines else "unknown"


async def _stop_process(process: Any, *, timeout: float) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()


async def run_local_probe(
    codex_path: str = "codex",
    *,
    which: Callable[[str], str | None] = shutil.which,
    process_factory: Callable[..., Awaitable[Any]] = asyncio.create_subprocess_exec,
    connector: Callable[..., Awaitable[ProbeSocket]] = connect,
    wait_listener: Callable[[str, Any], Awaitable[None]] = _wait_for_listener,
    endpoint_factory: Callable[[], str] = _loopback_endpoint,
    version_reader: Callable[[str], str] = _default_version_reader,
    timeout: float = 5.0,
    close_timeout: float = 2.0,
) -> ProbeResult:
    executable = which(codex_path)
    if executable is None:
        return ProbeResult(error_type="FileNotFoundError")

    try:
        codex_version = version_reader(executable)
    except BaseException:
        codex_version = "unknown"

    endpoint = endpoint_factory()
    process: Any | None = None
    sockets: list[ProbeSocket] = []
    try:
        process = await process_factory(
            executable,
            "app-server",
            "--listen",
            endpoint,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await wait_listener(endpoint, process)
        primary = await connector(endpoint, proxy=None, open_timeout=timeout)
        sockets.append(primary)
        secondary = await connector(endpoint, proxy=None, open_timeout=timeout)
        sockets.append(secondary)
        result = await probe_remote_clients(primary, secondary, timeout=timeout)
        return ProbeResult(**{**result.to_public_dict(), "codex_version": codex_version})
    except BaseException as error:
        return ProbeResult(error_type=type(error).__name__, codex_version=codex_version)
    finally:
        for socket in reversed(sockets):
            try:
                await socket.close()
            except BaseException:
                pass
        if process is not None:
            await _stop_process(process, timeout=close_timeout)


def main() -> None:
    result = asyncio.run(run_local_probe())
    print(json.dumps(result.to_public_dict(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
```

Do not print or persist the generated endpoint, thread ID, raw response, token,
prompt, or child stderr.

- [ ] **Step 4: Run focused tests and verify success**

Run:

```powershell
python -m pytest tests/test_codex_remote_probe.py -q
```

Expected: all probe unit tests pass.

- [ ] **Step 5: Run surrounding regression tests**

Run:

```powershell
python -m pytest tests/test_codex_interactive.py tests/test_codex_gateway.py tests/test_codex_app_server.py -q
```

Expected: all existing tests pass.

- [ ] **Step 6: Commit the implementation**

```powershell
git add src/lark_bot/codex_remote_probe.py tests/test_codex_remote_probe.py
git commit -m "feat: add codex remote resume probe"
```

### Task 3: Run the real probe and record the decision

**Files:**
- Create: `docs/superpowers/reports/2026-07-13-codex-remote-resume-probe.md`
- Inspect: `src/lark_bot/codex_remote_probe.py`

- [ ] **Step 1: Run the real installed-version probe**

Run:

```powershell
$env:PYTHONPATH="src"
python -m lark_bot.codex_remote_probe
```

Expected output is one JSON object. A complete pass requires:

```json
{
  "multi_client": true,
  "both_initialized": true,
  "both_listed_threads": true,
  "resume_attempted": true,
  "resume_succeeded": true,
  "primary_survived": true,
  "error_type": null,
  "codex_version": "codex-cli 0.144.1"
}
```

If there are no saved threads, `resume_attempted` and `resume_succeeded` may be
false; the multi-client topology is proven, but a resume-capable environment
must rerun the probe before approving the full gateway implementation.

- [ ] **Step 2: Write the probe report**

Record:

- date and Codex version;
- the exact public JSON result;
- whether two initialized clients were accepted;
- whether the secondary resumed a thread;
- whether the primary remained usable;
- decision `PROCEED_WITH_PER_CLIENT_UPSTREAM` or
  `IMPLEMENT_EXPLICIT_PICKER_DEGRADATION`;
- no endpoint, thread/session ID, token, prompt, raw payload, or stderr.

- [ ] **Step 3: Self-review the report**

Run:

```powershell
rg -n -S "ws://|wss://|threadId|session_id|token|prompt|stderr" docs/superpowers/reports/2026-07-13-codex-remote-resume-probe.md
git diff --check
```

Expected: the report contains no runtime endpoint or secret-bearing data;
generic field names used to state the privacy rule are acceptable only when no
value follows them.

- [ ] **Step 4: Commit the verified report**

```powershell
git add docs/superpowers/reports/2026-07-13-codex-remote-resume-probe.md
git commit -m "docs: record codex remote resume probe"
```

### Task 4: Produce the result-specific implementation plan

**Files:**
- Create one of:
  - `docs/superpowers/plans/2026-07-13-codex-multi-client-gateway.md`
  - `docs/superpowers/plans/2026-07-13-codex-resume-picker-degradation.md`

- [ ] **Step 1: Select exactly one path from the committed report**

Choose the multi-client gateway plan only when the report proves two initialized
clients, secondary resume, and primary survival. Otherwise choose explicit
degradation. Do not combine both paths into one implementation commit.

- [ ] **Step 2: Write a complete TDD implementation plan**

The multi-client plan must cover per-connection upstream state, scoped
interaction IDs, ownership/rebinding behavior observed by the probe, cleanup,
and two-client regression tests. The degradation plan must cover CLI preflight,
actionable errors, documentation, and preservation of `--last`, explicit ID,
and `--no-lark` behavior.

- [ ] **Step 3: Review and commit the selected plan**

Run `git diff --check`, review every referenced path and test name, then commit:

```powershell
git add docs/superpowers/plans/2026-07-13-codex-multi-client-gateway.md
# Use the degradation plan path instead when that is the selected decision.
git commit -m "docs: plan codex remote resume fix"
```
