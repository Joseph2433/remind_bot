# Next Iteration Directions (2026-07-17)

Record and evaluate four product directions for the version after the global-daemon-home work. This is a planning note, not an implementation plan.

## Current baseline (relevant constraints)

- One Lark app / one receive target: `LARK_BOT_LARK_APP_ID` + single `LARK_BOT_LARK_RECEIVE_ID`.
- Outbound notifications are plain `msg_type: text` (`src/lark_bot/lark/client.py` → `build_text_message`).
- Codex has a deep path: hooks notify fragment, event adapter, app-server orchestrator, approval/input loop, SQLite session store under machine-global `LARK_BOT_HOME`.
- Generic agent ingress already exists (`POST /agent/events` + `AgentEvent`), while Codex has a dedicated adapter (`notifications/adapters/codex.py`) and a full managed control plane under `codex/`.
- The product identity is still a **companion**: watch, summarize, redact, notify, and for managed Codex, mediate human-in-the-loop — not an independent coding agent.

---

## Direction 1 — Claude Code adapter (mirror Codex)

### Intent

Add first-class Claude Code support the way Codex was adapted: event schema, status normalization, tags/source, optional hook/install path, and later optional HITL if Claude Code exposes a stable control surface.

### Fit with current code

| Codex piece | Claude analogue to design |
|-------------|---------------------------|
| `notifications/adapters/codex.py` | `notifications/adapters/claude.py` (or `claude_code.py`) |
| `lark-bot codex-event` / hook notify | `lark-bot claude-event` + Claude Code hooks / Stop / PermissionRequest style events |
| `codex/` app-server orchestrator | **Not 1:1**. Claude Code does not share Codex app-server JSON-RPC. Prefer notify-first; only add a managed control plane if a stable remote/session API is chosen later. |
| `source="codex"` on `TaskResult` | `source="claude"` / `claude_code` |

### Evaluation

- **Value**: High. User already lives in Claude Code; companion value doubles without inventing a new product.
- **Risk**: Medium. Hook names, payload shapes, and approval semantics differ from Codex; over-fitting a full orchestrator early will thrash.
- **Complexity**: Notify-only path is moderate and mirrors existing adapter tests. Full bidirectional loop is large and should be a separate milestone.
- **Recommendation**: **Do next after / in parallel with MD render.** Ship:
  1. Pydantic event model + status aliases + `source` tag.
  2. CLI `claude-event` (file/stdin) and reuse generic `/agent/events` where possible.
  3. Installable/auditable hook or notify fragment **without** rewriting user Claude settings by default (same safety stance as Codex `lark-bot-notify.toml`).
  4. Defer managed session orchestration until notify UX is proven.

---

## Direction 2 — Multi-process concurrency / multi-bot capacity

### Intent

Run multiple agent conversations at once. Two sketched models:

**A. Sync worker pool of bots (thread-pool metaphor)**  
Create N Feishu bots (e.g. 3), put them in the same group. At most N concurrent `lark-bot` conversations; each conversation occupies one bot until release. Clear “which bot is busy” semantics.

**B. Async concurrent routing on fewer bots**  
Hash or schedule conversations onto bots (or even one bot). Before every outbound message, stamp metadata: conversation id, agent (codex/claude), goal, cwd, task status — so both the program and the human can disambiguate replies.

### Fit with current code

- Daemon already models **multiple Codex sessions** in SQLite (`CodexSession`, orchestrator live map). Concurrency of *sessions* is not the same as concurrency of *bots*.
- Outbound path is single `LarkBotClient(receive_id=...)`. No bot pool, no per-session receive target.
- Inbound routing for approvals already keys off **exact notification `message_id`**, not bot identity. That is the right primitive for HITL under concurrency.

### Evaluation

| Approach | Pros | Cons |
|----------|------|------|
| **A. N-bot pool** | Strong visual separation in Feishu; natural rate-limit isolation; easy “slot free/busy” mental model | N app credentials to manage; join/leave groups; pool starvation; hard to scale past small N; couples capacity to Feishu app count |
| **B. Async + metadata** | Better utilization; one or few apps; matches existing message_id routing; metadata helps human scan | Same-bot message stream is noisier; needs good card/header design; still need backpressure / max concurrent policy |
| **Hybrid** | Sessions concurrent in daemon; optional bot pool only when UX or API limits demand it | Two layers of scheduling to document |

- **Value**: High for real multi-repo / multi-agent use; not blocking single-session daily use.
- **Risk**: High if multi-bot is the *first* concurrency design — ops cost and state (bot lease table, reclaim on crash) dominate.
- **Recommendation**: **Implement concurrency in the daemon session layer first**, not the Feishu bot layer.
  1. Cap concurrent managed sessions (`max_concurrent_sessions`).
  2. Put a stable header on every notification: `session_id`, `source`, `name`, short `goal`/`cwd`, status.
  3. Keep reply routing by `message_id` (already correct for multi-session).
  4. Treat multi-bot pool as an optional **transport backend** behind a `BotTransport` interface later (`single` | `pool`), not as the concurrency core.
  5. Prefer **B + backpressure** as default; adopt **A** only when one bot is rate-limited or the human needs strict visual slots.

---

