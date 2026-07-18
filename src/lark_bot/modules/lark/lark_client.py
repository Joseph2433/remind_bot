from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from lark_bot.modules.lark.lark_message import (
    MessageFormat,
    RenderedMessage,
    build_api_payload,
    build_text_message,
    text_message,
)
from lark_bot.modules.lark.lark_render import render_notification_text, render_task_notification
from lark_bot.models import ReceiveIdType
from lark_bot.modules.notification.notification_model import NotificationRequest

logger = logging.getLogger(__name__)

# Re-export for existing tests and callers.
__all__ = [
    "LarkAPIError",
    "LarkBotClient",
    "build_text_message",
    "render_notification_text",
]


class LarkAPIError(RuntimeError):
    """Raised when the Lark OpenAPI returns an error."""


@dataclass
class CachedToken:
    value: str
    expires_at: float


class LarkBotClient:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        receive_id: str,
        receive_id_type: ReceiveIdType = "chat_id",
        base_url: str = "https://open.feishu.cn",
        timeout_seconds: float = 10.0,
        client: httpx.Client | None = None,
        message_format: MessageFormat = "card",
        output_tail_lines: int = 40,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.receive_id = receive_id
        self.receive_id_type = receive_id_type
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.message_format = message_format
        self.output_tail_lines = output_tail_lines
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None
        self._cached_token: CachedToken | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def send(self, request: NotificationRequest) -> str:
        rendered = render_task_notification(
            request,
            message_format=self.message_format,
            tail_lines=self.output_tail_lines,
        )
        return self.send_rendered(rendered)

    def send_text(self, text: str) -> str:
        return self.send_rendered(text_message(text))

    def send_rendered(self, message: RenderedMessage) -> str:
        token = self.get_tenant_access_token()
        url = f"{self.base_url}/open-apis/im/v1/messages"
        payload = build_api_payload(self.receive_id, message)
        try:
            response = self._client.post(
                url,
                params={"receive_id_type": self.receive_id_type},
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            logger.warning("Failed to send Lark message: %s", exc)
            raise LarkAPIError("Failed to send Lark message") from exc

        data = _safe_json(response)
        if response.status_code >= 400 or data.get("code", 0) != 0:
            logger.warning(
                "Lark message API failed: status=%s code=%s msg=%s",
                response.status_code,
                data.get("code"),
                data.get("msg"),
            )
            raise LarkAPIError("Lark message API failed")
        response_data = data.get("data")
        message_id = response_data.get("message_id") if isinstance(response_data, dict) else None
        if not isinstance(message_id, str) or not message_id:
            raise LarkAPIError("Lark message API response missing message_id")
        return message_id

    def get_tenant_access_token(self) -> str:
        now = time.time()
        if self._cached_token and self._cached_token.expires_at - now > 60:
            return self._cached_token.value

        url = f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal"
        try:
            response = self._client.post(
                url,
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            logger.warning("Failed to refresh Lark tenant access token: %s", exc)
            raise LarkAPIError("Failed to refresh Lark tenant access token") from exc

        data = _safe_json(response)
        token = data.get("tenant_access_token")
        if response.status_code >= 400 or data.get("code", 0) != 0 or not token:
            logger.warning(
                "Lark token API failed: status=%s code=%s msg=%s",
                response.status_code,
                data.get("code"),
                data.get("msg"),
            )
            raise LarkAPIError("Lark token API failed")

        expire_seconds = int(data.get("expire", 7200))
        self._cached_token = CachedToken(
            value=token,
            expires_at=now + max(expire_seconds - 60, 60),
        )
        return token

    @staticmethod
    def render_notification_text(request: NotificationRequest, tail_lines: int = 40) -> str:
        return render_notification_text(request, tail_lines=tail_lines)


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise LarkAPIError("Lark API returned non-JSON response") from exc
    if not isinstance(data, dict):
        raise LarkAPIError("Lark API returned unexpected JSON response")
    return data
