# North America roads layer (2026-10-03, a FORMAT 1.1 PROPOSAL: layer_kind 4)

Panoptes (display). Branch `panoptes/na-z8-level`. Why: the owner, 2026-10-02, "I want the entire US map"; the
Coordinator: roads and place names are wanted. NO DISPLAY READS THIS KIND YET: the firmware part is a design for
Security Review and Branding (pwca_argos docs/design/PANOPTES_MAP_ROADS_PLACES.md).

## The builder

`build_pack.py --layer roads`: OSM `highway` in six classes (1 motorway, 2 trunk, 3 primary, 4 secondary, 5 tertiary,
6 unclassified / residential / living_street; links take their road's class; service, tracks, paths and `area=yes`
left out). Levels: L0 (cut at zoom 8, serving 8 to 9, 150 m) classes 1 to 2; L1 (10, 10 to 11, 40 m) 1 to 3; L2 (12,
12 to 13, 10 m) 1 to 5; L3 (12, 14 to 16, 16 bits, 4 m) all six. Each class's ways joined end to end where exactly two
meet (`join_lines`); the per-tile budget (12,000 features or parts, 100,000 points). No relations pass, no sea.

## Measurements

| Extract | Ways | Joined lines | Points | File | Build |
|---|---|---|---|---|---|
| Minnesota | 284,286 | 172,055 | 3,275,838 | 6.6 MB | 92 s |
| North America | 19,590,708 | 12,754,979 | 228,664,438 | 441,127,198 B | 5,491 s (1 h 32 min) |

North America: `out\na-roads\north-america.pmt`, sha256 8b9675d2f4251d36c2d9084bb0ebbe695fb644711c28477b8c08abae0943ffd4;
one file (under 2 GiB); 207,297,231 nodes needed (half the water build's), no tile over the budget.
`pmt.py check`: ok, 4 levels, 389,456 tiles. `tile_limits.py`, maxima per level (blob / points / features or parts):

| Level | Tiles | Largest blob | Most points | Most features or parts |
|---|---|---|---|---|
| L0 | 984 | 97,792 B | 24,934 | 11,973 |
| L1 | 12,221 | 49,235 B | 12,580 | 5,292 |
| L2 | 158,971 | 20,165 B | 5,261 | 1,956 |
| L3 | 217,280 | 120,415 B | 26,562 | 8,683 |

## Places (counted, not built)

`tools/measure_places.py`: North America 198,892 named places (`place` = city 1,419, town 10,307, village 40,093, hamlet
147,073, with a `name`), about 4.5 MB as a layer; 3 names over 63 bytes (longest 77). The places layer needs a new
geometry (a point with a name), proposed in the design.
