# Adding a room type

A room type (`living_room`, `bedroom`, `dining_room`, `studio`) is the room
purpose the user picks in the brief. It is sent as `room_type` on
`POST /pipeline` and decides:

- which furniture a fresh design must include, and which it may never include;
- the furnishing policy that the interpret, selection and final review models read;
- which layout checks run, and which of them block a layout;
- how the solver groups and places the pieces.

There is no room registry. Each stage branches on the room name, so a new type
touches every place below. This guide uses `home_office` as the example and
`dining_room` as the model to copy. Line numbers are from branch
`issue-14-final-review`.

## Touch points

In pipeline order. "Required" means the request fails, or the room silently
gets living-room behavior, without it.

| # | Stage | File | What to add | Need |
| --- | --- | --- | --- | --- |
| 1 | Request contract | `pipeline/app/contracts.py:16` | Name in `RoomType` | Required |
| 2 | Room policy | `pipeline/app/rules/room_policy.py:7-8`, `:28` | Name in `RoomType` and `SUPPORTED_ROOM_TYPES`; a policy string like `DINING_FURNISHING_GUIDANCE` | Required |
| 3 | Room policy | `room_policy.py:93` `excluded_room_categories` | Categories the room may never hold | Required |
| 4 | Interpret | `pipeline/app/shared_stages.py:116` `_intent_prompt` | A policy bullet, like dining at `:175-187` | Required |
| 5 | Feasibility digest | `pipeline/app/rules/planner/feasibility_digest.py:151-336` | Anchor role, required groups, optional categories, caps, notes | Required |
| 6 | Fit estimate | `pipeline/app/rules/planner/fit_check.py:160` `_recommend_counts` | Categories the estimate must never cut | Optional |
| 7 | Seed guidance | `pipeline/app/rules/planner/seed_guidance.py:207-231` | `room_mode` and `primary_anchor_uid` | Required |
| 8 | Catalog slots | `pipeline/app/rules/selection/validation.py:134` `room_requirements` | Required items | Required |
| 9 | Selection prompt | `pipeline/app/variant_stages.py:372` `_selection_prompt` | Anchor name, role guidance, size, optional-piece and rug text | Required |
| 10 | Selection guidance | `pipeline/app/rules/layout_rules.py:216` `build_selection_guidance_block` | A `FIT FOR SELECTION` block | Optional |
| 11 | Selection validation | `validation.py:253` `validate_selection` | Room flag, group errors, opt out of living-room requirements | Required |
| 12 | Anchors | `variant_stages.py:655` `_ANCHOR_CATEGORIES` | Anchor category sets in priority order | Required |
| 13 | Variant directions | `variant_stages.py:98` `_ROOM_DIRECTIVES` | Two direction texts, variants 1 and 2 | Optional |
| 14 | Layout rules text | `layout_rules.py:144` `build_rules_block` | An `APPLICABILITY` block, like dining at `:177` | Required |
| 15 | Layout checks | `pipeline/app/rules/layout/analysis.py:119-124`, new `layout/home_office.py` | A measurement function and its issue keys | Optional |
| 16 | Issue tiers | `layout/constants.py:114`, `:148`; `layout/formatting.py:17-25`; `layout/metrics.py:100`, `:175` | Register the new issue keys | Required with 15 |
| 17 | Solver | `pipeline/app/rules/layout/solver.py:160`, `:303`, `:710` | `_STORAGE_FRONTS`, `_groups`, `_preference` | Optional |
| 18 | Final review prompt | `variant_stages.py:1303` `_arrangement_prompt` | Nothing, unless its design rules (`:1334`) miss a zone | Optional |
| 19 | Web brief | `web/src/lib/types.ts:4`, `web/src/lib/room.ts:102`, `:111` | `RoomType`, `ROOM_TYPES`, four `PROMPT_STARTERS` with sizes | Required |
| 20 | Benchmark, tests | `pipeline/scripts/benchmark_requests.json`, `pipeline/tests/` | One case and focused tests | Required |

## Steps

1. **Add the name to the contracts.** `RoomType` is declared in
   `contracts.py:16`, `room_policy.py:7` and `types.ts:4`. They must match, or
   the brief gets HTTP 422. `IntentPacket.room_type` (`shared_stages.py:60`)
   reads `SUPPORTED_ROOM_TYPES`, so the interpret schema follows.

   ```python
   RoomType = Literal["living_room", "bedroom", "dining_room", "studio", "home_office"]
   SUPPORTED_ROOM_TYPES = ("living_room", "bedroom", "dining_room", "studio", "home_office")
   ```

2. **Write the policy and exclusions** in `room_policy.py`. Keep the policy as
   one string, as `DINING_FURNISHING_GUIDANCE` does: core group, default counts,
   opt-in pieces, what to drop first when space is short. Then exclude what the
   room may never hold:

   ```python
   if normalize_room_type(room_type) not in {"bedroom", "studio"}:
       excluded.add("bed")
       if normalize_room_type(room_type) in {"dining_room", "home_office"}:
           excluded.add("daybed")
   ```

