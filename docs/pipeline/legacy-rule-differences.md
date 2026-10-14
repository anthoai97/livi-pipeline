# Room rules: legacy pipeline vs current

Compared on 2026-10-14: legacy `livinit_pipeline` `trunk` at `6d738f5` (rule
files last changed 2026-10-08) against `livi-pipeline` `main` at `d3ea93f`.
The rules tree was ported on 2026-10-09; the differences below come from
#54 (room rules, 2026-10-12 to 13) and from the solver re-architecture
(#5, #45, #52). Import renames and refactors that keep behavior are left out.

## 1. Thresholds (#54 part 1)

| Rule | Legacy | Current | Where |
| --- | --- | --- | --- |
| Sofa to coffee table | 0.46 to 0.61 m | 0.38 to 0.50 m | `layout_rules.py`, solver gaps, P2 critical check |
| Sofa back to wall | 0.08 to 0.15 m; flush flagged | 0 to 0.15 m | `layout_rules.py`, `validation_functional.py` |
| Secondary path | 0.56 m | 0.60 m | `layout_rules.py` |
| Bed side access | 0.56 m, one side | 0.60 m; both sides for beds 1.4 m+ unless compact | `layout/bedroom.py` |
| Bed foot access | 0.56 m | 0.60 m; 0.91 m when the foot strip is the route to a door | `layout/bedroom.py` |
| Storage front (dresser, wardrobe, cabinet, sideboard) | 0.56 m | 0.76 m | `layout/bedroom.py`, `dining.py`, solver bands |
| Dining chair pull-out | 0.56 m every chair | 0.46 m; 0.56 m when a door zone or path is behind | `layout/dining.py`, solver (always 0.46) |
| Desk chair pull-out | 0.56 m, bedroom and studio only | 0.60 m, every room (critical P2 in living and dining rooms too) | `layout/bedroom.py`, `analysis.py` |
| Door clear zone | opening width x 0.60 m deep | opening width x opening width (0.9 m default) | `door_geometry.py` |
| Wall art height (`WALL_MOUNT_Z`) | 1.60 m | 1.50 m | `categories.py` |
| TV center, bed viewing | 1.10 m | 1.30 m | `layout/constants.py`, `relations.py` |
| TV viewing distance | 1.2 to 3.5 x screen width | 1.5 to 2.8 x screen width | `layout/comfort.py`, solver cost |
| Sitting rug width | 90% of sofa width | 100% of sofa width | `layout/comfort.py` |
| Dining rug | covers 95% of table and chairs | extends 0.60 m past each table edge | `layout/dining.py` |
| Pendant over table | at least 0.76 m | 0.76 to 0.90 m (above 0.90 advisory); 0.15 m inside each table edge | `layout/dining.py`, selection preflight |
| Table seating | any depth seats both long sides | under 0.75 m deep seats one long side only | `layout/dining.py`, selection |
| Ceiling height | fixed 2.8 m | the request's `wall_height` (2.8 m default) | `categories.py`, mounts, normalization |
| Tall piece in front of a window | prompt text only | advisory P2 finding (`tall_piece_in_front_of_window`) | `validation_geometry.py` |
| Floor lamp reach | nearest seat of any kind | lounge seats; dining chairs only when there is no other seat | `validation_functional.py` |
| Room size class (compact / medium cutoffs) | 18 / 30 m² for every room | living 18 / 30, bedroom 11 / 16, dining 10 / 16, studio 25 / 40 | `planner/_util.py` |

Bug fixes made in the same change (keep in any revert): `tv_stand` no longer
counts as a TV display; a studio without a dining table no longer gets a
critical `incomplete_dining_group`; a studio storage front is reported once;
`bed_count != 1`; the dining light gap is checked for studios.

## 2. What a room must contain (#54 part 2)

Legacy required only the core group per room; everything else was a prompt
suggestion. Current `room_requirements` blocks selection on these extra pieces
(waived when the brief excludes them, asks for minimal or space-first, or the
candidate pool has none).

| Room | Legacy required | Current required |
| --- | --- | --- |
| Living | sofa; a surface (coffee table, side table or desk); rug; plant. Lighting advisory | same minus desk as a surface; plus a lamp; plus one accent seat when spacious |
| Bedroom | bed; rug; plant. Wardrobe, TV, desk opt-in | plus a nightstand and a bedside lamp; a nightstand per side for beds 1.4 m+ in a spacious room; a dresser or wardrobe in medium and spacious rooms |
| Dining | table; 4 chairs. Sideboard, pendant, art "suggestions" | plus a plant; in medium and spacious rooms a sideboard or cabinet and a pendant or chandelier; 6 chairs by default when spacious (minimum 4) |
| Studio | bed; sofa; dining table with 2 chairs, always; plant optional | dining group only when asked for or the studio is 25 m²+; nightstand and bedside lamp required; dresser or wardrobe in medium and spacious studios |

Other policy text differences (`room_policy.py`, `layout_rules.py`):

- Legacy: a space-first dining brief may drop below 4 seats with a reason.
  Current: never cut the seat count; shrink the table instead.
- Legacy studio: "reserve the dining footprint first", "report infeasibility
  rather than silently losing a group". Current: bed, sofa and any dining table
  are anchors placed together; a failed check removes the item.
- Legacy bedroom text: prefer both sides clear, no detached or foot-end
  nightstands, bed and nightstand separation formula, "do not add inventory,
  empty floor is not a defect". Current: shorter numbered lines; the new
  `DESIGN_RULES` add "bed seen from the door but not in line with it" and the
  studio zoning lines (bed in the far corner, sofa back divides, dining by the
  entry wall).
- Sentences about existing-room edits, frozen scope, explaining omissions and
  reporting infeasibility were removed everywhere (no edit flow exists).

## 3. Density

| Rule | Legacy | Current |
| --- | --- | --- |
| Load unit | (w + 2c) x (d + 2c) x weight per role (clearance ring c: bed 0.6, sofa 0.55, chair 0.45, surface 0.35, storage 0.35) | w x d of floor pieces only; rugs, wall, ceiling and tabletop pieces count 0 |
| Comfortable / overcrowded / sparse | one set per size class: compact 0.52 / 0.72 / 0.30, medium 0.55 / 0.75 / 0.32, spacious 0.60 / 0.82 / 0.25 | per room type and size, e.g. living 0.22 / 0.30 / 0.15 compact, bedroom 0.42 / 0.50 / 0.15 compact (full table in `fit_policy.py`) |
| Selection overcrowding | 2 x footprint above the available area blocks | footprint above the room's maximum blocks; warning after a fit step |
| Overcrowded response | LLM fit recommendation picks what to relax | automatic steps: compact, then capped counts |

## 4. Legacy checks the current pipeline dropped

These come from the solver re-architecture, not from #54. Each is a separate
decision.

| Check | Legacy | Current |
| --- | --- | --- |
| Circulation (P1, blocking) | every door reachable from door 0 and every service band reachable from a door, 0.56 m wide | removed; only protected paths, door zones and the bed-foot route rule |
| Protected paths in non-rectangular rooms | routed inside the polygon; a missing 0.91 m route fails the room | straight bands across the bounding box, never fails (#53 scope) |
| Angled walls | every polygon edge is a wall with its own normal | only near-cardinal edges; angled pieces forbidden (#53 scope) |
| Selection preflight fit | blocked when the anchor seating, bed, rug, desk cluster or dining cluster had no placement; TV without a support blocked | solver decides; a TV with no fitting stand is wall-mounted |
| Generic `chair` with a dining table | counted as a dining chair | `dining_chair` category only (selection relabels) |
| Layout quality judge | LLM judge with per-room rule IDs after placement | none; the final review adjusts and code keeps passing moves |
| Deterministic cleanup and model repair loop | P0 cleanup, dining chair arrangement, up to 8 model repairs | the solver places; the review moves pieces; failing items are removed |
| Physical support check (`physical_vertical_support_violations`) | z and rotation sanity per placement | removed |

## 5. Not comparable

The legacy model placed furniture and code repaired it; the current solver
places by rules. Legacy prompt sentences that steered model placement
(furnishing regions, entry connectivity, "compare closer wall-backed
arrangements") have no current equivalent because no model places anything.
