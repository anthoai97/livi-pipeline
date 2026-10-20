# Bring the new generate and chooser experience to the production web app

Issue: https://github.com/anthoai97/livi-pipeline/issues/68 (open). Status: ready for implementation.
Depends on [#67](67-move-generation-into-livinit-pipeline.md) for the pipeline facts; the
screens degrade to generic row text with the legacy pipeline.

Production paths below are in the `Livinit-ai/web-pipeline` checkout at
`../../../codebase/web-pipeline/` (branch `trunk`); links resolve on disk. Prototype paths
are in this repo under `web/`. Facts checked on 2026-10-16.

## Objective

`/generate` and `/select-variant` in production behave like the prototype's Generate and
Choose screens, built with the production design tokens and primitives in the light theme.
The data plumbing (pipeline hook, variant handoff, finalize, auth guard) stays.

## Current behavior

Production `/generate` ([GenerateWizardPage.tsx](../../../codebase/web-pipeline/app/generate/components/GenerateWizardPage.tsx)):

- Four step components portal an overlay and a right panel. Their task rows are scripted
  timers and hardcoded text ("Natural light: Bright, east-facing", "Budget fit 64%",
  "Scanning over 20k+ products"). Step 2 orbits ten stock photos; step 3 orbits the real
  selected assets with a fixed "Great fit" pill.
- Step changes are paced by minimum durations (6.5 s, 2 s, ring length) in
  `useGenerationWizard.ts`, which is instantiated five times (page, each step, the asset
  timeline), each with its own timers and URL writer.
- `GenerationRoomScene` rebuilds the WebGL scene on every step change, has no placeholder
  for a model still loading, and shows a cyan scan grid in reading mode only.
- The first `variant_ready` navigates to the chooser after 250 ms, from whatever step is on
  screen. `usePipelineRun` only tracks node progress for variant 0 and keeps no
  `node_complete` facts.
- `generation-animations.css` is 4,180 lines; about ten class families are unreferenced.

Production `/select-variant` ([VariantSelectionPage.tsx](../../../codebase/web-pipeline/app/select-variant/components/VariantSelectionPage.tsx)):
a locked `Scene3DViewer`, prev/next arrows with a dot pager, an "Option n" card with the
piece count and subtotal, Continue and Start over. No product list, no budget. Variants
arrive through the sessionStorage handoff and a live merge from the pipeline context.

Prototype ([Generate.tsx](../../web/src/screens/Generate.tsx), [Choose.tsx](../../web/src/screens/Choose.tsx)):
one room scene with effects per stage (walls rise, scan, door clearance, walkway, camera
sway), callouts with real sizes, a product ring (catalog shortlist, then a design's picks),
task rows built from `node_complete.data` ([tasks.ts](../../web/src/lib/tasks.ts)), a
segmented stepper with an elapsed clock, a user-driven "See all designs" button, and a
chooser with three design cards, product total, budget delta and a grouped product list.

## Scope

In scope: both screens' presentation and step logic, the pipeline hook's node facts, the
room scene effects and placeholders, deletion of the scripted steps and dead CSS, and the
chooser's new layout. Out of scope: the Create Layout form, the Build wizard, Studio, the
`/api/pipeline` proxy, the variant handoff format, and dark mode.

## Decisions

Revised 2026-10-19 at the stakeholder's request ("use the livi-pipeline/web UI UX, just make it
light"). These replace decisions 1, 2, 3 and 10 below:

- Both screens port the prototype's screens and components (`Generate.tsx`, `Choose.tsx`,
  `RoomScene.tsx`, `ProductRing.tsx`, `ui.tsx`) as closely as the production data allows, in the
  prototype's light palette (Tailwind `livi` colors), with Eudoxus Sans and lucide icons.
- The prototype's react-three-fiber `RoomScene` renders both screens
  (`app/generate/components/RoomScene.tsx`). `GenerationRoomScene` is back to its pre-#68 form and
  serves only the Build wizard and dashboard previews. The chooser no longer uses `Scene3DViewer`.
- The chooser preview orbits, as in the prototype. The generate room also orbits once a design is
  shown (a deviation: its copy says "Look around the room").
- Step 4 before any design is ready reads "Arranging the pieces of your first design in the room".

1. Light theme only: role tokens (`bg-primary`, `text-on-surface`, `bg-surface-container`,
   `border-outline-variant`, `text-success`, `bg-warning-container`), Eudoxus Sans, lucide
   icons, framer-motion, `components/ui` primitives. No hex, no `dark:` variants. The
   prototype's indigo palette and Geist are not ported.
2. Keep `GenerationRoomScene`, do not port the prototype's `RoomScene`. It renders the user's
   wall colors, floor material and opening styles and shares the GLB cache with the chooser
   and Studio. The prototype's effects are added to it.
3. One scene instance across the four steps: mode changes update materials and effects in
   place instead of rebuilding, so walls rise once and the room never flashes.
4. Task rows come from pipeline facts. `usePipelineRun` stores every `node_complete` payload
   per shared node and per variant (not only variant 0); the rows read them through a port of
   the prototype's `tasks.ts`. Missing facts show the prototype's default row text.
5. Steps are derived from the furthest node reached (`currentStep` in the prototype's
   `run.ts`) and paced only by the prototype's 1.8 s minimum and the 2 s hold on the room
   step, in one `useGenerationWizard` instance passed down by the page.
6. No automatic navigation to the chooser. The first ready design furnishes the room with a
   "Showing Design n" pill; the "See all designs" button captures the handoff and navigates.
   The 90 s backstop and the Step 4 navigation go away.
7. The product ring shows real data: the retrieve preview (top product per slot) first, then
   the first design whose selection is valid. The stock photo catalog is deleted.
8. Callouts reuse the existing anchor projection (`ScanChips` mechanism) with the
   prototype's labels: floor area, ceiling height, "Door 0.9 m", "Window 1.2 m".
9. Placeholder boxes sized from backend dimensions stand in for models still loading, so the
   first design appears the moment `variant_ready` arrives.
10. Chooser: orbit allowed in the preview (`Scene3DViewer` with `interactionLocked` off,
    `enableAssetEditing` and `enableKeyboardShortcuts` off), three design
    cards replace the arrows and dots (arrow keys and swipe stay), the aside adds product
    total, budget delta from the handoff's `budget`, and the grouped product list. Data logic
    (auth guard, load, live merge, prefetch, finalize, abandon, leave guards) is unchanged.
11. The fit-check dialog wiring stays until #67 removes the event for new designs.
12. Copy and labels follow the prototype (`STEPS`, `COPY`, task titles); `STEP_NAMES` in
    `nodeStepMap.ts` takes them (only the generate page reads it).

## Phases

### Phase 1: Pipeline facts in the hook

- `services/ApiClient.ts`: the `node_complete` branch of `normalizeLegacySseEvent` passes
  the frame's `data` through as `facts` (today it spreads it into the event).
- `features/pipeline/usePipelineRun.ts`: new `nodeFacts` state, `{shared, variants[3]}`,
  each node entry `{status, runs, elapsed, doneAt, facts}`; the `progress` case updates it
  for every variant index. Exposed on the hook return and the provider.
- `types/pipeline.ts`: `NodeFacts` per node, from the pipeline README's browser delivery
  table (`interpret`, `extract_room`, `rag_scope_assets`, `select_asset_intent`,
  `layout_initial`, `render_scene`), optional fields, no `any`.
- `lib/wizard/nodeStepMap.ts`: `interpret` joins step 1; `STEP_NAMES` become the prototype's.

### Phase 2: Generate screen

- `GenerateWizardPage.tsx`: new layout from the prototype. Header: wordmark, segmented
  stepper, elapsed clock, Cancel. Main: stage (scene, callouts, product ring, brief chips,
  "Showing Design n") and aside (step copy, task rows, "Up next" or the CTA, stopped state
  with Try again and Edit room details). Keeps the saved-room gate, error and fit dialogs,
  `start`, `abort`, `retry`, `captureVariantSelection`.
- `useGenerationWizard.ts`: step from `nodeFacts` with the prototype's pacing; drop the
  pacing constants, the auto-navigation, `roomFacts`, `signals`, `advanceTo`; keep the GLB
  readiness gates for the chooser prefetch.
- New `GenerationTasks.ts` (port of `tasks.ts`), `TaskRow.tsx`, `SegmentedStepper.tsx`,
  `ProductRing.tsx`, `RoomCallouts.tsx`, and `useDropIn.ts` (the `placedSpecs`, `startFly`
  and `onBeforeRender` plumbing moved out of Step 4).
- Delete the four step components, `LiviPanel`, `LiviModelBadge`, `BottomBar`,
  `FloatingCardsLayer`, `FloatingProductCard`, `OrbitalImageRing`, `JarvisScanOverlay` and
  its CSS, `WizardProductsContext`, `orbitChoreography.ts`, `photoCatalog.ts`,
  `generation-animations.css` (replaced by a small `generation.css` with the shimmer
  keyframe), the stock `CARD_POSITIONS` data, and `WizardSceneContext` (only the page and
  the deleted steps import it; the page renders its overlays directly).

### Phase 3: Room scene effects

`components/three/GenerationRoomScene.tsx`:

- Rebuild only on geometry or `interactive` changes; `generationMode` and `step` update
  wall opacity, overlays and camera in place.
- Walls rise from the floor on the first build (0.6 s, staggered), reduced motion honored.
- Door clearance quads (door rect pushed into the room) while the room is read and until a
  design is shown; a walkway band from the main door across the room during the fit step.
  Both are floor overlays computed from the geometry, as the prototype's `Clearance` and
  `Walkway` do.
- Camera sway in the waiting modes; off when a design is shown or `interactive`.
- A placeholder box per mounted spec until its GLB is in the cache.

### Phase 4: Chooser

- `VariantSelectionPage.tsx` JSX: preview with orbit, three design cards (name, status with
  spinner or price, check on the selected one), aside with design name, item count, product
  total, budget delta, Continue, "n designs still generating", grouped product list with
  thumbnails and "Decor" for unpriced items.
- `features/variant-selection/variantSelectionView.ts`: `groupedProducts(variant)` and
  `budgetDelta(variant, budget)` helpers next to `selectionFacts`.

### Phase 5: Verification and cleanup

`pnpm type-check`, `pnpm lint`, `pnpm build`; unit tests for the facts-to-rows mapping and
the `nodeFacts` reducer (node test runner, like `test:shapes`); manual pass on the dev
stack against the new pipeline (#67) and against the legacy one.

## File changes

web-pipeline (paths relative to that repo):

| File | Action | Planned change | Why |
| --- | --- | --- | --- |
| [app/generate/components/GenerateWizardPage.tsx](../../../codebase/web-pipeline/app/generate/components/GenerateWizardPage.tsx) | Modify | Prototype layout, one wizard instance, CTA-driven navigation; keep gate, dialogs and actions | The screen's composition |
| [app/generate/components/hooks/useGenerationWizard.ts](../../../codebase/web-pipeline/app/generate/components/hooks/useGenerationWizard.ts) | Modify | Step from node facts with prototype pacing; remove auto-navigation, pacing constants, dead fields | Truthful steps, single instance |
| `app/generate/components/GenerationTasks.ts` (proposed) | Create | Port of the prototype's `tasks.ts` | Pure facts-to-rows mapping; no existing module owns it |
| `app/generate/components/TaskRow.tsx` (proposed) | Create | Row with status icon and tone in role tokens | Replaces `LiviPanel` task cards |
| `app/generate/components/SegmentedStepper.tsx` (proposed) | Create | Four-segment progress bar | The prototype's header; the dotted DS `Stepper` stays for the Build wizard |
| `app/generate/components/ProductRing.tsx` (proposed) | Create | Orbiting product cards from retrieve preview and design picks, skeletons, mobile strip | Replaces `OrbitalImageRing` and `FloatingCardsLayer` |
| `app/generate/components/RoomCallouts.tsx` (proposed) | Create | Anchor-projected pills with real sizes | Replaces `ScanChips` in Step 1 |
| `app/generate/components/hooks/useDropIn.ts` (proposed) | Create | `placedSpecs`, staggered `startFly`, lift-in and settle `onBeforeRender` | Keeps Step 4's plumbing without the step component |
| `app/generate/components/generation.css` (proposed) | Create | Shimmer keyframe and stage insets | Replaces the 4,180-line file |
| [app/generate/components/steps/Step1ReadingRoom.tsx](../../../codebase/web-pipeline/app/generate/components/steps/Step1ReadingRoom.tsx), [Step2SelectingPieces.tsx](../../../codebase/web-pipeline/app/generate/components/steps/Step2SelectingPieces.tsx), [Step3BestFitFound.tsx](../../../codebase/web-pipeline/app/generate/components/steps/Step3BestFitFound.tsx), [Step4RefiningLayout.tsx](../../../codebase/web-pipeline/app/generate/components/steps/Step4RefiningLayout.tsx), [LiviPanel.tsx](../../../codebase/web-pipeline/app/generate/components/steps/LiviPanel.tsx), [FloatingCardsLayer.tsx](../../../codebase/web-pipeline/app/generate/components/steps/FloatingCardsLayer.tsx), [FloatingProductCard.tsx](../../../codebase/web-pipeline/app/generate/components/steps/FloatingProductCard.tsx), [OrbitalImageRing.tsx](../../../codebase/web-pipeline/app/generate/components/steps/OrbitalImageRing.tsx) | Delete | Scripted steps and decorative rings | Replaced by facts-driven rows and the product ring |
| [app/generate/components/BottomBar.tsx](../../../codebase/web-pipeline/app/generate/components/BottomBar.tsx), [LiviModelBadge.tsx](../../../codebase/web-pipeline/app/generate/components/LiviModelBadge.tsx), [WizardProductsContext.tsx](../../../codebase/web-pipeline/app/generate/components/WizardProductsContext.tsx), [panels/JarvisScanOverlay.tsx](../../../codebase/web-pipeline/app/generate/components/panels/JarvisScanOverlay.tsx), [panels/jarvis-scan.css](../../../codebase/web-pipeline/app/generate/components/panels/jarvis-scan.css), [three/orbitChoreography.ts](../../../codebase/web-pipeline/app/generate/components/three/orbitChoreography.ts), [generation-animations.css](../../../codebase/web-pipeline/app/generate/components/generation-animations.css) | Delete | Old chrome and dead code | Not part of the new layout |
| [lib/wizard/photoCatalog.ts](../../../codebase/web-pipeline/lib/wizard/photoCatalog.ts) | Delete | Stock photo list | The ring shows real products |
| [lib/wizard/localGlbCatalog.ts](../../../codebase/web-pipeline/lib/wizard/localGlbCatalog.ts) | Modify | Remove `CARD_POSITIONS` (only the deleted ring components import it) | Stock card data |
| [lib/wizard/wizardPacing.ts](../../../codebase/web-pipeline/lib/wizard/wizardPacing.ts) | Modify | Keep `IMAGE_PRELOAD_CAP_MS` and `GLB_PRELOAD_CAP_MS` (used by `useAssetPreload`); drop the step and navigation constants | Step pacing moves to the wizard hook |
| [lib/wizard/nodeStepMap.ts](../../../codebase/web-pipeline/lib/wizard/nodeStepMap.ts) | Modify | `interpret` in step 1; prototype step names | Step derivation and labels |
| [components/three/GenerationRoomScene.tsx](../../../codebase/web-pipeline/components/three/GenerationRoomScene.tsx) | Modify | In-place mode changes, rising walls, clearance and walkway overlays, camera sway, placeholder boxes | The prototype's scene effects on the production renderer |
| [features/pipeline/usePipelineRun.ts](../../../codebase/web-pipeline/features/pipeline/usePipelineRun.ts) | Modify | `nodeFacts` for shared and per-variant nodes, all variants | Rows and design status need the facts |
| [services/ApiClient.ts](../../../codebase/web-pipeline/services/ApiClient.ts) | Modify | Pass `node_complete.data` as `facts` | Avoid the field-spread collision |
| [types/pipeline.ts](../../../codebase/web-pipeline/types/pipeline.ts) | Modify | `NodeFacts` types | Typed facts |
| [app/select-variant/components/VariantSelectionPage.tsx](../../../codebase/web-pipeline/app/select-variant/components/VariantSelectionPage.tsx) | Modify | Prototype layout; data logic unchanged | The chooser's look |
| [features/variant-selection/variantSelectionView.ts](../../../codebase/web-pipeline/features/variant-selection/variantSelectionView.ts) | Modify | Grouped products and budget delta helpers | View facts live here already |
| [app/generate/components/WizardSceneContext.tsx](../../../codebase/web-pipeline/app/generate/components/WizardSceneContext.tsx) | Delete | Portal slots unneeded when the page renders overlays | Simpler composition |

## Contracts

Facts the rows read, from `node_complete.data` (pipeline README, browser delivery):

| Node | Fields | Rows |
| --- | --- | --- |
| `interpret` | `style_hints`, `requested_categories` | Reading your brief; brief chips |
| `extract_room` | `room_area`, `wall_height`, `doors`, `windows`, `floor_area_sqm`, `usable_area_sqm`, `fit`, `fit_message` | Room dimensions, Doors and windows, Usable floor, Space check; callouts |
| `rag_scope_assets` | `slots`, `candidates`, `gaps`, `preview` | Catalog search; ring shortlist |
| `select_asset_intent` (shared) | `ranked_slots`, `shared_products` | Comparing products |
| `select_asset_intent` (variant) | `turn`, `valid`, `errors`, `fit_step`, `total_cost`, `items` | Design n row; ring picks |
| `layout_initial` | `placed`, `findings`, `blocking` | Design n row |
| `render_scene` | `valid`, `errors`, `dropped`, `review` | Design n row |

With the legacy pipeline these payloads are absent and every row shows its default text;
steps still advance from node names. `variant_ready`, `variant_failed`, `complete` and the
handoff record are unchanged.

## Acceptance criteria

1. On the dev stack with the new pipeline: the room rises and is scanned with real size
   labels, clearances and the walkway appear at their steps, the ring shows catalog
   products then a design's picks, rows show counts and prices, the first finished design
   furnishes the room with placeholders replaced by models, and only the button opens the
   chooser.
2. On the dev stack with the legacy pipeline the screen still runs: generic rows, steps
   advance, designs arrive.
3. Chooser: three cards with status or price, orbitable preview, product total, budget
   delta, grouped list; Continue reaches Studio with the first chat message; Start over and
   the leave guards behave as today; designs still generating arrive on the cards.
4. Phone width: stage and panel stack, the ring becomes a scroll strip, no horizontal
   scroll.
5. No hex colors or `dark:` classes in the changed files; `pnpm type-check`, `pnpm lint`
   and `pnpm build` pass; the listed files are deleted.

## Dependencies, rollout, risks

- #67 on the dev service for criterion 1; the screens ship independently.
- `GenerationRoomScene` in-place updates are the riskiest change: the file is 1,508 lines
  and rebuilds today; test the Build wizard preview and the dashboard room preview, which
  also render it.
- `usePipelineRun` is 1,691 lines and shared with Studio; the `nodeFacts` addition must not
  change existing fields.
- Removing auto-navigation changes a flow QA may expect; it is the agreed behavior.
- PR targets `trunk` of `Livinit-ai/web-pipeline`; deploy to `web.dev.livinit.ai` first.

## Unresolved questions

None.