3. **Tell the interpret model.** Add a bullet to `_intent_prompt` that injects the
   policy only for this room, and say how prose counts map to `requested_items`
   ("desks for two" -> `desk` count=2, exact=true), as dining does for chairs:

   ```python
   - For a home office, use this furnishing policy:
     {HOME_OFFICE_FURNISHING_GUIDANCE if room_type == "home_office" else "Not applicable to this room type."}
   ```

4. **Shape the feasibility digest.** Add a flag beside
   `dining = room_type == "dining_room"` (`:153`), then handle it at each branch:
   `:183` block `excluded_room_categories`; `:212-213` anchor role (for example
   `"desk"` with `{"desk"}`); `:224-228` companion groups, as dining adds
   `dining_seating`; `:230` whether a living-room `surface` group applies;
   `:280-289` `optional_categories`; `:309-311` caps; `:334-335` completion notes.

5. **Set the seed room mode** in `build_seed_guidance` (`:218`, `:231`), for
   example `"work"` with the desk as `primary_anchor_uid`. `desk` and
   `office_chair` already have roles and placement modes (`:316-319`).

6. **Declare the required items** in `room_requirements`. `plan_slots`
   (`shared_stages.py`) makes one `required` search slot per entry; an item
   with no entry is never searched unless the user asks for it.

   ```python
   if room_type in {"dining_room", "studio"}:
       need("dining_table", {"dining_table"})
       need("dining_seating", {"dining_chair"}, dining_chair_count({**intent, "room_type": room_type}))
   if room_type == "home_office":
       need("desk", {"desk"})
       need("work_seating", {"office_chair"})
   ```

   Then decide the rug (`:170`) and the design-only plant (`:176`).

7. **Write the selection prompt branches.** In `_selection_prompt`, add the flag
   beside `dining` (`:388`), then extend `anchor_name` (`:400`),
   `required_role_guidance` (`:401-437`), the guidance overrides (`:481-488`)
   and `rug_guidance` (`:489-493`). The dining branch is one line:

   ```python
   "Apply the dining furnishing policy in the feasibility digest: keep the usable table/chair group and access ahead of optional additions."
   if dining else
   ```

   Add a `build_selection_guidance_block` branch when the room has fit facts the
   selector can act on, as dining does with seat pitch (`:256`).

8. **Enforce the selection** in `validate_selection`. See "Selection rule" below.

9. **Register anchors** with `"home_office": ({"desk"},),` in
   `_ANCHOR_CATEGORIES`. The constraint audit (`:728`), the drop and budget
   repairs (`:879`, `:915`) and the final-check drop (`:1443`) keep these pieces.

10. **Add variant directions** to `_ROOM_DIRECTIVES`, two strings.

    Also add a `"home_office"` key to `DESIGN_RULES` in
    `app/rules/layout_rules.py`: plain-text comfort rules the final review
    applies, for example "The desk faces the room or a window, not a wall
    corner." A room type without a key gets only the shared rules.

11. **Write the layout rules text.** Add `elif room_type == "home_office":` to
    `build_rules_block`: what anchors the room, what is not required, and the
    access rules. Selection and the final review prompt (`:1345`) both read it.

12. **Add layout checks** when the room has geometry rules no shared check
    covers. Model them on `layout/dining.py:114`: a read-only function that
    returns `{"violations": {key: [] for key in HOME_OFFICE_ISSUE_KEYS}}`. Call
    it from `analyze_layout`, then register the keys (row 16). A desk and task
    chair are already checked in every room by
    `compute_task_chair_orientation_violations` (`validation_functional.py:527`).

13. **Teach the solver only what it misses.** `_groups` already builds a `work`
    group from `desk` and `office_chair` (`solver.py:365-367`, `:383-384`). Add
    the room to `_STORAGE_FRONTS` when your checks measure storage fronts, and
    add costs in `_preference`.

14. **Add the room to the web brief.** `draftFromParams` (`room.ts:203`) rejects
    a room missing from `ROOM_TYPES`.

    ```ts
    { value: "home_office", label: "Home office" },  // ROOM_TYPES
    { roomType: "home_office", label: "Warm minimal", width: 3.2, length: 3.6,  // PROMPT_STARTERS
      prompt: "A warm, minimal home office in oak: a desk facing the window, a supportive task chair, a bookcase and a floor lamp." },
    ```

15. **Add a benchmark case** to `benchmark_requests.json` with `label`,
    `geometry` (one sentence on doors and windows), `note` and a full `request`,
    copied from the `dining_room` case.

## Writing the room's rules

| Kind | Effect | Where | Example |
| --- | --- | --- | --- |
| Selection rule | Fails the selection; the model retries with the error text | `validate_selection` | `DINING GROUP` |
| Layout check | Blocks the layout (P0, P1, `critical_p2`) or flags it (other P2, `non_blocking_findings`) | `layout/<room>.py`, tiers in `layout/constants.py` | `dining_completeness_violations` blocks; `dining_rug_violations` flags |
| Solver preference | Orders candidate poses; never fails anything | `_preference` | `DINING_WALL_COST` |

