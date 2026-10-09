# Development Rules

## Writing Mode

- Keep plans and documents concise without sacrificing clarity.
- At the end of each plan, give me a list of unresolved questions to answer, if any.

## Engineering principles

- Use python env `conda activate livinit`
- Keep edits minimal. Delete obsolete code instead of layering compatibility paths.
- Modify existing functions in place. Do not create a new version of, or rename, a function.
- Do not over-engineering.
- Aim for simple, elegant implementations.
- Let agents interpret intent. Validate tool arguments and execution safety, not user wording. Avoid regex intent checks and redundant confirmations.
- Do not write tests for reversible, low-impact changes that mirror the implementation. If you do choose to verify your work with tests, make sure that the tests are meaningful and necessary to verify implementation.
- Run tests appropriate to the change and complete required checks. Once those pass, broaden or repeat testing only when new changes, failures, or unresolved concerns justify it; otherwise, continue toward completing the task.
- Delegate concrete independent work when the benefit outweighs coordination overhead. Use collaboration tools unless the requested workflow specifies Herdr.
- DO NOT CHANGE ANY CONTRACT VERSION JUST REPLACE DIRECTLY
- PR always target to `trunk` branch

## Conversational Style

- Keep answers short and concise
- No emojis in commits, issues, PR comments, or code
- No fluff or cheerful filler text (e.g., "Thanks @user" not "Thanks so much @user!")
- Use plain language over jargon, and reference technical details only to the degree that it helps illustrate an idea or your work to the user. Communicate complex concepts in a clear and cohesive manner, and calibrate your writing to the level of background knowledge assumed from the user's prompt and context.
- Prefer concrete behavior and small illustrations over abstract summaries, dense terminology, or unexplained lists of changes.
- When the user asks a question, answer it first before making edits or running implementation commands.
- When responding to user feedback or an analysis, explicitly say whether you agree or 
disagree before saying what you changed.

## Writing Issues

- Keep issue titles and descriptions short, plain, and easy to understand.
- Start with the user-facing problem or feature, then a few bullets describing expected behavior. Use concrete examples when helpful.
- Leave implementation plans, code references, contract details, and extensive acceptance/test checklists for later unless explicitly requested.
- Implementation plan should put under docs/plans with format <issue-#>-<feature>.md

## Code Quality

- Read affected code and relevant dependencies before editing or auditing. Read full files for wide-ranging changes or when local context is insufficient. Do not rely on search snippets for broad changes.
- No `any` unless absolutely necessary.
- Inline single-line helpers that have only one call site when doing so improves readability.
- Check node_modules for external API types; don't guess.
- Never remove or downgrade code to fix type errors from outdated deps; upgrade the dep instead.
- Ask before removing functionality or code that appears intentional unless that removal is already authorized by the current task.
- Do not preserve backward compatibility unless the user asks for it.
- Code refactor target for this scope only if user do not have any specific asking: livi-server, packages/decorator-agent, scripts

## User Override

Explicit task instructions override repository defaults. Ask about material unresolved scope decisions or missing authorization. Continue independent authorized work while waiting. This does not override sandbox or approval enforcement.

The user's instructions take precedence over guidelines provided in a skill. If explicit user instructions conflict with a skill's instructions, prioritize the user's instructions.
