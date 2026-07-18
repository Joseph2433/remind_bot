from __future__ import annotations

import re
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

MARKDOWN_BODY_LIMIT = 4000

_FENCE_LINE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_INLINE_BACKTICK_RE = re.compile(r"`+")
_LARK_AT_TAG_RE = re.compile(r"</?at(?:\s+[^<>]*?)?\s*>", re.IGNORECASE)

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
    body_text = _escape_lark_at_tags(body_text)
    if message_format == "text":
        return text_message(body_text)
    markdown = _task_markdown(request, tail_lines=tail_lines)
    markdown = redact_text(markdown)
    markdown = _neutralize_lark_mentions(markdown)
    markdown = _truncate_markdown(markdown, MARKDOWN_BODY_LIMIT)
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
    summary = redact_text(str(item.payload_summary))
    plain = _outbox_plain_text(heading, summary, instruction)
    plain = _escape_lark_at_tags(plain)
    if message_format == "text":
        return text_message(plain)
    markdown = _outbox_markdown(summary, instruction)
    markdown = _neutralize_lark_mentions(markdown)
    markdown = _truncate_markdown(markdown, MARKDOWN_BODY_LIMIT)
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
        parts.extend(["", "### Output", "", "\n".join(tail)])
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


def _outbox_markdown(summary: str, instruction: str | None) -> str:
    parts: list[str] = []
    if summary:
        parts.append(summary)
    if instruction:
        if parts:
            parts.extend(["", "---", ""])
        parts.append(instruction)
    return "\n".join(parts)


def _neutralize_lark_mentions(markdown: str) -> str:
    parts: list[str] = []
    open_fence: tuple[str, int] | None = None
    for line in markdown.splitlines(keepends=True):
        fence = _fence_marker(line)
        if open_fence is not None:
            parts.append(line)
            if fence is not None and _closes_fence(line, fence, open_fence):
                open_fence = None
            continue
        if fence is not None:
            open_fence = fence
            parts.append(line)
            continue
        parts.append(_neutralize_inline_mentions(line))
    return "".join(parts)


def _neutralize_inline_mentions(text: str) -> str:
    matches = [
        match
        for match in _INLINE_BACKTICK_RE.finditer(text)
        if not _is_escaped(text, match.start())
    ]
    parts: list[str] = []
    cursor = 0
    match_index = 0
    while match_index < len(matches):
        opening = matches[match_index]
        closing_index = next(
            (
                index
                for index in range(match_index + 1, len(matches))
                if len(matches[index].group(0)) == len(opening.group(0))
            ),
            None,
        )
        if closing_index is None:
            break
        closing = matches[closing_index]
        parts.append(_escape_lark_at_tags(text[cursor : opening.start()]))
        parts.append(text[opening.start() : closing.end()])
        cursor = closing.end()
        match_index = closing_index + 1
    parts.append(_escape_lark_at_tags(text[cursor:]))
    return "".join(parts)


def _is_escaped(text: str, index: int) -> bool:
    backslash_count = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslash_count += 1
        cursor -= 1
    return backslash_count % 2 == 1


def _escape_lark_at_tags(text: str) -> str:
    return _LARK_AT_TAG_RE.sub(
        lambda match: match.group(0).replace("<", "&#60;").replace(">", "&#62;"),
        text,
    )


def _fence_marker(line: str) -> tuple[str, int] | None:
    match = _FENCE_LINE_RE.match(line.rstrip("\r\n"))
    if match is None:
        return None
    marker = match.group(1)
    return marker[0], len(marker)


def _closes_fence(
    line: str,
    fence: tuple[str, int],
    open_fence: tuple[str, int],
) -> bool:
    marker_match = _FENCE_LINE_RE.match(line.rstrip("\r\n"))
    if marker_match is None:
        return False
    return (
        fence[0] == open_fence[0]
        and fence[1] >= open_fence[1]
        and not marker_match.group(2).strip()
    )


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


def _truncate_markdown(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    ellipsis = "..."
    prefix = text[: limit - len(ellipsis)]
    open_fence = _unclosed_fence(prefix)
    if open_fence is None:
        return prefix + ellipsis
    closing_fence = "\n" + open_fence[0] * open_fence[1]
    content_limit = limit - len(ellipsis) - len(closing_fence)
    if content_limit <= 0:
        return text[:limit]
    prefix = text[:content_limit]
    open_fence = _unclosed_fence(prefix)
    if open_fence is None:
        return prefix + ellipsis
    closing_fence = "\n" + open_fence[0] * open_fence[1]
    return prefix + ellipsis + closing_fence


def _unclosed_fence(markdown: str) -> tuple[str, int] | None:
    open_fence: tuple[str, int] | None = None
    for line in markdown.splitlines(keepends=True):
        fence = _fence_marker(line)
        if open_fence is None:
            if fence is not None:
                open_fence = fence
            continue
        if fence is not None and _closes_fence(line, fence, open_fence):
            open_fence = None
    return open_fence
