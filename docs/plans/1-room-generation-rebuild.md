# Rebuild initial room generation

Issue: [#1](https://github.com/anthoai97/livi-pipeline/issues/1).
Status: phase 1 complete; phases 2 and 3 ready for implementation; later phases planned.

Turn a prompt, budget, and room metadata into three validated furnished layouts.
Cover living room, bedroom, dining room, and studio. Return all three layouts in
under 60 seconds from request submission, and measure browser loading separately.
Preserve current budget, count, density, required furniture, decor, and geometry
rules wherever possible. Record deliberate rule changes.

## Phases

Each phase is one issue. Implement them in order.

| Phase | Issue | Scope | Plan |
| --- | --- | --- | --- |
| 1, complete | [#2](https://github.com/anthoai97/livi-pipeline/issues/2) | Agree on comparison targets and inputs. | [Generation targets](2-generation-baseline.md) |
| 2 | [#3](https://github.com/anthoai97/livi-pipeline/issues/3) | Build embedding storage, generation, image caching, retrieval, and catalog population. | [Embedding pipeline](3-embedding-pipeline.md) |
| 3 | [#4](https://github.com/anthoai97/livi-pipeline/issues/4) | Build LangGraph stages, shared work, variant state, progress, cancellation, and bounded failures. | [LangGraph runtime](4-langgraph-runtime.md) |
| 4 | [#5](https://github.com/anthoai97/livi-pipeline/issues/5) | Rebuild product selection and placement with Jev decisions, existing rules, and bounded corrections. | Detailed planning follows phase 3. |
| 5 | [#6](https://github.com/anthoai97/livi-pipeline/issues/6) | Deliver three variants in parallel and connect results to the browser viewer. | Detailed planning follows phase 4. |
| 6 | [#7](https://github.com/anthoai97/livi-pipeline/issues/7) | Compare performance and validity, then replace initial generation with deployment rollback available. | Detailed planning follows phase 5. |

The pipeline is Python end to end. Phase 2 uses Gemini Embedding 2 and pgvector.
Gemini and Jev API access are confirmed. Storage, embedding generation, and
retrieval stay together in phase 2.

Phase 1 is complete because its targets are agreed. The rebuilt pipeline's speed
has not been verified. Later phases check speed, validity, visual quality, and cost
against the current flow using the same inputs.

Use the [pipeline overview](../pipeline-overview.md),
[preparation design](../product-preparation-v2.md),
[embedding format](../product-embedding-v2.md), and [stack notes](../Stack.md)
as source context. Replace contracts directly without adding contract versions.

## Unresolved questions

None for the agreed scope or the phase 2 and 3 plans. Detailed plans for phases
4 through 6 are deferred until those phases are refined.
