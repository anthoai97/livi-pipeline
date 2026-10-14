# Adding a product category

A product category (`sofa`, `sideboard`, `dining_chair`, `wall_art`) is the
canonical name the rules use for a kind of furniture. It decides:

- whether the interpret model can ask for it, and which catalog labels are
  searched for it;
- how many pieces a room may hold, and whether it gets a search slot when the
  brief does not ask for it;
- whether it stands on the floor, hangs on a wall or ceiling, or sits on another
  piece;
- which group the solver places it in, and what space it keeps clear.

There is no category registry. A category is a name in a few lists, and its
behavior comes from the role and placement mode those lists give it. This guide
uses `credenza` as the worked example and a kitchen island with stools as a
sketch. It is the companion to [Adding a room type](adding-a-room-type.md).
Line numbers in the tables are from `main` on 2026-10-11 and
drift; search for the named symbol.

## Where a category's rules go

Put each rule in the lightest place that works. Every fit rule lives in
`pipeline/app/rules/layout`: the solver plus the layout checks. Never write a
fit rule as a size estimate in selection or as a prompt-only rule. The selection
fit check and place run the solver, and the final check runs the same layout
checks, so a rule there holds at every step.

| Kind of rule | Where | Example |
| --- | --- | --- |
| Name and catalog spellings | `CANONICAL_CATEGORIES` and `CATEGORY_ALIASES` in `taxonomy.py` | `credenza`; `armoire` -> `wardrobe` |
| Counts and optional search | A role in `ROLE_CATEGORIES` (`taxonomy.py`) | `sideboard` is `storage` |
| How it mounts | `placement_type` from catalog preparation, plus the lists in `categories.py` | `wall_art` hangs on a wall |
| Arrangement with other pieces | A solver group template in `layout/solver.py` | Dining chairs at offsets around the table |
| Space it needs | A layout check in `rules/layout` with an issue tier, mirrored by a solver service band | Dining chair pull-out, storage front |
| Taste | A `DESIGN_RULES` sentence in `layout_rules.py`, read by the final review | "Stools tuck under the island overhang." |

## How a category moves through the pipeline

1. **Catalog preparation.** `prepare_assets.py` asks a model to pick one
   category from the `catalog.assets` labels (`load_categories`), so a label that
   is not in the catalog never appears. The fallback placement is `wall`,
   `ceiling` or `surface` for the listed categories and `floor` for the rest.
   [Product categories](../data/product-categories.md) lists the labels and
   their counts.
2. **Canonical name.** `normalize_category` maps a prepared label to a
   canonical one: aliases first, then the canonical list, then a plural with
   the trailing `s` dropped. The interpret schema accepts only
   `CANONICAL_CATEGORIES` (`Category` in `shared_stages.py`).
3. **Role.** `ROLE_CATEGORIES` gives each canonical category one of 11 roles:
   `sleeping_anchor`, `bedside`, `anchor_seating`, `accent_seating`, `surface`,
   `media`, `storage`, `lighting`, `tabletop`, `rug`, `wall_or_ceiling`. The
   feasibility digest caps each category by its role and room size
   (`_max_counts_by_category`), and the fit estimate sizes it by its role
   (`ROLE_PROFILES`).
4. **Search slots.** `plan_slots` makes one slot per requested item, per
   required item (`room_requirements`), per optional role and per decor
   category. Optional roles come from `COUNT_GUIDANCE_ROLE_CATEGORIES`, less
   room exclusions, intent exclusions and zero caps. A slot searches its
   canonical categories plus every alias that maps to them
   (`_prepared_categories`).
5. **Placement mode.** A selected product's `placement_mode` comes from its
   prepared `placement_type`: `floor`, `surface` (`tabletop`), `wall`
   (`wall_mounted`), `ceiling` (`ceiling_mounted`). A record
   with no `placement_type` falls back to the category lists in
   `categories.py`. Selection rejects a product whose mode stays `unknown`.
6. **Solver.** `build_seed_guidance` gives each piece a seed role
   (`_classify_asset_role`), and `build_seed_layout_plan` turns the seed role
   into a seed mode such as `support_wall` or `around_dining_table`. `_groups`
   builds the solver groups from the seed roles; a piece in no group is placed
   alone, against a wall when its seed mode is a wall mode, else anywhere. Wall,
   ceiling and tabletop pieces, a TV on a stand, and rugs outside the sofa group
   are left to the accessory pass.
7. **Layout checks.** `analyze_layout` measures the layout, and the tier lists
   in `layout/constants.py` decide which findings block it.

## Pick the case

Most categories are case (a). If the new label is only another name for an
existing category, for example `buffet` for `sideboard`, add an alias and stop.

