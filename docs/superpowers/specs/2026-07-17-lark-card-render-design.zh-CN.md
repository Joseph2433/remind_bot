# 飞书 Interactive 卡片渲染设计

## 目标

将出站通知从纯 `msg_type: text` 升级为飞书 **interactive 卡片（schema 2.0）**，在手机端渲染 Markdown 与状态色标题。保留 `text` 作为配置回退。不改动 HITL 关联逻辑。

## 非目标

- 卡片按钮 approve/deny
- multi-bot / Claude 适配
- card 失败自动降级 text
- outbox / interaction 表结构变更
- 入站仍仅处理用户 text 回复

## 约束

- 渲染前脱敏（`redact_text`）
- 保留返回的 `message_id`，供 `attach_lark_message_id` 与 reaction/reply 路由
- 同时覆盖 CLI `NotificationRequest` 与 daemon outbox
- 默认 `card`；`LARK_BOT_MESSAGE_FORMAT=text` 强制纯文本
- 单测不访问真实网络

## 架构

```text
Event / Outbox item
  → render_*（脱敏 + 结构化）
  → RenderedMessage(msg_type, content)
  → LarkBotClient.send_rendered
  → message_id
```

### 模块

| 模块 | 职责 |
|------|------|
| `lark/messages.py` | 纯 payload 构造 |
| `lark/render.py` | task / outbox 渲染 |
| `lark/client.py` | HTTP 发送 |
| `server/daemon/app.py` | outbox 调 `send_rendered` |
| `config.py` | `message_format` |

### Header 颜色

| 场景 | template |
|------|----------|
| 成功 | `green` |
| 失败 / 中断 / 降级 | `red` |
| 等待 / 审批 / 输入 | `orange` |
| 信息 / 启动 / hook | `blue` |

### HITL

不变：仍按 `lark_message_id` 关联表情与回复。卡片按钮后置。

## 验收

1. CLI 默认发 interactive
2. Daemon outbox 默认发 interactive
3. `MESSAGE_FORMAT=text` 恢复纯文本
4. 敏感信息仍脱敏
5. pending interaction 仍绑定 `message_id`
6. 单测无离线通过

英文版：`docs/superpowers/specs/2026-07-17-lark-card-render-design.md`
