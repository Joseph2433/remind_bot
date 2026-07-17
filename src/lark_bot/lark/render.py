from __future__ import annotations

from typing import Any

from lark_bot.codex.models import InteractionKind
from lark_bot.lark.messages import (
    HeaderTemplate,
    MessageFormat,
    RenderedMessage,
    interactive_card,
    text_message,
)
from lark_bot.models import NotificationRequest, TaskStatus
from lark_bot.redaction import redact_text

OUTBOX_SUMMARY_LIMIT = 500
MARKDOWN_BODY_LIMIT = 4000

_OUTBOX_HEADINGS = {
    "orchestrator:session_completed": "Codex 会话已完成",
    "orchestrator:session_interrupted": "Codex 会话已中断",
    "orchestrator:turn_completed": "Codex 本轮已完成",
    "orchestrator:turn_interrupted": "Codex 本轮已中断",
}


def render_task_notification(
    request: NotificationRequest,
    *,
    message_format: MessageFormat = "card",
    tail_lines: int = 40,
) -> RenderedMessage:
    title = f"Lark Bot: {request.detection.status.value}"
    body_text = _task_body_text(request, tail_lines=tail_lines)
    body_text = redact_text(body_text)
    if message_format == "text":
        return text_message(body_text)
    markdown = _task_markdown(request, tail_lines=tail_lines)
    markdown = redact_text(markdown)
    markdown = _truncate(markdown, MARKDOWN_BODY_LIMIT)
    return interactive_card(
        title=title,
        markdown=markdown,
        template=_status_template(request.detection.status),
    )


def render_outbox_notification(
    item: Any,
    *,
    message_format: MessageFormat = "card",
    interaction: Any | None = None,
) -> RenderedMessage:
    heading, instruction = _outbox_heading_and_instruction(item, interaction)
    summary = redact_text(str(item.payload_summary))[:OUTBOX_SUMMARY_LIMIT]
    plain = _outbox_plain_text(heading, summary, instruction)
    if message_format == "text":
        return text_message(plain)
    markdown = _outbox_markdown(heading, summary, instruction)
    markdown = _truncate(markdown, MARKDOWN_BODY_LIMIT)
    return interactive_card(
        title=heading,
        markdown=markdown,
        template=_outbox_template(str(item.notification_type), interaction),
    )


def render_notification_text(request: NotificationRequest, tail_lines: int = 40) -> str:
    """Backward-compatible plain-text render used by older call sites/tests."""

    return redact_text(_task_body_text(request, tail_lines=tail_lines))


def _task_body_text(request: NotificationRequest, *, tail_lines: int) -> str:
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
    return "\n".join(lines)


def _task_markdown(request: NotificationRequest, *, tail_lines: int) -> str:
    task = request.task
    detection = request.detection
    tags = ", ".join(detection.tags) if detection.tags else "-"
    parts = [
        f"**Task:** {task.name}",
        f"**Source:** {task.source}",
        f"**Exit:** {task.exit_code} · **Duration:** {task.duration_seconds:.1f}s",
        f"**Tags:** {tags}",
    ]
    tail = task.combined_tail_text.splitlines()[-tail_lines:]
    if tail:
        fenced = "\n".join(tail)
        parts.extend(["", "### Output", "```", fenced, "```"])
    return "\n".join(parts)


def _outbox_heading_and_instruction(
    item: Any, interaction: Any | None
) -> tuple[str, str | None]:
    notification_type = str(item.notification_type)
    heading = _OUTBOX_HEADINGS.get(
        notification_type,
        notification_type.replace("orchestrator:", "Codex ").replace("_", " "),
    )
    instruction: str | None = None
    if notification_type.endswith("interaction_requested"):
        if interaction is not None and getattr(interaction, "kind", None) is InteractionKind.USER_INPUT:
            heading = "Codex 请求输入"
            instruction = "请回复本消息并 @机器人。若有多个问题，请每行使用 `1: 回答` 的格式。"
        else:
            heading = "Codex 请求审批"
            instruction = (
                "请长按本消息并选择“回复”：输入 yes 或 y 表示允许，"
                "输入 no 或 n 表示拒绝。也可使用 👍 / 👎。"
            )
    return heading, instruction


def _outbox_plain_text(heading: str, summary: str, instruction: str | None) -> str:
    if instruction:
        return f"{heading}\n{summary}\n{instruction}"
    return f"{heading}\n{summary}"


def _outbox_markdown(heading: str, summary: str, instruction: str | None) -> str:
    parts = [f"**{heading}**", "", "```", summary, "```"]
    if instruction:
        parts.extend(["", instruction])
    return "\n".join(parts)


def _status_template(status: TaskStatus) -> HeaderTemplate:
    if status is TaskStatus.SUCCEEDED:
        return "green"
    if status is TaskStatus.FAILED:
        return "red"
    return "orange"


def _outbox_template(notification_type: str, interaction: Any | None) -> HeaderTemplate:
    lowered = notification_type.casefold()
    if lowered.endswith("interaction_requested"):
        return "orange"
    if any(token in lowered for token in ("failed", "interrupted", "degraded", "error")):
        return "red"
    if any(token in lowered for token in ("completed", "succeeded", "resolved")):
        return "green"
    if interaction is not None and getattr(interaction, "kind", None) is InteractionKind.USER_INPUT:
        return "orange"
    return "blue"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."
