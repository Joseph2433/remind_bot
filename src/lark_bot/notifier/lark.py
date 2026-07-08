from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from lark_bot.models import NotificationRequest, ReceiveIdType
from lark_bot.redaction import redact_text

logger = logging.getLogger(__name__)


class LarkAPIError(RuntimeError):
    """Raised when the Lark OpenAPI returns an error."""


@dataclass
class CachedToken:
    value: str
    expires_at: float


def build_text_message(receive_id: str, text: str) -> dict[str, Any]:
    return {
        "receive_id": receive_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }


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
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.receive_id = receive_id
        self.receive_id_type = receive_id_type
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None
        self._cached_token: CachedToken | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def send(self, request: NotificationRequest) -> None:
        text = self.render_notification_text(request)
        self.send_text(text)

    def send_text(self, text: str) -> None:
        token = self.get_tenant_access_token()
        url = f"{self.base_url}/open-apis/im/v1/messages"
        payload = build_text_message(self.receive_id, text)
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
        task = request.task
        detection = request.detection
        lines = [
            f"Lark Bot: {detection.status.value}",
            f"Task: {task.name}",
            f"Source: {task.source}",
            f"Exit code: {task.exit_code}",
            f"Duration: {task.duration_seconds:.1f}s",
            f"Tags: {', '.join(detection.tags) if detection.tags else '-'}",
        ]
        tail = task.combined_tail_text.splitlines()[-tail_lines:]
        if tail:
            lines.append("")
            lines.append("Output tail:")
            lines.extend(tail)
        return redact_text("\n".join(lines))


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise LarkAPIError("Lark API returned non-JSON response") from exc
    if not isinstance(data, dict):
        raise LarkAPIError("Lark API returned unexpected JSON response")
    return data