## Direction 3 — Separate bots for Claude vs Codex (group-based management)

### Intent

Decide whether Codex and Claude should bind to different Feishu bots, and whether group membership is the management surface (“this group’s Codex bot vs Claude bot”).

### Evaluation

Two layers of “distinction” are easy to conflate:

1. **Logical distinction (required)**  
   `source`, adapter, tags, session store namespace, CLI entrypoints. Already partially present for Codex; Claude must not blur into `source="codex"`.

2. **Physical bot distinction (optional)**  
   Separate Feishu apps / bot identities / receive targets per agent family.

| Choice | When it wins | When it loses |
|--------|--------------|---------------|
| **Same bot, tagged messages** | Single ops surface; one permission grant; message_id routing already works; cheaper | Busy group hard to skim if titles are weak |
| **Separate bots by agent** | Instant visual filter; independent tokens/rate limits; can @mention the right bot | Double app setup; group member churn; config matrix (`codex_app_*`, `claude_app_*`); still need logical routing |
| **Separate bots by capacity slot** (Dir 2A) | Orthogonal to agent type | Mixing “slot bots” with “agent bots” without a clear matrix confuses operators |

Group-as-management is useful for **human organization** (e.g. one chat per project), but **agent routing should not depend on “which bot is in which group” alone**. Prefer:

- Config: optional `agent → bot profile` map later.
- Default: one bot, strong source labels.
- Upgrade path: `LARK_BOT_PROFILES` or multi-receive bindings without rewriting orchestrator logic.

- **Recommendation**: **Do not hard-require separate bots for Claude vs Codex in vNext.**  
  Require logical `source` separation immediately with the Claude adapter. Offer multi-bot binding as configuration once Direction 2’s transport abstraction exists. Use groups for **project/context**, not as the primary agent router.

---

## Direction 4 — Markdown-rendered Lark replies

### Intent

Notifications are authored as Markdown-ish plain text but sent as unrendered `text`, which is hard to scan on mobile.

### Fit with current code

- `LarkBotClient.render_notification_text` builds multi-line plain text.
- `build_text_message` always sets `msg_type: "text"`.
- Feishu/Lark options to evaluate (OpenAPI):
  - **`post`** (rich text / post content) — structured blocks, better than raw text.
  - **`interactive` card** — title, markdown element, buttons (approve/deny later), color by status.
  - Keep `text` as fallback for failure or simple smoke tests.

### Evaluation

- **Value**: High UX, low product risk — every message benefits immediately.
- **Risk**: Low–medium. Card schema quirks, length limits, and markdown subset differences between Feishu and GitHub-flavored MD; must keep redaction **before** render.
- **Complexity**: Contained in notifier/client + golden tests for payload shape. Approval flows that currently depend on message content remain OK if cards still produce a stable `message_id` (they do).
- **Recommendation**: **Highest priority quick win.** Suggested sequence:
  1. Introduce `render_notification_card` / `build_post_or_card_message` next to `build_text_message`.
  2. Default managed + adapter notifications to card/post; keep `send-test` able to force text.
  3. Status → header color / emoji; put Output tail in a collapsible or fenced markdown block.
  4. Preserve redaction and tail line limits; never put secrets into card raw fields.
  5. Later: card actions for approve/deny (optional replacement for reaction-only UX).

---

## Suggested priority order

| Priority | Direction | Rationale |
|----------|-----------|-----------|
| P0 | **4. MD / card render** | Small surface, immediate readability, foundation for multi-session headers |
| P1 | **1. Claude notify adapter** | Expands agent coverage with Codex-proven shape; avoid full orchestrator initially |
| P2 | **2. Session concurrency + metadata** | Daemon-side caps and stamped headers; message_id routing already multi-session ready |
| P3 | **3. Optional multi-bot by agent or slot** | Only after transport abstraction; logical source split already done in P1 |

## Cross-cutting design principles

1. **Companion, not agent** — lark-bot still summarizes and mediates; it does not become a third coding brain.
2. **Redact before render / store / send** — unchanged.
3. **Route HITL by `message_id` + interaction id**, not by bot display name.
4. **Agent family is a `source` dimension**; bot identity is a transport dimension. Keep them decoupled.
5. **Global home stays machine-scoped** — multi-bot credentials and session leases live under `LARK_BOT_HOME`, not project cwd.
6. **Prefer one abstract notification pipeline**: `Event → NotificationRequest → Render(text|post|card) → Transport(single|pool)`.

## Open questions (for later product decisions)

1. Claude Code: which hook/events are in-scope for v1 notify (Stop / PermissionRequest / Notification / UserPromptSubmit)?
2. Default max concurrent managed sessions on a laptop daemon?
3. Should approve/deny move from reactions to card buttons once cards exist?
4. One shared group for all agents vs one group per project vs DM-only for approvals?
5. Profile config shape: single `.env` multi-prefix vs `bots/*.toml` under `LARK_BOT_HOME`?

## Non-goals for this note

- Implementation patches
- API field freezes
- Choosing a concrete Feishu card JSON schema (do that in a dedicated design when P0 starts)

---

中文版：`docs/superpowers/specs/2026-07-17-next-iteration-directions.zh-CN.md`
