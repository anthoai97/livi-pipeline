---
name: issue-investigation
description: "Investigate a selected GitHub issue and report findings and root cause before proposing a fix. Update the issue with an agreed fix plan when requested. Stops before implementation."
---

# Issue investigation

Explain the reported behavior from evidence before deciding what to change.
An investigation request authorizes investigation and reporting. Continue to a
fix plan or GitHub update when the user has also authorized that work; do not add
another confirmation step. Issue updates do not authorize implementation.

## Investigate the reported behavior

- Resolve the selected issue from its URL or repository context. Read its complete
  body, comments, and attachments with `gh`, specifying the repository explicitly.
  Inspect screenshots rather than relying on their captions. Use authenticated
  access for private attachments before concluding that an image is unavailable.
- Identify the user's requested result and the observed result. Keep the inquiry
  focused on that difference. An adjacent limitation is not automatically the
  cause or part of the fix.
- Read affected code and relevant dependencies. Trace the request through the
  agent, tool schema and actual arguments, backend, saved state, and displayed
  result as applicable. Studio frontend work belongs to `web-pipeline`, not
  `livi-client`.
- Check existing capabilities before proposing another tool, filter, or service.
  Verify the meaning, units, and provenance of compared values. For example,
  bed-frame bounds do not establish mattress size, and a shared summary may not
  describe the selected variant.
- Look for the exact conversation, design, product, or run in available local
  records. Ask only for consequential missing information that cannot be found;
  continue independent investigation while waiting.
- Use focused reproductions with the current runtime or adapter. When live access
  is authorized, use configured credentials for read-only probes without printing
  secrets. Keep temporary scripts and outputs outside production source. Label
  fixtures and simulations explicitly; do not change live rooms or catalog data
  to investigate.
- For latency, separate model, context, validation, rendering, and presentation
  time. Inspect queues and locks before proposing parallel work. Combined elapsed
  time does not establish which stage is slow.

## Report findings before proposing a fix

Lead with the user-facing finding, then explain the causal path in plain language.
Include a concrete example, relevant source links, and the checks actually run.
Distinguish verified code behavior, live results, simulations, and inferences.

State unknowns that limit the conclusion. If the original incident is unavailable,
report the reproduced mechanism without claiming an exact reproduction. Historical
timings from another conversation are supporting evidence, not this incident's
measurements.

For an investigation-only request, stop after the findings and root cause. If the
user also requested a plan, present findings first and then continue within that
authorization. Answer follow-up questions about current behavior before expanding
the proposed fix.

## Put the authorized plan in GitHub

Base the plan on the agreed requirement and verified cause. Prefer a focused
change to existing functions and capabilities. Identify the relevant files,
expected behavior, proportionate verification, and any missing data that limits
the result. End the plan with unresolved questions, or `None` when settled.

Honor the user's chosen plan location. For small issues in an issue-only workflow,
put findings, root cause, and the plan directly in the issue body. Do not create a
plan file when the user says none is needed. For a separate implementation plan,
follow the project conventions and [issue-planning](../issue-planning/SKILL.md)
within the user's authorization.

Prepare the exact updated body in a temporary file. Re-read the issue before
writing and reconcile concurrent edits. Preserve screenshots, links, unrelated
requirements, and metadata. Change labels such as `ready to fix` only when
requested for the selected issue.

Update the existing issue with `gh issue edit --repo <owner/repository> --body-file
<draft>`, or a structured API body. Replace stale speculation with the agreed
findings rather than adding duplicate summary comments. Fetch the saved issue
and verify its body and attachments. Finish with the issue link and a short
statement of what was updated.
