"""Lazy bridge for the optional Claude Agent SDK.

The rest of the application talks to the small internal contract in this
module.  The optional third-party SDK is imported only when a managed client
is requested, so importing :mod:`lark_bot.cli` remains safe on installations
that do not include the SDK.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Protocol, TypeAlias, runtime_checkable

JsonValue: TypeAlias = (
    None
    | bool
    | int
    | float
    | str
    | list["JsonValue"]
    | dict[str, "JsonValue"]
)

ClaudeToolContext: TypeAlias = Mapping[str, JsonValue] | None
CanUseToolCallback: TypeAlias = Callable[
    [str, Mapping[str, JsonValue], ClaudeToolContext], Awaitable["ClaudePermissionResult"]
]


@dataclass(frozen=True)
class ClaudePermissionResult:
    allowed: bool
    updated_input: Mapping[str, JsonValue] | None = None
    message: str | None = None


@dataclass(frozen=True)
class ClaudeSdkOptions:
    cwd: str
    model: str | None
    permission_mode: str | None
    resume: str | None
    session_id: str
    can_use_tool: CanUseToolCallback


@dataclass(frozen=True)
class ClaudeSdkResult:
    session_id: str
    subtype: str
    is_error: bool
    duration_ms: int
    result: str | None
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClaudeSdkOtherMessage:
    """Normalized non-result SDK message with no third-party object fields."""

    kind: str
    payload: Mapping[str, JsonValue]


ClaudeSdkMessage: TypeAlias = ClaudeSdkResult | ClaudeSdkOtherMessage


@runtime_checkable
class ClaudeSdkClient(Protocol):
    async def connect(self) -> None: ...

    async def query(self, prompt: str) -> None: ...

    def receive_response(self) -> AsyncIterator[ClaudeSdkMessage]: ...

    async def interrupt(self) -> None: ...

    async def close(self) -> None: ...


@runtime_checkable
class ClaudeSdkClientFactory(Protocol):
    def __call__(self, options: ClaudeSdkOptions) -> ClaudeSdkClient: ...


def _json_value(value: Any) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    return str(value)


def _safe_mapping(value: Any) -> Mapping[str, JsonValue]:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, Mapping):
        return {str(key): _json_value(item) for key, item in attributes.items() if not str(key).startswith("_")}
    return {}


def _safe_context(value: Any) -> ClaudeToolContext:
    return None if value is None else _safe_mapping(value)


def _safe_errors(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        return (str(value),)
    try:
        return tuple(str(item) for item in value)
    except TypeError:
        return (str(value),)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class _ClaudeSdkClient:
    def __init__(
        self,
        sdk_client: Any,
        *,
        session_id: str,
        result_type: type[Any] | tuple[type[Any], ...] | None,
    ) -> None:
        self._sdk_client = sdk_client
        self._session_id = session_id
        self._result_type = result_type

    async def connect(self) -> None:
        method = getattr(self._sdk_client, "connect")
        await _maybe_await(method())

    async def query(self, prompt: str) -> None:
        await _maybe_await(getattr(self._sdk_client, "query")(prompt))

    async def interrupt(self) -> None:
        await _maybe_await(getattr(self._sdk_client, "interrupt")())

    async def close(self) -> None:
        method = getattr(self._sdk_client, "disconnect", None)
        if method is None:
            method = getattr(self._sdk_client, "close", None)
        if method is not None:
            await _maybe_await(method())

    def receive_response(self) -> AsyncIterator[ClaudeSdkMessage]:
        return self._receive_response()

    async def _receive_response(self) -> AsyncIterator[ClaudeSdkMessage]:
        stream = getattr(self._sdk_client, "receive_response")()
        stream = await _maybe_await(stream)
        async for message in stream:
            yield self._normalize_message(message)

    def _normalize_message(self, message: Any) -> ClaudeSdkMessage:
        if self._is_result(message):
            return ClaudeSdkResult(
                session_id=_optional_str(getattr(message, "session_id", None)) or self._session_id,
                subtype=_optional_str(getattr(message, "subtype", None)) or "unknown",
                is_error=bool(getattr(message, "is_error", False)),
                duration_ms=_optional_int(getattr(message, "duration_ms", None)) or 0,
                result=_safe_result(getattr(message, "result", None)),
                errors=_safe_errors(getattr(message, "errors", None)),
            )
        payload = _safe_mapping(message)
        return ClaudeSdkOtherMessage(kind=type(message).__name__, payload=payload)

    def _is_result(self, message: Any) -> bool:
        if self._result_type is not None:
            try:
                if isinstance(message, self._result_type):
                    return True
            except TypeError:
                pass
        return all(hasattr(message, name) for name in ("result", "is_error"))


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_result(value: Any) -> str | None:
    return None if value is None else str(value)


class ClaudeAgentSdkBridge:
    """Build a normalized client while importing the optional SDK lazily."""

    def __init__(self, importer: Callable[[str], ModuleType] | None = None) -> None:
        self._importer = importer or importlib.import_module

    def __call__(self, options: ClaudeSdkOptions) -> ClaudeSdkClient:
        try:
            sdk = self._importer("claude_agent_sdk")
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                "claude_agent_sdk is required to create a managed Claude client; "
                "install the optional 'claude-agent-sdk' dependency."
            ) from exc

        sdk_client_type = getattr(sdk, "ClaudeSDKClient")
        sdk_options_type = getattr(sdk, "ClaudeAgentOptions")
        permission_allow = getattr(sdk, "PermissionResultAllow")
        permission_deny = getattr(sdk, "PermissionResultDeny")
        result_type = getattr(sdk, "ResultMessage", None)

        sdk_callback = self._wrap_callback(options.can_use_tool, permission_allow, permission_deny)
        sdk_options = sdk_options_type(
            cwd=options.cwd,
            model=options.model,
            permission_mode=options.permission_mode,
            resume=options.resume,
            can_use_tool=sdk_callback,
        )
        sdk_client = sdk_client_type(options=sdk_options)
        client = _ClaudeSdkClient(
            sdk_client,
            session_id=options.session_id,
            result_type=result_type,
        )
        return client

    @staticmethod
    def _wrap_callback(
        callback: CanUseToolCallback,
        allow_type: type[Any],
        deny_type: type[Any],
    ) -> Callable[..., Awaitable[Any]] | None:
        async def wrapped(tool_name: str, input_data: Any, context: Any) -> Any:
            result = await callback(tool_name, _safe_mapping(input_data), _safe_context(context))
            if result.allowed:
                updated_input = None
                if result.updated_input is not None:
                    updated_input = dict(_safe_mapping(result.updated_input))
                return allow_type(updated_input=updated_input)
            return deny_type(message=result.message or "Permission denied")

        return wrapped


__all__ = [
    "CanUseToolCallback",
    "ClaudeAgentSdkBridge",
    "ClaudePermissionResult",
    "ClaudeSdkClient",
    "ClaudeSdkClientFactory",
    "ClaudeSdkMessage",
    "ClaudeSdkOptions",
    "ClaudeSdkOtherMessage",
    "ClaudeSdkResult",
    "ClaudeToolContext",
    "JsonValue",
]