**Selection rule.** Append a model-facing error that names the fix, and set
`is_valid = False`. From `validation.py:452-458`:

```python
if table_count != 1 or not minimum_count <= chair_count <= target_count:
    errors.append(
        f"DINING GROUP: Select exactly one standalone dining_table and {seat_range} dining_chairs; "
        f"found {table_count} tables and {chair_count} chairs. Preserve the requested seating count."
    )
    is_valid = False
```

Advice goes into `warnings`, as `MISSING RECOMMENDED` does (`:509-516`). Keep
`room_requirements` and `validate_selection` in step: each required entry needs
a matching error, or slots and validation disagree.

**Layout check.** A measurement returns findings under issue keys; the tier
lists decide the effect. `findings_by_level` (`analysis.py`) puts keys from
`CRITICAL_P2_ISSUE_KEYS` into `critical_p2`, which `_measure`
(`variant_stages.py:1073-1079`) counts as blocking. From `dining.py:130-134`:

```python
if len(tables) != 1 or not chairs:
    result["violations"]["dining_completeness_violations"].append({
        "kind": "incomplete_dining_group", "table_count": len(tables), "chair_count": len(chairs),
        "uids": [item.uid for item in tables + chairs],
    })
```

That key is in `DINING_CRITICAL_ISSUE_KEYS` (`constants.py:86`) and blocks;
`dining_rug_violations` is only in `DINING_ISSUE_KEYS` (`:94`) and flags. Block
only what makes the room unusable.

**Solver preference.** Add a cost for a less preferred candidate. Costs only
rank candidates that pass the solver's cheap checks. From `solver.py:773-776`:

```python
if group.kind == "dining" and ctx.room["room_type"] in {"studio", "living_room"}:  # its own part of the room, by a wall
    cost += DINING_WALL_COST * min(ctx.polygon.exterior.distance(polygon) for _, polygon in floors)
elif group.kind == "dining":
    cost += 0.1 * math.dist(poses[anchor][:2], ((min_x + max_x) / 2, (min_y + max_y) / 2))
```

Mirror a layout check in a cost when the solver should avoid what the check
flags; the sitting-rug cost (`:756-764`) mirrors a comfort check.

## Verify

Run from `pipeline/` with `conda activate livinit`.

1. Add unit tests modeled on the dining ones:
   - Like `tests/test_rules_parity.py:159`: `validate_selection` passes the core
     group and fails without it.
   - `room_requirements` returns the room's required roles and no `bed`.
   - Like `tests/test_solver.py:44`: a `home_office(...)` helper builds the
     benchmark room with `build_room_context`; `solve_layout` places every item
     with no blocking findings (`blocking`, `:31`).
   - One test per new blocking layout key.

   ```bash
   python -m pytest tests/test_rules_parity.py tests/test_solver.py tests/test_stages.py
   ```

2. Run the case end to end against the local service. It prints `ready_of_3`
   and the `run_id`:

   ```bash
   python scripts/benchmark.py --only home_office --runs 1
   ```

3. Score it. The report goes to `pipeline/.data/reviews/<timestamp>/`:

   ```bash
   python scripts/review.py --runs <run_id>
   ```

4. In the web brief, pick the room and each starter, and check that the request
   is accepted.

## Pitfalls

- **Unknown rooms fall through to the living room**, with no error:
  - `validate_selection` requires sofa, surface and rug unless the room is
    dining or studio (`validation.py:489-492`), and warns about missing media
    unless bedroom or dining (`:509-510`). The new room fails with
    `MISSING REQUIRED: No anchor seating` until you add it.
  - The digest picks `anchor_seating` as the anchor (`feasibility_digest.py:212`)
    and adds a surface group (`:230`).
  - The selection prompt says a sofa and a surface are mandatory
    (`variant_stages.py:436`).
  - `_ANCHOR_CATEGORIES.get(..., (FIT_ANCHOR_SEATING,))` protects a sofa, not
    your anchor (`:728`, `:879`, `:915`, `:1443`).
  - `build_seed_guidance` falls back to a living-room `_room_mode` (`:218`).
  - Variants 1 and 2 keep the generic texts without `_ROOM_DIRECTIVES` (`:120`).
- **Room exclusions are applied only for listed rooms.** The digest blocks
  `excluded_room_categories` only for bedroom, dining and studio (`:183`).
- **`bedroom` in `validate_selection` also means studio** (`:399`). Add a new
  flag; do not reuse `bedroom` or `dining`.
- **Studio-only group names.** A selection item's `functional_group` allows only
  `sleeping`, `sitting`, `dining` (`variant_stages.py:277`), and so does
  `accessory_group` (`layout/studio.py:24`).
- **The sitting rug joins the sofa group only in a living room** (`solver.py:333`).
  Elsewhere it is placed as a free piece.
- **Dining handling lists rooms by name.** The fit estimate
  (`fit_check.py:169`, `:190`) and the digest caps (`:309`) list
  `dining_room` and `studio`; add the new room if it has a dining group.
- **The three `RoomType` lists are not checked against each other.**
