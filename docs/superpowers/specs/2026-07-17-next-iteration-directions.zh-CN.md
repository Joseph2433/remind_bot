# 下一版本迭代方向（2026-07-17）

记录并评估 global-daemon-home 之后的四个产品方向。本文是规划备忘，不是实现计划。

## 当前基线（相关约束）

- 单一 Lark 应用 / 单一接收目标：`LARK_BOT_LARK_APP_ID` + 单个 `LARK_BOT_LARK_RECEIVE_ID`。
- 出站通知为纯文本 `msg_type: text`（`src/lark_bot/lark/client.py` → `build_text_message`）。
- Codex 已有完整路径：hooks 通知片段、事件适配器、app-server 编排器、审批/输入闭环、机器全局 `LARK_BOT_HOME` 下的 SQLite session 存储。
- 已有通用 agent 入口（`POST /agent/events` + `AgentEvent`）；Codex 另有专用适配器（`notifications/adapters/codex.py`）和 `codex/` 下的托管控制面。
- 产品定位仍是 **companion（陪伴侧车）**：监视、摘要、脱敏、通知；对托管 Codex 再做人机协同，而不是独立写代码的 agent。

---

## 方向 1 — Claude Code 适配（对标 Codex）

### 意图

按 Codex 的适配方式，为 Claude Code 做一等公民支持：事件 schema、状态归一、tags/source、可选 hook/安装路径；若 Claude Code 日后有稳定控制面，再考虑 HITL。

### 与现有代码的对应

| Codex 现状 | Claude 侧建议 |
|------------|---------------|
| `notifications/adapters/codex.py` | `notifications/adapters/claude.py`（或 `claude_code.py`） |
| `lark-bot codex-event` / hook notify | `lark-bot claude-event` + Claude Code hooks / Stop / PermissionRequest 类事件 |
| `codex/` app-server 编排 | **不要 1:1 照搬**。Claude Code 不共享 Codex app-server JSON-RPC。优先 notify-first；只有选定稳定 remote/session API 后再做托管控制面 |
| `TaskResult.source="codex"` | `source="claude"` / `claude_code` |

### 评估

- **价值**：高。日常已在用 Claude Code，companion 覆盖面直接翻倍，且不必另起产品形态。
- **风险**：中。Hook 名、payload、审批语义与 Codex 不同；过早做完整 orchestrator 会反复返工。
- **复杂度**：仅通知路径中等，可复用现有 adapter 测试形态。全双向闭环很大，应单独里程碑。
- **建议**：**与 MD 渲染之后或并行推进。** 交付顺序：
  1. Pydantic 事件模型 + 状态别名 + `source` 标签。
  2. CLI `claude-event`（file/stdin），能走通用 `/agent/events` 的尽量复用。
  3. 可安装、可审计的 hook/notify 片段，**默认不改写用户 Claude 配置**（与 Codex `lark-bot-notify.toml` 安全立场一致）。
  4. 托管 session 编排延后，等 notify UX 跑通再说。

---

## 方向 2 — 多进程并发 / 多 Bot 容量

### 意图

同时跑多个 agent 对话。你提出了两种模型：

**A. 同步 worker pool（线程池隐喻）**  
建 N 个飞书 bot（如 3 个），拉进同一群。最多 N 路并发 `lark-bot` 对话；每路对话占用一个 bot，直到释放。语义清晰：“哪个 bot 忙着”。

**B. 异步并发路由（可哈希映射）**  
把对话哈希/调度到 bot（甚至一个 bot 也行）。每条出站消息前打上元数据：对话 id、agent（codex/claude）、goal、cwd、任务状态等，方便程序和人区分。

### 与现有代码的关系

- Daemon 已在 SQLite 里建模 **多个 Codex session**（`CodexSession`、orchestrator live map）。session 并发 ≠ bot 并发。
- 出站路径是单个 `LarkBotClient(receive_id=...)`，没有 bot 池，也没有 per-session 接收目标。
- 入站审批路由已按 **精确通知的 `message_id`** 键控，而不是 bot 身份。这对并发下的 HITL 是正确原语。

### 评估

| 方案 | 优点 | 缺点 |
|------|------|------|
| **A. N-bot 池** | 飞书侧视觉隔离强；限流隔离自然；空闲/占用心智模型清楚 | N 套 app 凭证运维；进退群；池饥饿；难扩到大 N；把容量绑死在飞书 app 数量上 |
| **B. 异步 + 元数据** | 利用率高；一两个 app 即可；贴合现有 message_id 路由；元数据利于人扫读 | 同 bot 消息流更噪；依赖卡片/标题设计；仍需背压 / 最大并发策略 |
| **混合** | Daemon 内 session 可并发；仅在 UX 或 API 限流需要时上 bot 池 | 两层调度需要写清文档 |

- **价值**：真实多仓库 / 多 agent 场景很高；不阻塞单会话日常使用。
- **风险**：若把 multi-bot 当作**第一**并发设计，运维与状态（bot 租约表、崩溃回收）会主导复杂度。
- **建议**：**先在 daemon session 层做并发，而不是先上飞书 bot 层。**
  1. 限制托管 session 并发（`max_concurrent_sessions`）。
  2. 每条通知固定页眉：`session_id`、`source`、`name`、短 `goal`/`cwd`、状态。
  3. 回复路由继续靠 `message_id`（多 session 已适用）。
  4. 把 multi-bot 池做成可选 **transport 后端**，挂在 `BotTransport` 接口后（`single` | `pool`），而不是并发核心。
  5. 默认走 **B + 背压**；仅在单 bot 触顶限流、或人需要严格视觉槽位时再上 **A**。

