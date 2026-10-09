# Product categories

Raw category labels in the source tables, checked on October 8, 2026.
`catalog.assets.category` is the furniture vocabulary.
`pipeline.decor_items.label` is a separate free-text label, not a catalog category.

`catalog.assets` has 36,765 products that are not deleted, in 288 categories.
4,995 of those products have a `model_url`, and those products use 167 of the categories.
`pipeline.decor_items` has 320 products with a `glb_url`, in 13 labels.

The preparation step uses the catalog names below as the category vocabulary.
A prepared decor item still needs one of those names, such as `planter` or `wall_mirror`.

Regenerate the counts from the remote database:

```sql
SELECT category, count(*)
FROM catalog.assets
WHERE NOT is_deleted
GROUP BY 1
ORDER BY count(*) DESC, 1;

SELECT label, count(*)
FROM pipeline.decor_items
WHERE NOT is_deleted AND coalesce(glb_url, '') <> ''
GROUP BY 1
ORDER BY count(*) DESC, 1;
```

## Catalog categories with at least 100 products

61 categories.

| Category | Products |
| --- | ---: |
| `accent_chair` | 2,063 |
| `dining_table` | 2,004 |
| `dining_chair` | 1,820 |
| `bed_frame` | 1,383 |
| `side_table` | 1,313 |
| `sofa` | 1,242 |
| `table_lamp` | 1,213 |
| `coffee_table` | 1,210 |
| `area_rug` | 1,045 |
| `nightstand` | 983 |
| `sectional` | 976 |
| `end_table` | 931 |
| `floor_lamp` | 924 |
| `sculpture` | 921 |
| `ottoman` | 878 |
| `counter_stool` | 742 |
| `cabinet` | 713 |
| `rug` | 703 |
| `vase` | 662 |
| `bar_stool` | 650 |
| `dresser` | 625 |
| `canvas_print` | 623 |
| `wall_art` | 590 |
| `bench` | 589 |
| `sideboard` | 588 |
| `loveseat` | 556 |
| `console_table` | 507 |
| `arm_chair` | 465 |
| `chandelier` | 432 |
| `sectional_sofa` | 418 |
| `decorative_bowl` | 401 |
| `pendant_light` | 386 |
| `outdoor_chair` | 364 |
| `candle_holders` | 361 |
| `chair` | 344 |
| `planter` | 341 |
| `bed` | 314 |
| `outdoor_table` | 277 |
| `floor_mirror` | 274 |
| `wall_sconce` | 274 |
| `chest_of_drawers` | 271 |
| `desk` | 267 |
| `wall_mirror` | 256 |
| `lounge_chair` | 240 |
| `reclinear` | 229 |
| `tv_stand` | 220 |
| `outdoor_sofa` | 198 |
| `bookcase` | 174 |
| `media_console` | 158 |
| `office_chair` | 153 |
| `storage_bench` | 132 |
| `throw_pillow` | 132 |
| `recliner` | 126 |
| `bar_cabinet` | 118 |
| `armchair` | 115 |
| `lamp` | 115 |
| `storage_box` | 113 |
| `daybed` | 107 |
| `mirror` | 106 |
| `footstool` | 105 |
| `sleeper_sofa` | 104 |

## Catalog categories with 10 to 99 products

53 categories.

