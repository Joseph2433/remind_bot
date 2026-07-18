# 飞书透明 Markdown 渲染实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 移除飞书通知渲染层注入的外层代码围栏，使正文原有 Markdown 由 JSON 2.0 富文本组件直接渲染，同时防止意外飞书提及并保证长内容截断后围栏闭合。

**Architecture:** 保持现有 `RenderedMessage` 与 `interactive_card` 结构不变，只调整 `lark/render.py` 的正文预处理。outbox 和 task 共享“脱敏 → 中和非代码区域 `<at>` 标签 → Markdown 安全截断”的处理顺序，正文自身已有 fenced code block 原样保留。

**Tech Stack:** Python 3.11、pytest、飞书卡片 JSON 2.0、标准库 `re`

---

## 文件结构

- Modify: `src/lark_bot/lark/render.py` — 透明 Markdown 组合、安全提及处理、围栏感知截断。
- Modify: `tests/test_lark_render.py` — 渲染单元测试与安全边界测试。
- Modify: `tests/test_daemon_core.py` — daemon 完成通知的集成级正文断言。
- Reference: `docs/superpowers/specs/2026-07-18-transparent-markdown-render-design.md` — 已批准设计。

仓库禁止未经明确许可创建 commit，因此本计划执行时只保留工作树改动和测试结果，不运行 `git commit`。

### Task 1: 用失败测试锁定透明 Markdown 行为

**Files:**
- Modify: `tests/test_lark_render.py`
- Modify: `tests/test_daemon_core.py`

- [x] **Step 1: 新增 outbox Markdown 透明传递测试**

构造包含标题、列表、表格和内部 Python fenced code block 的 `payload_summary`，断言正文：

```python
def test_outbox_card_preserves_markdown_without_outer_code_fence():
    summary = (
        "# 结论\n\n"
        "- 已完成\n\n"
        "| 项目 | 状态 |\n| --- | --- |\n| 测试 | 通过 |\n\n"
        "```python\nprint('ok')\n```"
    )
    item = type(
        "Item",
        (),
        {
            "notification_type": "orchestrator:turn_completed",
            "payload_summary": summary,
            "interaction_id": None,
        },
    )()

    rendered = render_outbox_notification(item, message_format="card")
    body = rendered.content["body"]["elements"][0]["content"]

    assert body == summary
    assert body.count("```") == 2
    assert "**Codex 本轮已完成**" not in body
```

- [x] **Step 2: 新增 task 输出不加围栏测试**

```python
def test_task_card_preserves_output_markdown_without_outer_code_fence():
    request = _request(stdout_tail=["## 结果", "", "```python", "print('ok')", "```"])

    rendered = render_task_notification(request, message_format="card", tail_lines=10)
    body = rendered.content["body"]["elements"][0]["content"]

    assert "### Output\n\n## 结果" in body
    assert body.count("```") == 2
```

- [x] **Step 3: 更新 daemon 标题去重断言**

在 `test_runtime_renders_interactive_turn_notifications_in_chinese` 中断言 Header 保持中文状态标题，同时正文等于摘要且不包含重复状态标题。

- [x] **Step 4: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/test_lark_render.py tests/test_daemon_core.py -q
```

Expected: 新增测试因正文存在外层三反引号或重复标题而失败，已有脱敏与 Header 测试继续通过。

### Task 2: 实现透明 Markdown 组合

**Files:**
- Modify: `src/lark_bot/lark/render.py`
- Test: `tests/test_lark_render.py`
- Test: `tests/test_daemon_core.py`

- [x] **Step 1: 移除 outbox 500 字符二次截断**

删除 `OUTBOX_SUMMARY_LIMIT`，将：

```python
summary = redact_text(str(item.payload_summary))[:OUTBOX_SUMMARY_LIMIT]
```

改为：

```python
summary = redact_text(str(item.payload_summary))
```

- [x] **Step 2: 重写 outbox Markdown 组合函数**

将 `_outbox_markdown` 改为不接收 `heading`，正文只包含摘要与可选说明：

```python
def _outbox_markdown(summary: str, instruction: str | None) -> str:
    parts: list[str] = []
    if summary:
        parts.append(summary)
    if instruction:
        if parts:
            parts.extend(["", "---", ""])
        parts.append(instruction)
    return "\n".join(parts)
```

调用点改为：

```python
markdown = _outbox_markdown(summary, instruction)
```

- [x] **Step 3: 移除 task 输出外层围栏**

将 `_task_markdown` 的输出尾部组合改为：

```python
if tail:
    parts.extend(["", "### Output", "", "\n".join(tail)])
```

- [x] **Step 4: 运行目标测试**

Run:

```powershell
python -m pytest tests/test_lark_render.py tests/test_daemon_core.py -q
```

Expected: 透明 Markdown 与标题去重测试通过；安全标签和截断测试尚未添加。

### Task 3: 中和非代码区域的飞书提及标签

**Files:**
- Modify: `src/lark_bot/lark/render.py`
- Modify: `tests/test_lark_render.py`

- [x] **Step 1: 新增失败测试**

覆盖三个场景：普通正文中的 `<at id=all></at>` 被转义；fenced code block 内保持原样；行内代码中的标签保持原样。

