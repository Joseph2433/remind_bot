# Multi-Agent Workflow Design

## Goal

Define a predictable multi-agent workflow for repository tasks using three available models: `gpt-5.6-sol`, `gpt-5.6-luna`, and `gpt-5.6-terra`. The workflow must keep one accountable owner while allowing bounded implementation and verification work to run through subagents.

## Roles

- `gpt-5.6-sol` is always the main thread. Sol owns requirement interpretation, repository inspection, planning, architecture, task decomposition, delegation, integration, final verification, commits, and the user-facing result.
- `gpt-5.6-luna` is the default implementation subagent. Luna handles scoped coding, local refactors, straightforward bug fixes, and other changes with explicit file and acceptance boundaries.
- `gpt-5.6-terra` is the default verification subagent. Terra handles tests, static checks, code review, regression analysis, and documentation updates.

These are default specialties rather than rigid restrictions. Sol may reassign Luna or Terra when task fit, workload, independence, or available context makes another allocation more effective.

## Delegation Rules

Sol delegates only concrete, bounded tasks that can be independently verified. Each assignment states the objective, allowed scope, relevant files, constraints, expected deliverables, and verification criteria. Architecture decisions, security-sensitive changes, ambiguous requirements, cross-cutting integration, and final acceptance remain with Sol.

Luna and Terra must not independently broaden their scope or make product-level decisions. If an assignment exposes ambiguity, overlapping ownership, or a required change outside its boundary, the subagent reports it to Sol instead of proceeding by assumption.

## Coordination and File Ownership

Sol inspects the working tree before delegation and preserves unrelated user changes. Parallel work should use disjoint files or clearly separated regions. Two subagents should not edit the same file concurrently unless Sol explicitly partitions ownership and accepts responsibility for resolving conflicts.

Subagents do not create final integration commits unless Sol explicitly delegates a self-contained commit. Sol reviews every resulting diff, resolves conflicts, confirms repository conventions, runs the final relevant verification, and owns the final commit history.

## Subagent Handoff Contract

Every subagent reports:

1. A concise summary of completed work.
2. Files changed or inspected.
3. Verification commands and their results.
4. Assumptions, risks, and unresolved issues.
5. Any recommended follow-up work without performing it outside the assigned scope.

## Execution Sequence

1. Sol reads the current implementation and working-tree state.
2. Sol restates the requirement and creates a small-step execution plan.
3. Sol keeps high-risk and integrative work in the main thread and delegates suitable bounded tasks.
4. Luna and Terra complete their assigned work and return structured handoffs.
5. Sol reviews and integrates the results, then runs targeted or full verification as appropriate.
6. Sol creates a focused commit for each completed step, following the repository's existing workflow.
7. Sol reports the integrated outcome, verification status, risks, and remaining work to the user.

## Success Criteria

- Sol remains the single accountable owner throughout the task.
- Luna and Terra have clear default specialties with controlled dynamic reassignment.
- Delegated work has explicit scope and acceptance criteria.
- Concurrent edits avoid file ownership conflicts.
- All subagent output is reviewed and verified before integration.
- Existing security, testing, and commit requirements remain in force.
