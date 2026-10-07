---
name: orchestrate-implementation
description: Delegate implementation through Herdr. Use when the user explicitly requests implementation through Herdr or invokes this skill.
---

# Orchestrate implementation

Use a root orchestrator and implementation agents in Herdr. Invoking this skill selects Herdr; honor an explicit user override. Use the configured model with suitable effort (low, medium, high, xhigh) base on the complexity of task unless the user requests otherwise.

## Set up and assign

- Read applicable repository instructions and inspect the working tree, branch, HEAD, and relevant PR state. Preserve unrelated changes and capture the requested behavior and constraints.
- If the task names an issue, read it and any existing implementation plan. Resolve
  its number and title from the issue; ask only if the named target is ambiguous.
  Without an issue, use the user's task description and any existing relevant plan.
- Before implementation, create a dedicated Git worktree and branch named
  `issue-<number>-<title-slug>` for an issue or `task-<task-slug>` otherwise. Use a
  lowercase, hyphen-separated slug with punctuation removed. For example, issue
  #48 "Connect Studio chat" becomes `issue-48-connect-studio-chat`.
  Place `<repo-name>-<branch>` in a permitted writable root, beside the repository
  when permitted. Honor user-specified locations and filesystem approval requirements.
- Use the repository's default branch as the base unless the user or approved plan
  specifies another base. Verify
  the base ref before running `git worktree add -b <branch> <worktree-path> <base>`.
  Keep the original checkout and its uncommitted changes intact.
- Inspect `git worktree list` and existing branches/paths first. Reuse a matching
  task worktree only after verifying its branch, base, and local changes. If the
  intended branch exists without a worktree, verify it belongs to this task and
  attach it without `-b`. For unrelated name collisions, choose an unused numeric
  suffix; never overwrite a directory, reset a branch, or force a worktree lock.
- Load the Herdr skill, verify `HERDR_ENV=1`, and discover the CLI and actual root pane. Create a sibling pane with its working directory set to the dedicated worktree, without changing focus. Follow Herdr's readiness and prompt-delivery guidance; track every pane created for cleanup.
- If Herdr is unavailable, stop Herdr operations, report the limitation, and continue other authorized work. Ask before substituting another implementation backend when the user required Herdr.
- Give the implementation agent ownership of implementation and relevant tests, docs, and builds. The root independently inspects companion code or integration points and resolves boundary questions. Add agents only for concrete independent work; avoid overlapping edits.
- Assign one owner for Git mutations in a shared checkout. Use separate worktrees when writers need independent branches.
- Run implementation, checks, builds, and Git mutations in the task worktree.
  Include its absolute path, branch, and verified base in the agent brief.

Send a self-contained brief with `herdr agent prompt`, omitting irrelevant fields:

```text
Task: <requested behavior, scope, acceptance scenario>
Repository/state: <absolute path, branch, HEAD, relevant base/PR, existing changes>
Ownership: <agent's files and checks; root's independent investigation>
Constraints: <architecture, exclusions, allowed builds and external actions>
Publication gate: Complete any requested or warranted simplification, then have
the root verify the final diff and required checks before code push or PR publication.
Handoff: <local diff, commit, or authorized draft PR and base>
Report to: <actual root target via herdr agent prompt>
Flag scope changes early. Return changed behavior/files, check results, limitations,
and commit/PR details when applicable.
```

## Coordinate and verify

Use Herdr for assignments, follow-ups, and handoffs. Reuse existing agents. Share discoveries and user steering promptly; report meaningful progress without fixed milestone ceremonies. Inspect agent state/output when needed—silence or a pane title is not completion.

Companion-code findings do not authorize edits to that repository. Keep external actions within the current task's authorization; unrelated historical permission does not carry over.

The root verifies the final diff and acceptance scenario, including relevant failure cases. Ensure tests exercise the requested behavior rather than passing through unrelated automatic behavior. Run targeted checks and required builds; repeat only after changes, failures, or unresolved concerns. Distinguish synthetic checks from live-system evidence. Use independent review when it adds confidence, without fixed review loops unless required.

## Simplify when needed and verify before publishing

After implementation, apply [$code-simplification](../code-simplification/SKILL.md)
to changed code only when the user requests it or the changes warrant cleanup.
Delegate through Herdr when the benefit outweighs coordination overhead; otherwise
complete the pass directly. Preserve behavior and avoid unrelated cleanup.

If delegating, reuse an available agent or create a pane in the task worktree.
Give it the agreed scope, base ref, changed files, and existing validation results.
Pause other writers while it owns simplification; it must not push or publish a PR.
Wait for its handoff covering changes, checks, and remaining concerns.

The root inspects the final diff and ensures required checks pass before any code
push or PR publication, including draft PRs and pushes to an existing PR. After
further code changes, inspect those changes and rerun affected checks. A separate
simplification pass is not required for every task; do not force cleanup edits.

## Finish

For authorized draft PRs, use `gh pr create` with the verified base. Keep one PR per repository for the task. Verify the PR URL, draft status, base, commit, and working-tree state. PR preparation alone does not authorize merging, deployment, or other external actions.

Resolve remaining work before handoff. Include a concise **Change | Related code** Markdown table linking meaningful behavior to verified file lines, followed by validation and material limitations or commit/PR status. Use absolute clickable file targets with repository-relative labels.

Close and verify closure of panes created for this task before the final response, unless the user asks to keep them. Preserve the root and all pre-existing panes.

Report the task worktree path and branch at handoff. Keep the worktree available
for user review; remove it only when cleanup is explicitly authorized, after
verifying that no uncommitted or unpushed work would be lost.

Summary what channges were made, what code was affected in each file exclude test files.