---
name: issue-planning
description: Plan a user-selected issue, clarify consequential decisions, then update its requirements and write docs/plans using session authorization. Stops before implementation.
---

# Issue Planning

Deliver two artifacts: a concise issue updated with the agreed requirements and
a detailed implementation plan at `docs/plans/<issue-number>-<feature>.md` in the
target repository. Ground both artifacts in verified facts and agreed decisions.

## 1. Read and understand

- Resolve the issue number/URL against the user's repository and conversation
  context. If the target is still ambiguous, ask which issue; never choose one
  merely because it was mentioned in an earlier, unrelated task.
- Read the complete issue body and discussion, linked material needed to understand
  it, applicable repository instructions, and any existing plan. Use `gh` for
  GitHub issues, with the repository explicit in issue operations.
- Inspect the relevant code, callers, contracts, and tests. Distinguish current
  behavior, requested behavior, verified facts, user decisions, and unknowns.
- An empty or vague issue is a starting point for discovery, not permission to
  invent requirements. Research facts before asking the user for decisions.

## 2. Clarify consequential decisions

Ask focused questions about material unresolved decisions. Resolve routine
implementation choices from repository and conversation context. Do not ask
questions already answered in the issue or conversation; research facts first.

Read and apply the companion [grilling skill](../grilling/SKILL.md) only when the
user explicitly requests a stress test. Inform the user when applying it, and
use step 3 for any needed proposal approval rather than adding a separate gate.

Relevant decisions may concern intended users and behavior, scope boundaries,
acceptance criteria, consequential design tradeoffs, and rollout constraints.
Explore only branches relevant to this issue; this is not a mandatory questionnaire.
If the issue is already complete, skip clarification.

## 3. Prepare a reviewable proposal

Before updating the issue, prepare its exact proposed body and the full proposed
plan in temporary drafts. Preserve unrelated issue content and agreed decisions;
revise stale requirements instead of accumulating contradictory addenda.

Use the user's request and existing session authorization for issue and plan
writes. Ask only about material unresolved decisions or missing authorization;
do not require renewed confirmation for already-authorized work.

If approval is needed, present a concise summary and make both complete drafts
available for review before asking. Explain the missing authorization and link
any instruction that requires approval. Wait only on dependent actions; continue
other authorized work. A recommendation or silence is not approval. Corrections
reopen only affected decisions.

## 4. Update the issue and write the plan

Complete both artifacts within the session's authorization. Re-read the issue
before writing and reconcile concurrent edits; preserve new unrelated content.
If a new edit changes an agreed requirement, return only that decision to the user
before applying dependent updates.

Keep the issue short and user-facing:

- Concrete problem or desired behavior.
- A few precise bullets for agreed outcomes/acceptance criteria and scope limits.
- Plan path, with a working link only if that file is already published.

Keep technical detail in the plan. Update an existing plan in place when present;
otherwise use `docs/plans/<issue-number>-<feature>.md`. Include:

- Issue URL and status.
- Objective, verified current behavior, scope and explicit non-goals.
- Agreed decisions with enough rationale to explain consequential tradeoffs.
- Implementation phases in dependency order, concrete files/components or APIs to
  change, and how each phase produces the required behavior.
- A `File changes` table: `File | Action | Planned change | Why`. List the main
  files first, with one row per planned file edit, creation, or deletion. Use exact
  repository paths; link existing files relative to the plan and label new paths
  as proposed. Describe what changes in each file and why that change is needed
  for the agreed outcome. For new files, explain why an existing file cannot
  reasonably own the work. Include relevant tests/configuration/docs; write
  `No new files planned` when applicable. Verify existing paths from the repository
  and clearly mark any file choice that still depends on investigation. File change should exclude .test files.
- Relevant contracts, data/state transitions, failure paths and compatibility or
  migration consequences when applicable.
- Acceptance criteria and proportionate verification tied to those criteria.
- Real dependencies, rollout requirements and known risks; avoid generic checklists.
- `Unresolved questions` at the end: `None` when settled. Record explicitly deferred
  work separately; do not silently convert required unanswered decisions to deferrals.

Use precise, short prose even in a detailed plan. Do not add architecture,
functionality, or a new review/approval process the user did not agree to.
Mark the plan as ready for implementation, never implemented or verified by tests
that have not run.

For GitHub body updates, use a body file or a structured API field to preserve
newlines. Update the existing issue body, not a duplicate issue or a series of
summary comments. Keep labels, assignees, state and unrelated metadata unchanged.
Authorization for issue updates and plan creation does not authorize committing,
pushing, creating a PR, or implementation unless separately requested in the session.

## 5. Verify and hand off

Re-read the saved plan and fetch the updated issue. Check that facts, scope,
acceptance criteria and plan references agree with the proposal and settled
decisions. Ensure neither artifact introduces new assumptions. Verify links;
label local-only plans as local rather than inventing a GitHub link.

Finish with the issue link, clickable local plan path, and unresolved questions
if any. If either write fails, retain completed work and clearly report which
artifact remains blocked; do not claim the workflow is complete until both exist.
Stop here. The expected result is an issue with clear requirements and a detailed
implementation plan, not code.