```python
def test_outbox_card_neutralizes_lark_mentions_outside_code():
    summary = (
        "通知 <at id=all></at>\n\n"
        "`<at id=all></at>`\n\n"
        "```xml\n<at id=all></at>\n```"
    )
    item = type(
        "Item",
        (),
        {
            "notification_type": "orchestrator:turn_completed",
            "payload_summary": summary,
            "interaction_id": None,
        },
    )()

    rendered = render_outbox_notification(item, message_format="card")
    body = rendered.content["body"]["elements"][0]["content"]

    assert "通知 &#60;at id=all&#62;&#60;/at&#62;" in body
    assert "`<at id=all></at>`" in body
    assert "```xml\n<at id=all></at>\n```" in body
```

- [x] **Step 2: 运行单测并确认失败**

Run:

```powershell
python -m pytest tests/test_lark_render.py::test_outbox_card_neutralizes_lark_mentions_outside_code -q
```

Expected: FAIL，普通正文标签尚未转义。

- [x] **Step 3: 实现围栏与行内代码感知的提及中和函数**

在 `render.py` 中新增 `_neutralize_lark_mentions(markdown: str) -> str`：

- 逐行识别最多三个前导空格后的三反引号或三波浪线围栏。
- fenced code block 内的行原样保留。
- 非 fenced 行按匹配的反引号 run 识别行内代码。
- 仅在普通文本片段中用正则匹配 `</?at ...>`，将 `<`、`>` 分别替换为 `&#60;`、`&#62;`。

渲染调用顺序调整为：

```python
markdown = redact_text(markdown)
markdown = _neutralize_lark_mentions(markdown)
markdown = _truncate_markdown(markdown, MARKDOWN_BODY_LIMIT)
```

- [x] **Step 4: 运行相关测试**

Run:

```powershell
python -m pytest tests/test_lark_render.py -q
```

Expected: PASS。

### Task 4: 实现 fenced code block 感知的截断

**Files:**
- Modify: `src/lark_bot/lark/render.py`
- Modify: `tests/test_lark_render.py`

- [x] **Step 1: 新增失败测试**

直接测试渲染后的长摘要，断言总长度不超过 `MARKDOWN_BODY_LIMIT`，正文以匹配的闭合围栏结束：

```python
def test_outbox_card_closes_code_fence_when_markdown_is_truncated():
    summary = "```python\n" + ("print('long')\n" * 400)
    item = type(
        "Item",
        (),
        {
            "notification_type": "orchestrator:turn_completed",
            "payload_summary": summary,
            "interaction_id": None,
        },
    )()

    rendered = render_outbox_notification(item, message_format="card")
    body = rendered.content["body"]["elements"][0]["content"]

    assert len(body) <= 4000
    assert body.endswith("...\n```")
```

- [x] **Step 2: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/test_lark_render.py::test_outbox_card_closes_code_fence_when_markdown_is_truncated -q
```

Expected: FAIL，现有 `_truncate` 留下未闭合围栏。

- [x] **Step 3: 实现 `_truncate_markdown`**

新增围栏状态扫描辅助函数；未超限时原样返回，普通文本超限时追加 `...`，开放围栏内超限时为省略号预留空间并追加相同字符和长度的闭合围栏。用 `_truncate_markdown` 替换 card 路径中的 `_truncate` 调用；保留 `_truncate` 仅在仍有其他调用时使用，否则删除。

- [x] **Step 4: 运行渲染测试**

Run:

```powershell
python -m pytest tests/test_lark_render.py -q
```

Expected: PASS。

### Task 5: 完整回归与差异审查

**Files:**
- Verify: `src/lark_bot/lark/render.py`
- Verify: `tests/test_lark_render.py`
- Verify: `tests/test_daemon_core.py`
- Verify: `docs/superpowers/specs/2026-07-18-transparent-markdown-render-design.md`
- Verify: `docs/superpowers/plans/2026-07-18-transparent-markdown-render.md`

- [x] **Step 1: 运行目标测试**

```powershell
python -m pytest tests/test_lark_render.py tests/test_daemon_core.py -q
```

Expected: PASS。

- [x] **Step 2: 运行完整测试**

```powershell
python -m pytest
```

Expected: 全部测试通过，无真实网络访问。

- [x] **Step 3: 检查工作树和差异**

```powershell
git status --short
git diff --check
git diff -- src/lark_bot/lark/render.py tests/test_lark_render.py tests/test_daemon_core.py docs/superpowers/specs/2026-07-18-transparent-markdown-render-design.md docs/superpowers/plans/2026-07-18-transparent-markdown-render.md
```

Expected: 仅出现计划内文件；`git diff --check` 无输出。不要创建 commit。

### Task 6: 修复独立审查发现的提及绕过

**Files:**
- Modify: `src/lark_bot/lark/render.py`
- Modify: `tests/test_lark_render.py`

- [x] **Step 1: 添加 text 回退提及测试**

分别覆盖 task 与 outbox 的 `message_format="text"`，确认 `<at id=all></at>` 被转义。

- [x] **Step 2: 添加转义反引号回归测试**

确认被反斜杠转义的反引号属于 Markdown 字面量，不会让其中的 `<at>` 被误判为行内代码内容。

- [x] **Step 3: 实现最小修复**

text 返回前调用 `_escape_lark_at_tags`；行内代码扫描使用 `_is_escaped` 排除奇数个反斜杠转义的反引号 run。

- [x] **Step 4: 重新验证并复审**

目标测试通过 23 项，完整测试通过 302 项；独立复审未发现剩余 Critical 或 Important 问题。
