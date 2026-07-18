"""Wire and process protocol types for the Codex app-server."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ServerRequest:
    request_id: int | str
    method: str
    params: dict[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ServerNotification:
    method: str
    params: dict[str, Any] = field(repr=False)


class _Reader(Protocol):
    async def readline(self) -> bytes: ...

    async def read(self, size: int = -1) -> bytes: ...


class _Writer(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...


class _Process(Protocol):
    stdin: _Writer
    stdout: _Reader
    stderr: _Reader
    returncode: int | None

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...