| Case | When | What to add |
| --- | --- | --- |
| (a) Data only | The piece behaves like an existing role: a credenza is storage against a wall, a piano bench is accent seating | Canonical name, aliases, role, placement lists |
| (b) New arrangement | The piece only works with other pieces in a fixed relation: an island with stools along one edge, a piano with its bench in front | (a), plus a seed role and a solver group template with offsets and service bands, following `_dining_templates` and `_work_templates` |
| (c) New spatial rule | The piece needs space no check measures: stool pull-out behind an island, a crib side kept clear | A layout check in `rules/layout` plus its issue tier, and the matching solver band |

Case (b) usually needs (c) as well. A solver band only steers the search; the
final check measures only what a layout check measures.

## Touch points

In pipeline order. "Required" means the category is not searched, not capped,
or placed as a generic floor piece without it.

| # | Stage | File | What to add | Need |
| --- | --- | --- | --- | --- |
| 1 | Catalog vocabulary | `product-data/src/prepare_assets.py:342` `load_categories` | The label in `catalog.assets` | Required for a new label |
| 2 | Catalog placement | `prepare_assets.py:55`, `:64`, `:65`; `:285` `fallback_placement` | Wall, ceiling or surface categories | Required unless floor |
| 3 | Category list | `docs/data/product-categories.md` | Refreshed counts | Optional |
| 4 | Canonical name | `pipeline/app/rules/planner/taxonomy.py:8` `CANONICAL_CATEGORIES` | The name | Required |
| 5 | Aliases | `taxonomy.py:61` `CATEGORY_ALIASES` | Every catalog spelling that is not the canonical name or its plural | Required when spellings differ |
| 6 | Role | `taxonomy.py:109` `ROLE_CATEGORIES` | The name under one role | Required |
| 7 | Optional slots, counts | `taxonomy.py:180` `COUNT_GUIDANCE_ROLE_CATEGORIES`, `:193` `COUNT_CONSTRAINT_EQUIVALENTS` | The name, where its role uses a fixed set; the sibling group | Optional |
| 8 | Placement and keyword lists | `pipeline/app/rules/categories.py:45` `FLOOR_ONLY_CATEGORIES`, `:165` `WALL_MOUNTED_CATEGORIES`, `:175` `CEILING_MOUNTED_CATEGORIES`, `:189` `WALL_ALIGNED_CATEGORY_KEYWORDS`, `:99` and `:108` support lists | The name in the lists that describe it | Required for wall and ceiling pieces |
| 9 | Placement mode | `pipeline/app/rules/placement_mode.py:26`, `:43` | Nothing: reads rows 2 and 8 | None |
| 10 | Seed role | `pipeline/app/rules/planner/seed_guidance.py:18`, `:19` `_ROLE_PRIORITY`, `:78` `_classify_asset_role`, `:283` seed modes | A seed role and seed mode | Required for (b) |
| 11 | Solver | `pipeline/app/rules/layout/solver.py:159` `ZONE_KINDS`, `:162` `_STORAGE_FRONTS`, `:313` `_groups`, `:491` `_dining_templates`, `:535` `_work_templates`, `:554` `_placements`, `:732` `_preference` | Front bands (a); a group, its templates and candidates (b) | Optional; required for (b) |
| 12 | Layout checks | `pipeline/app/rules/layout/analysis.py:53-76`, `:118-175`; `layout/bedroom.py:185`, `layout/dining.py:194` | A measurement and its issue keys; storage-front lists | Required for (c) |
| 13 | Issue tiers | `layout/constants.py:118` `ISSUE_KEYS_BY_TIER`, `:152` `CRITICAL_P2_ISSUE_KEYS`; `layout/metrics.py:78` `serialize_layout_issues` | Register the new keys | Required with 12 |
| 14 | Caps | `pipeline/app/rules/planner/feasibility_digest.py:69` `_max_counts_by_category`; `fit_policy.py:35` `ROLE_CAPS`, `:74` `CATEGORY_CAPS` | Nothing, or a per-category cap | Optional |
| 15 | Required items | `pipeline/app/rules/selection/validation.py:134` `room_requirements`, `:253` `validate_selection` | A required entry and its matching error | Optional |
| 16 | Room exclusions | `pipeline/app/rules/room_policy.py:74`, `:93` `excluded_room_categories` | Rooms that may never hold it | Optional |
| 17 | Budget | `taxonomy.py:59` `NON_SELLABLE_CATEGORIES` (`BUDGET_EXCLUDED_CATEGORIES` in `selection/constants.py:13`) | Only for pieces never bought, like `tv` | Optional |
| 18 | Decor slots | `pipeline/app/shared_stages.py:273` `DECOR_CATEGORIES` | Decor that gets a slot without a request | Optional |
| 19 | Taste | `pipeline/app/rules/layout_rules.py:73` `DESIGN_RULES` | A sentence for the final review | Optional |
| 20 | Tests, benchmark | `pipeline/tests/test_solver.py`, `pipeline/scripts/benchmark_requests.json` | Solver tests; a case for a new room composition | Required |