---

## 方向 3 — Claude 与 Codex 是否分 Bot（用拉群管理）

### 意图

是否要把 Codex 与 Claude 绑到不同飞书 bot；是否用群成员关系作为管理面（“这个群的 Codex bot vs Claude bot”）。

### 评估

容易混在一起的两层“区分”：

1. **逻辑区分（必须）**  
   `source`、adapter、tags、session 存储命名空间、CLI 入口。Codex 已部分具备；Claude 绝不能糊进 `source="codex"`。

2. **物理 bot 区分（可选）**  
   按 agent 族拆分飞书 app / bot 身份 / receive 目标。

| 选择 | 何时占优 | 何时吃亏 |
|------|----------|----------|
| **同 bot，消息打标签** | 运维面单一；权限一次配齐；message_id 路由已可用；成本低 | 若标题弱，群里难扫 |
| **按 agent 分 bot** | 视觉立刻可滤；独立 token/限流；可 @ 对应 bot | 双倍 app 配置；群成员抖动；配置矩阵膨胀；仍要做逻辑路由 |
| **按容量槽位分 bot**（方向 2A） | 与 agent 类型正交 | 再和“按 agent 分 bot”混用、矩阵不清会难运维 |

“用群来管理”适合 **人组织项目**（例如一项目一群），但 **agent 路由不应只依赖“哪个 bot 在哪个群”**。更稳妥的是：

- 配置：日后可选 `agent → bot profile` 映射。
- 默认：一个 bot + 强 source 标签。
- 升级：`LARK_BOT_PROFILES` 或多 receive 绑定，且不改 orchestrator 核心逻辑。

- **建议**：**下一版不要强制 Claude / Codex 分 bot。**  
  做 Claude 适配时立刻做逻辑 `source` 分离。等方向 2 有 transport 抽象后，再提供 multi-bot 绑定。群用于 **项目/上下文**，不当主 agent 路由器。

---

## 方向 4 — 回复消息 Markdown 渲染

### 意图

通知内容按 Markdown 风格写，但当前以未渲染的 `text` 发出，手机端阅读体验差。

### 与现有代码的关系

- `LarkBotClient.render_notification_text` 拼多行纯文本。
- `build_text_message` 固定 `msg_type: "text"`。
- 飞书/Lark 可评估的 OpenAPI 选项：
  - **`post`**（富文本/post content）——结构化块，优于纯 text。
  - **`interactive` 卡片**——标题、markdown 元素、按钮（后续 approve/deny）、按状态配色。
  - 失败或简单 smoke test 保留 `text` 兜底。

### 评估

- **价值**：UX 收益高、产品风险低——每条消息立刻更好读。
- **风险**：低到中。卡片 schema 细节、长度限制、飞书 Markdown 子集与 GFM 差异；必须在 **渲染前** 完成脱敏。
- **复杂度**：集中在 notifier/client + payload 黄金测试。现有依赖 `message_id` 的审批流，卡片同样能返回稳定 `message_id`。
- **建议**：**最高优先级的快赢项。** 建议顺序：
  1. 在 `build_text_message` 旁增加 `render_notification_card` / `build_post_or_card_message`。
  2. 托管与 adapter 通知默认走 card/post；`send-test` 可强制 text。
  3. 状态 → 标题色 / emoji；Output tail 放可折叠或 fenced markdown 块。
  4. 保留脱敏与 tail 行数限制；卡片原始字段也不写 secrets。
  5. 后续：卡片按钮 approve/deny（可选替代纯 reaction UX）。

---

## 建议优先级

| 优先级 | 方向 | 理由 |
|--------|------|------|
| P0 | **4. MD / 卡片渲染** | 改动面小，立刻提升可读性，并为多 session 页眉打基础 |
| P1 | **1. Claude 通知适配** | 用 Codex 验证过的形态扩 agent 覆盖；先不做完整 orchestrator |
| P2 | **2. Session 并发 + 元数据** | Daemon 侧限流 + 打戳页眉；message_id 路由已多 session 就绪 |
| P3 | **3. 可选 multi-bot（按 agent 或槽位）** | 等有 transport 抽象后再做；逻辑 source 分离在 P1 已完成 |

## 横切设计原则

1. **Companion，不是 agent** — lark-bot 仍只摘要与中介，不成为第三个写代码大脑。
2. **先脱敏，再渲染 / 落库 / 发送** — 不变。
3. **HITL 按 `message_id` + interaction id 路由**，不靠 bot 显示名。
4. **Agent 族是 `source` 维度；bot 身份是 transport 维度。** 二者解耦。
5. **全局 home 仍是机器范围** — multi-bot 凭证与 session 租约落在 `LARK_BOT_HOME`，不进项目 cwd。
6. **优先一条抽象通知管线**：`Event → NotificationRequest → Render(text|post|card) → Transport(single|pool)`。

## 开放问题（后续产品决策）

1. Claude Code：v1 notify 覆盖哪些 hook/事件（Stop / PermissionRequest / Notification / UserPromptSubmit）？
2. 笔记本上 daemon 默认最大托管 session 并发数？
3. 有卡片后，approve/deny 是否从 reaction 迁到卡片按钮？
4. 默认是全 agent 共一群、一项目一群，还是审批只用私聊？
5. Profile 配置形态：单个 `.env` 多前缀，还是 `LARK_BOT_HOME/bots/*.toml`？

## 本文非目标

- 实现补丁
- 冻结 API 字段
- 选定具体飞书卡片 JSON schema（P0 启动时另开设计文档）

---

英文版：`docs/superpowers/specs/2026-07-17-next-iteration-directions.md`
