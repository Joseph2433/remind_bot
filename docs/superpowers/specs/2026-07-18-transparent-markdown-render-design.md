# 飞书透明 Markdown 渲染设计

## 目标

让飞书卡片的富文本组件直接渲染通知正文携带的 Markdown，不再由渲染层统一添加外层代码围栏。正文自身包含的标题、列表、表格、引用、行内代码和 fenced code block 由飞书 JSON 2.0 Markdown 组件按原语义渲染。

## 当前问题

`src/lark_bot/lark/render.py` 的 `_outbox_markdown` 将完整 `payload_summary` 包在三反引号中，`_task_markdown` 也将完整输出尾部包在三反引号中。这会把 Markdown 源文本显示成等宽代码块，导致标题、列表、表格等语法失效；同时 outbox 正文重复显示卡片 Header 已经包含的通知标题。

## 设计原则

1. 渲染器透明传递 Markdown，不猜测内容类型，不自动添加代码围栏。
2. 正文自身已有的 fenced code block（例如 ` ```python `）原样保留，由飞书负责渲染。
3. 卡片 Header 是通知状态的唯一标题；正文不重复状态标题。
4. 审批说明、输入说明和任务输出均使用同一透明规则。
5. 脱敏发生在 Markdown 渲染之前，保持现有 `[REDACTED]` 行为。
6. 防止正文中的飞书 `<at ...></at>` 标签触发意外提及；代码围栏和行内代码中的示例标签保持原样。
7. 移除 outbox 的 500 字符二次截断，沿用上游 2000 字符摘要限制和卡片正文 4000 字符保护上限。

## 数据流

```text
payload_summary / task tail
  → redact_text
  → neutralize_lark_mentions（仅非代码 Markdown 区域）
  → 组合说明文字（不添加代码围栏）
  → Markdown 安全截断
  → interactive_card(tag=markdown)
```

## 渲染规则

### Outbox 通知

- `summary` 非空时直接作为正文。
- `instruction` 非空时在正文后追加分割线和说明文字。
- 不在正文重复 `heading`。
- 空摘要但存在说明文字时只显示说明文字。

### Task 通知

- 保留任务元数据字段。
- 保留 `### Output` 小标题。
- 输出尾部直接追加，不添加外层代码围栏。
- 输出自身携带的 fenced code block 保持不变。

## 安全处理

卡片格式仅在 fenced code block 和真正的行内代码之外，将飞书 `<at ...>` 与 `</at>` 标签的尖括号转义为 HTML 实体；被反斜杠转义的反引号不视为代码边界。纯文本回退格式中统一转义 `<at>` 标签。这样可避免机器人输出触发 `@指定人` 或 `@所有人`，同时不改变普通 Markdown 链接、标题、列表、表格或代码示例。

## 截断策略

继续限制单个 Markdown 组件最大 4000 字符。若截断点位于 fenced code block 内，截断函数为正文补充省略标记和匹配的闭合围栏，避免整张卡片后续内容被错误解释为代码块。

## 影响文件

- `src/lark_bot/lark/render.py`：透明渲染、提及标签中和、Markdown 安全截断。
- `tests/test_lark_render.py`：新增渲染语义、安全与截断测试。
- `tests/test_daemon_core.py`：更新 daemon 卡片正文断言，确认标题不重复。

## 非目标

- 不改变卡片 schema、Header 颜色或消息发送接口。
- 不改变 text 格式回退。
- 不引入完整 Markdown AST 依赖。
- 不改变 outbox、interaction 或 session 数据库结构。

## 验收标准

1. Codex 标题、列表、表格直接保留在卡片 Markdown 正文中。
2. 正文已有的 Python fenced code block 原样保留，且不存在渲染器添加的外层围栏。
3. 卡片 Header 标题不在正文重复。
4. task 输出尾部不再统一显示为代码块。
5. 敏感信息仍被脱敏。
6. 非代码区域的飞书 `<at>` 标签不会触发提及，代码示例中的标签不被改写。
7. 长 Markdown 截断后不会留下未闭合 fenced code block。
8. 目标测试与完整 `python -m pytest` 通过。