## Add a credenza (case a)

A credenza is low storage that stands against a wall, like a sideboard. The
catalog has no `credenza` label today: credenzas are filed under `sideboard`,
`buffet` or `media_console`. If a credenza should act exactly like a sideboard,
add `"credenza": "sideboard"` to `CATEGORY_ALIASES` and stop. The steps below
make it its own category.

1. **Add the label to the catalog.** `prepare_assets.py` offers the model only
   the labels in `catalog.assets`, so `credenza` must be one of them. Then
   reprepare the affected rows with `--refresh` and rerun `embed_assets.py`,
   which re-embeds rows whose text changed. A credenza needs no placement entry:
   `fallback_placement` returns `floor` for any category not in the wall,
   ceiling or surface lists.

2. **Name it and give it a role** in `taxonomy.py`:

   ```python
   CANONICAL_CATEGORIES = (..., "sideboard", "credenza", ...)
   ROLE_CATEGORIES = {..., "storage": {"bookcase", "cabinet", "storage_unit", "sideboard", "credenza", "dresser", "wardrobe"}, ...}
   ```

   The plural `credenzas` needs no alias. Add `credenza` to the
   `storage_unit` set in `COUNT_CONSTRAINT_EQUIVALENTS` so a brief that asks
   for "two storage pieces" counts it.

   The role gives the rest for free:
   - The interpret model can request `credenza`.
   - The digest caps it with the storage cap: 1, 2 or 3 by room size, and 1
     under high opening pressure.
   - `COUNT_GUIDANCE_ROLE_CATEGORIES["storage"]` is the whole storage role, so
     the optional storage slot searches it.
   - `seed_guidance._WALL_STORAGE_BUCKET` is built from the storage role, so the
     seed role is `wall_storage`, the seed mode `support_wall`, and the solver
     places it alone against a wall.

3. **Describe it in `categories.py`.** Add `credenza` to:
   - `FLOOR_ONLY_CATEGORIES`, so the category fallback says `floor`;
   - `WALL_ALIGNED_CATEGORY_KEYWORDS`, so normalization snaps it to its wall and
     the P1 wall-aligned check requires it flush, as for `sideboard`;
   - `TABLETOP_SUPPORT_CATEGORIES` and `TABLETOP_STORAGE_SUPPORT_CATEGORIES`, so
     vases and sculptures can stand on it.

4. **Keep its front clear where a room measures storage fronts.** The bedroom
   check measures `dresser`, `cabinet`, `wardrobe`, `storage_unit` and
   `sideboard` (`bedroom.py`), and the dining check measures `sideboard`,
   `cabinet` and `storage_unit` (`dining.py`). Add `credenza` to the sets the
   room should check, and to the same rooms in `_STORAGE_FRONTS`
   (`solver.py`), which reserves a 0.56 m band in front of it (`STORAGE_FRONT_M`). The two lists
   must match: a band with no check only steers the search, and a check with no
   band makes the solver find the problem late. A living room measures no
   storage front, so a credenza there gets no band.

5. **Decide the optional rules.** Add `credenza` to
   `PERIMETER_STORAGE_CATEGORIES` (`selection/constants.py`) if it may fill the
   storage a spacious living room requires. Add a `DESIGN_RULES` sentence if the
   final review should know where it looks right.

Nothing else changes: the solver places it against a wall with its front band, and the budget counts its price.

## Sketch: a kitchen island with stools (case b)

An island stands free, with stools along one long edge, facing it. No code
exists for this; the steps below name the functions to change.

1. **Taxonomy.** Add `kitchen_island` to the `surface` role, as `dining_table`
   is. Add a `counter_stool` category with `bar_stool` as an alias, in
   `accent_seating` for caps. Do not alias the stools to `stool`; step 2
   explains why.
2. **Seed roles.** Every `accent_seating` category gets the seed role
   `conversation_support`, and in a room with a sofa and a coffee table
   `_groups` adds those seats to the sofa group. Give the island and its stools
   their own seed roles, as `dining_table` and `dining_chair` have:
   add them to `_ROLE_PRIORITY`, return them in `_classify_asset_role` before
   the `accent_seating` branch, and give them seed modes in
   `build_seed_layout_plan`.