| Category | Products |
| --- | ---: |
| `shelving_unit` | 94 |
| `print` | 81 |
| `sconce` | 80 |
| `stool` | 77 |
| `buffet` | 70 |
| `pendant` | 63 |
| `outdoor_accessories` | 61 |
| `pillow` | 61 |
| `curio_cabinet` | 56 |
| `outdoor_bench` | 53 |
| `table` | 53 |
| `storage_basket` | 51 |
| `storage_organizer` | 50 |
| `throw` | 48 |
| `pouf` | 47 |
| `writing_desk` | 47 |
| `accessories` | 43 |
| `wardrobe` | 41 |
| `bookshelf` | 37 |
| `glassware` | 36 |
| `sofa_bed` | 36 |
| `armoire` | 35 |
| `curtain` | 35 |
| `media_unit` | 35 |
| `chaise_lounge` | 33 |
| `bedroom_set` | 27 |
| `storage_unit` | 27 |
| `bunk_bed` | 26 |
| `headboard` | 25 |
| `hardware` | 22 |
| `picture_frame` | 21 |
| `bedding` | 20 |
| `blanket` | 20 |
| `bowl` | 20 |
| `mattress` | 19 |
| `tray` | 19 |
| `tv` | 19 |
| `furniture_cover` | 18 |
| `vanity` | 17 |
| `mount` | 16 |
| `storage_furniture` | 16 |
| `dinnerware` | 14 |
| `outdoor_furniture` | 14 |
| `accessory` | 13 |
| `panel` | 13 |
| `coat_rack` | 12 |
| `entryway_bench` | 12 |
| `flush_mount_lamp` | 11 |
| `living_room_set` | 11 |
| `umbrella_stand` | 11 |
| `wall_shelf` | 11 |
| `holder` | 10 |
| `settee` | 10 |

## Catalog categories with fewer than 10 products

174 categories. This set includes duplicate spellings and labels that are not product types, such as `reclinear`, `dinning`, `gift_card`, and `subscription`.

