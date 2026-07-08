from __future__ import annotations


class RedisNotificationStore:
    """Reserved Redis backend interface for a later release."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise NotImplementedError("Redis storage is reserved for a later release.")