3. **Group.** In `_groups`, pick the island and its stools after the dining
   block, `take` them, and append `_Group("island", _island_templates(...))`.
   Model `_island_templates` on `_dining_templates`: the island at the origin,
   stools at even spacing along one long edge, each offset by half the island
   depth plus a gap plus half the stool depth, facing the island, with a
   `(stool, "back")` band for pull-out.
4. **Candidates.** `_placements` puts every group kind it does not list on the
   walls. An island stands free, so give `"island"` the grid branch that
   `"dining"` uses. Add `"island"` to `ZONE_KINDS` so kept layouts differ in
   where the island goes, and add a cost in `_preference` only if the review
   keeps flagging a placement.
5. **Check (case c).** Add a measurement in `rules/layout`, modeled on
   `dining_layout_measurements`: each stool next to the island and facing it,
   and its pull-out band clear. Register its keys (rows 12 and 13), blocking
   only the stool that cannot be used.

## Verify

Run from `pipeline/` with `conda activate livinit`.

1. Add solver tests in `tests/test_solver.py`, built with the `item()` helper and
   checked with `blocking()`:
   - Case (a): a credenza in the benchmark dining room (`dining_room(...)`)
     lands against a wall, its front band is clear, and `blocking()` is empty.
   - Case (b): the group template puts each stool along the island edge,
     facing it, in a small and a large room, with nothing unplaceable.
   - Case (c): one test per new blocking key, failing and passing.

   ```bash
   python -m pytest tests/test_solver.py tests/test_rules.py tests/test_stages.py
   ```

2. If the category brings a room composition no benchmark case has, such as a
   studio with a kitchen island, add a case to `scripts/benchmark_requests.json`
   and run it end to end against the local service:

   ```bash
   python scripts/benchmark.py --only studio_island --runs 1
   python scripts/review.py --runs <run_id>
   ```

## Pitfalls

- **The taxonomy and the role map are both needed.** A canonical name with no
  role can be requested, but it gets no cap, no optional slot, and the seed
  role `generic_floor`: the solver places it alone anywhere, near a corner.
  `ROLE_CATEGORIES` holds canonical names only; an alias there does nothing.
- **Some roles get no optional slot.** `COUNT_GUIDANCE_ROLE_CATEGORIES` uses
  fixed sets for `surface`, `lighting` and `media`, and has no `tabletop` or
  `wall_or_ceiling` entry; of the wall and ceiling pieces only pendants and
  chandeliers (`overhead_lighting`, dining rooms and studios) and wall art
  (`DECOR_CATEGORIES`) get a slot. A new piece in those roles is searched only
  when the brief asks for it, unless you add it to the set or to
  `DECOR_CATEGORIES`.
- **A catalog label with no alias is never searched.** A slot searches only its
  canonical names and their aliases. Today `buffet`, `counter_stool`,
  `bar_stool` and `chest_of_drawers` are catalog labels that map to no
  canonical category, so no slot finds them.
- **Wall and ceiling pieces need a placement.** A real product gets its mode
  from the prepared `placement_type`, and a record without one falls back to the
  `categories.py` lists. A product with no clear placement fails selection with
  `PLACEMENT SEMANTICS UNRESOLVED`.
- **Tabletop pieces need a support.** The solver leaves them to the accessory
  pass, which puts them on a piece in `TABLETOP_SUPPORT_CATEGORIES`. A new
  support category missing from that list holds nothing, and a tabletop piece
  with no support gets a `table_lamp_support_violations` finding.
- **Keyword lists match substrings of the category and the instance key.** The
  `shelf` keyword also matches `wall_shelf`, and `print` matches
  `canvas_print`. Check what else a new keyword catches.
- **The seed role, not the role, picks the solver group.** A stool is
  `accent_seating`, so it joins the sofa group as a conversation seat in any
  room with a sofa and a coffee table.
- **Placement still uses the bounding rectangle.** `build_room_facts` reports
  every wall of the outline with its real length (`"shape": "polygon"` for an
  L or a T), and the selection model sees those. The solver's wall candidates
  still come from the four box walls, and the real outline filters them, so the
  walls of the notch are never candidates until #53 phase 3. A wall piece can
  find fewer spots than the facts suggest. The checks use the real outline.
- **Room exclusions match the prepared label.** `asset_is_eligible_for_room`
  compares the raw prepared category, while the digest uses canonical names.
  List every prepared spelling in `excluded_room_categories`, as
  `DEFERRED_SLEEP_CATEGORIES` lists `bed_frame` and `bunk_bed` beside `bed`.
- **Every piece counts toward the budget** unless its category is in
  `NON_SELLABLE_CATEGORIES`.