| Category | Products |
| --- | ---: |
| `bar_table` | 9 |
| `runner` | 9 |
| `shelf` | 9 |
| `shelf_unit` | 9 |
| `cover` | 8 |
| `covers` | 8 |
| `outdoor_chairs` | 8 |
| `shade` | 8 |
| `stand` | 8 |
| `storage` | 8 |
| `dinning` | 7 |
| `fire_pit` | 7 |
| `futon` | 7 |
| `shelving_system` | 7 |
| `umbrella` | 7 |
| `cart` | 6 |
| `flatware` | 6 |
| `modular` | 6 |
| `pot` | 6 |
| `base` | 5 |
| `basket` | 5 |
| `blind` | 5 |
| `box` | 5 |
| `glider` | 5 |
| `outdoor_furniture_set` | 5 |
| `rocking_chair` | 5 |
| `scented_candle` | 5 |
| `standing_desk` | 5 |
| `swivel_chair` | 5 |
| `tables` | 5 |
| `bucket` | 4 |
| `bulb` | 4 |
| `canvas` | 4 |
| `entertainment_unit` | 4 |
| `file_cabinet` | 4 |
| `hurricane` | 4 |
| `lantern` | 4 |
| `outdoor_furniture_sets` | 4 |
| `outdoor_lounge_chair` | 4 |
| `sofa_accessories` | 4 |
| `art` | 3 |
| `banquette` | 3 |
| `custom_furniture` | 3 |
| `dish` | 3 |
| `glass_door_cabinet` | 3 |
| `hook` | 3 |
| `outdoor_coffee_side_tables` | 3 |
| `platter` | 3 |
| `set` | 3 |
| `sofa_cover` | 3 |
| `sofa_with_chaise` | 3 |
| `unit` | 3 |
| `armrest` | 2 |
| `bar` | 2 |
| `bed_add_ons` | 2 |
| `bed_hardware` | 2 |
| `cabinet_door` | 2 |
| `calendar` | 2 |
| `candleholder` | 2 |
| `chiller` | 2 |
| `coffee_side_table` | 2 |
| `desk_chair` | 2 |
| `display_cabinet` | 2 |
| `door` | 2 |
| `fabric_by_the_yard` | 2 |
| `frame` | 2 |
| `furniture` | 2 |
| `glass` | 2 |
| `lighting_accessory` | 2 |
| `malta` | 2 |
| `outdoor_dining_table` | 2 |
| `outdoor_lounge_chairs` | 2 |
| `plant_stand` | 2 |
| `rack` | 2 |
| `rocker` | 2 |
| `sideboards_and_buffet_cabinets` | 2 |
| `storage_ao_eu` | 2 |
| `storage_cabinet` | 2 |
| `storage_insert` | 2 |
| `vessel` | 2 |
| `activity_table` | 1 |
| `apparel` | 1 |
| `armchair_cover` | 1 |
| `bar_cart` | 1 |
| `bed_accessories` | 1 |
| `bed_dresser` | 1 |
| `bell` | 1 |
| `bistro_table` | 1 |
| `blocker` | 1 |
| `board` | 1 |
| `bookcase_extension` | 1 |
| `bookends` | 1 |
| `cabinet_frame` | 1 |
| `candle` | 1 |
| `candle_holder` | 1 |
| `candlestick` | 1 |
| `canoe` | 1 |
| `carving` | 1 |
| `chain` | 1 |
| `chair_pad` | 1 |
| `chaise_cover` | 1 |
| `chiseled` | 1 |
| `curtain_bracket` | 1 |
| `curtain_rod` | 1 |
| `curtain_track` | 1 |
| `desk_system` | 1 |
| `door_drawer_front` | 1 |
| `door_mat` | 1 |
| `drawer` | 1 |
| `drawer_frame` | 1 |
| `earth` | 1 |
| `easel` | 1 |
| `etagere` | 1 |
| `folding_chair` | 1 |
| `furniture_accessory` | 1 |
| `furniture_expansion` | 1 |
| `game` | 1 |
| `gift_card` | 1 |
| `handle` | 1 |
| `hippo` | 1 |
| `hutch` | 1 |
| `knob` | 1 |
| `knot` | 1 |
| `ladder` | 1 |
| `leaf` | 1 |
| `loveseat_cover` | 1 |
| `marquina` | 1 |
| `media_shelf` | 1 |
| `monitor_stand` | 1 |
| `office_furniture` | 1 |
| `oil_vinegar_bottle` | 1 |
| `ottoman_cover` | 1 |
| `outdoor_chaise_lounge_cover` | 1 |
| `outdoor_coffee_and_side_tables` | 1 |
| `outdoor_dining_bench` | 1 |
| `outdoor_dining_chair` | 1 |
| `outdoor_dining_set` | 1 |
| `outdoor_dining_set_cover` | 1 |
| `outdoor_ottoman` | 1 |
| `outdoor_side_table` | 1 |
| `pedestal` | 1 |
| `pitcher` | 1 |
| `platform` | 1 |
| `ring` | 1 |
| `screen` | 1 |
| `seat` | 1 |
| `server` | 1 |
| `shelf_bracket` | 1 |
| `shelf_insert` | 1 |
| `shelves_bookcases` | 1 |
| `shipping_protection` | 1 |
| `side_console` | 1 |
| `slab` | 1 |
| `sofa_legs_hardware` | 1 |
| `sofa_module` | 1 |
| `stem` | 1 |
| `step_stool` | 1 |
| `stool_with_storage` | 1 |
| `storage_combination` | 1 |
| `storage_console` | 1 |
| `storage_door` | 1 |
| `storage_shelves_bookcases` | 1 |
| `storage_table` | 1 |
| `subscription` | 1 |
| `susan` | 1 |
| `table_mirror` | 1 |
| `tabletop` | 1 |
| `tic-tac-toe` | 1 |
| `top_panel` | 1 |
| `trough` | 1 |
| `tv_unit` | 1 |
| `umbrella_base` | 1 |
| `wall_storage` | 1 |
| `wall_tree` | 1 |

## Decor labels

These labels describe the 320 decor products that have a 3D model. They are not catalog categories.

| Label | Products |
| --- | ---: |
| a small tree in a pot | 87 |
| a potted plant | 81 |
| a decorative mirror | 38 |
| a figurine | 29 |
| a sculpture | 21 |
| a decorative object on a shelf | 19 |
| a decorative sculpture | 18 |
| a decorative plant | 9 |
| a bonsai tree | 8 |
| a wall mirror | 4 |
| an indoor plant | 4 |
| a framed mirror | 1 |
| an ornament | 1 |
