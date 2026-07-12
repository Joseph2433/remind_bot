# Multi-Agent Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an operational three-model multi-agent workflow to the repository's `AGENTS.md`.

**Architecture:** Extend the existing `Agent Workflow` guidance with a dedicated `Multi-Agent Workflow` section. Keep `gpt-5.6-sol` accountable for orchestration and integration, give Luna and Terra default specialties, and define delegation, file ownership, handoff, verification, and commit rules.

**Tech Stack:** Markdown, Git

---

### Task 1: Document the multi-agent workflow

**Files:**
- Modify: `AGENTS.md`

- [ ] **Step 1: Add the workflow section**

Insert the following section immediately after the existing `Agent Workflow` section and before `Security & Configuration Tips`:

```markdown
## Multi-Agent Workflow

The available models are `gpt-5.6-sol`, `gpt-5.6-luna`, and `gpt-5.6-terra`. Use `gpt-5.6-sol` as the main thread for every task. Sol is the single accountable owner and is responsible for understanding requirements, inspecting the repository, planning, architecture decisions, task decomposition, delegation, integration, final verification, commits, and the final user-facing report.

Use the other models as subagents with default specialties:

- `gpt-5.6-luna` is the default implementation subagent. Assign Luna scoped coding, local refactors, straightforward bug fixes, and other changes with explicit file and acceptance boundaries.
- `gpt-5.6-terra` is the default verification subagent. Assign Terra tests, static checks, regression analysis, code review, and documentation updates.

These specialties are defaults, not rigid restrictions. Sol may dynamically reassign Luna or Terra based on task fit, workload, independence, and available context. Sol must retain architecture decisions, security-sensitive work, ambiguous requirements, cross-module integration, conflict resolution, and final acceptance.

When delegating work, Sol must provide:

1. A concrete objective and expected deliverable.
2. The allowed scope, relevant files, and prohibited changes.
3. Repository conventions and task-specific constraints.
4. Exact verification or acceptance criteria.
5. The required handoff format.

Delegate only bounded tasks that can be completed and verified independently. Subagents must not broaden their scope or make product-level decisions. If a task reveals ambiguity, overlapping ownership, or required work outside its boundary, the subagent must stop that part of the work and report it to Sol.

Before delegation, Sol must inspect the working tree and preserve unrelated user changes. Parallel tasks should use disjoint files or clearly separated regions. Do not let Luna and Terra edit the same file concurrently unless Sol explicitly partitions ownership and accepts responsibility for resolving conflicts.

Each subagent handoff must include:

- A concise summary of completed work.
- Files changed or inspected.
- Verification commands and their results.
- Assumptions, risks, and unresolved issues.
- Suggested follow-up work, without performing work outside the assigned scope.

Subagents should not create final integration commits unless Sol explicitly delegates a self-contained commit. Sol must review every resulting diff, resolve conflicts, confirm repository conventions, run the final relevant verification, and create focused commits following the repository's existing Git workflow.

For each task, follow this execution sequence:

1. Sol reads the implementation and working-tree state.
2. Sol restates the requirement and creates a small-step plan.
3. Sol keeps high-risk and integrative work in the main thread and delegates suitable bounded tasks.
4. Luna and Terra complete their assignments and return structured handoffs.
5. Sol reviews and integrates the results, then runs targeted or full verification as appropriate.
6. Sol creates a focused commit for each completed step.
7. Sol reports the integrated outcome, verification status, risks, and remaining work to the user.
```

- [ ] **Step 2: Review the Markdown content**

Run:

```powershell
Get-Content -LiteralPath AGENTS.md -Raw
rg -n "gpt-5\.6-(sol|luna|terra)|Multi-Agent Workflow" AGENTS.md
git diff --check -- AGENTS.md
```

Expected: all three model names and the workflow heading are present; the content review finds no incomplete language; `git diff --check` exits successfully.

- [ ] **Step 3: Commit the documentation change**

```bash
git add AGENTS.md
git commit -m "docs: add multi-agent workflow"
```
