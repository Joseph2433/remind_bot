from __future__ import annotations

import asyncio
import multiprocessing
import queue
from collections.abc import Callable, Mapping
from dataclasses import asdict
from typing import Any

from lark_bot.lark.events import (
    LarkControlEvent,
    LarkMessageEvent,
    LarkReactionEvent,
    normalize_message_event,
    normalize_reaction_event,
)


def _safe_child_put(output_queue: Any, event: LarkControlEvent) -> None:
    payload = {
        "type": "reaction" if isinstance(event, LarkReactionEvent) else "message",
        **asdict(event),
    }
    output_queue.put_nowait(payload)


def _lark_ws_worker(app_id: str, app_secret: str, output_queue: Any) -> None:
    import lark_oapi as lark

    def on_message(event: object) -> None:
        try:
            _safe_child_put(output_queue, normalize_message_event(event))
        except (ValueError, queue.Full):
            return

    def on_reaction(event: object) -> None:
        try:
            _safe_child_put(output_queue, normalize_reaction_event(event))
        except (ValueError, queue.Full):
            return

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_im_message_reaction_created_v1(on_reaction)
        .build()
    )
    lark.ws.Client(app_id, app_secret, event_handler=handler).start()


def decode_child_event(payload: object) -> LarkControlEvent:
    if not isinstance(payload, Mapping):
        raise ValueError("malformed child event")
    kind = payload.get("type")
    cls: type[LarkReactionEvent] | type[LarkMessageEvent]
    cls = (
        LarkReactionEvent
        if kind == "reaction"
        else LarkMessageEvent
        if kind == "message"
        else None  # type: ignore[assignment]
    )
    if cls is None:
        raise ValueError("unknown child event type")
    try:
        return cls(**{key: value for key, value in payload.items() if key != "type"})
    except TypeError as error:
        raise ValueError("malformed child event") from error


class LarkLongConnection:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        process_factory: Callable[..., Any] | None = None,
        queue_factory: Callable[[int], Any] | None = None,
        queue_capacity: int = 100,
    ) -> None:
        if queue_capacity <= 0:
            raise ValueError("queue_capacity must be positive")
        context = multiprocessing.get_context("spawn")
        self._app_id = app_id
        self._app_secret = app_secret
        self._process_factory = process_factory or context.Process
        self._queue_factory = queue_factory or (
            lambda size: context.Queue(maxsize=size)
        )
        self._capacity = queue_capacity
        self.events: asyncio.Queue[LarkControlEvent] = asyncio.Queue(
            maxsize=queue_capacity
        )
        self.terminal_error: BaseException | None = None
        self._child_queue: Any = None
        self._process: Any = None
        self._pump_task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        if self._pump_task is not None:
            return
        if self._closed:
            raise RuntimeError("LarkLongConnection is closed")
        self._child_queue = self._queue_factory(self._capacity)
        self._process = self._process_factory(
            target=_lark_ws_worker,
            args=(self._app_id, self._app_secret, self._child_queue),
            daemon=True,
        )
        self._process.start()
        self._pump_task = asyncio.create_task(
            self._pump(), name="lark-long-connection-pump"
        )

    async def _pump(self) -> None:
        try:
            while True:
                try:
                    payload = await asyncio.to_thread(
                        self._child_queue.get, True, 0.1
                    )
                except queue.Empty:
                    if self._process is not None and not self._process.is_alive():
                        raise RuntimeError("Lark long-connection child stopped")
                    continue
                event = decode_child_event(payload)
                self.events.put_nowait(event)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            self.terminal_error = error

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is not None:
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, 1.0)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                await asyncio.to_thread(process.join, 1.0)
        if self._pump_task is not None:
            self._pump_task.cancel()
            await asyncio.gather(self._pump_task, return_exceptions=True)
        if self._child_queue is not None:
            close = getattr(self._child_queue, "close", None)
            if close is not None:
                close()

    async def __aenter__(self) -> LarkLongConnection:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()
