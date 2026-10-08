# Initial generation comparison

Issue: [#2](https://github.com/anthoai97/livi-pipeline/issues/2). Parent: [#1](https://github.com/anthoai97/livi-pipeline/issues/1).

**Phase 1 complete: comparison targets agreed.** Performance will be measured during implementation.

| Metric | Target |
| --- | --- |
| Total generation time | **Under 60 seconds**, from request submission until all three validated layouts are returned. |
| Valid results | **3 validated layouts** per request. |
| Generation rules | Preserve current budget, count, density, required furniture, decor, and geometry rules wherever possible. Record any deliberate changes. |

Compare the current and rebuilt pipelines using the same prompt, budget, and room metadata. Cover living room, bedroom, dining room, and studio. Reuse prompt starters from `web-pipeline/components/dashboard/DesignPromptSection.tsx` and existing web app budget and room presets.

Measure browser model loading and display separately from the 60-second target. Check Jev's benefit when integrating it; a separate benchmark project is not required to finish phase 1.

Existing timings and generation rules are documented in [Pipeline overview](../pipeline-overview.md). [Stack](../Stack.md) identifies the source repositories.

Unresolved questions: none for phase 1.
