# Map tile file format (PMT), version 1.1

This is the on-card and in-flash format of the offline map packs. It is published with the build tools as part of the
build recipe (ODbL section 4.6). The files are plain, documented and unencrypted (ODbL section 4.7). The
integrity checks below (CRC-32 per tile and per header, the pack's SHA-256 manifest) detect damage; they never lock
anything.

`tools/pmt.py` is the reference encoder and decoder. The test vectors in `tests/vectors/` pin the format: any reader
must produce the same canonical dump (section 9) for each vector.

Version 1.1 (2026-10-04) adds two layer kinds and nothing else: roads (kind 4, section 7.4) and places (kind 5, section
7.5, with a new geometry: a point with a name). A base, water or zones file is unchanged and is still written as 1.0,
byte for byte. A 1.0 reader draws a 1.1 pack's water and never draws the kinds it does not know.

## 1. Overview

- A pack is a directory of `.pmt` files plus `ATTRIBUTION.txt`, `LICENSE.txt`, `SOURCES.json` and
  `MANIFEST.sha256` (see `README.md`).
- Each `.pmt` file is self-describing: one **layer** (base, water, zones, roads or places), one or more **levels**,
  and for each
  level a sorted tile index and the tile data.
- Tiles are Web Mercator tiles (x to the east, y to the south; zoom z has 2^z by 2^z tiles). A level is cut at one
  tile zoom and serves a range of display zooms.
- Geometry is tile-local integer coordinates, delta coded as zigzag varints.
- Readers need no parsing library: fixed little-endian structures, one binary search in RAM and one in a 4 KiB page
  per tile lookup.
- A file is never larger than 2 GiB − 1 byte (FAT32 cards; every offset fits a `u32`).

All integers are little-endian. All offsets are absolute byte offsets from the start of the file unless stated
otherwise.

## 2. File layout

```
[header block]  fixed header (64 B) | level table | coverage grids | page tables | attribute table | string table
                | zero padding to a multiple of 4096
[indexes]       one per level, each starting at a multiple of 4096
[tile data]     one region per level
```

The header block is everything a reader loads into RAM when it opens the file. Its size is `header_size`, and one
CRC-32 covers it. The order of the parts inside the header block, and of the indexes and data regions after it, is
the recommended order; readers must use the offsets and not assume an order.

## 3. Fixed header (offset 0, 64 bytes)

| Offset | Type | Field | Notes |
|---|---|---|---|
| 0 | u8[4] | `magic` | `PMTF` (0x50 0x4D 0x54 0x46) |
| 4 | u16 | `version_major` | 1. A reader rejects any other major version |
| 6 | u16 | `version_minor` | 0 or 1. Minor versions only add optional content; a reader accepts any minor of its major. A file of layer kind 4 or 5 states 1 or higher; a reader refuses kind 4 or 5 in a file that states 0 |
| 8 | u32 | `header_size` | Bytes in the header block, a multiple of 4096, at most 16 MiB |
| 12 | u32 | `header_crc` | CRC-32 of bytes [0, `header_size`) computed with this field as 0 |
| 16 | u32 | `file_size` | Total file size; a shorter file is a truncated copy and is rejected |
| 20 | u8 | `layer_kind` | 1 base, 2 water, 3 zones, 4 roads, 5 places (section 7). A reader never decodes or draws a kind it does not know |
| 21 | u8 | `level_count` | 1 to 8 |
| 22 | u16 | `flags` | 0 in versions 1.0 and 1.1 |
| 24 | u32 | `build_time` | UTC seconds since 1970 when the file was built |
| 28 | u32 | `data_time` | UTC seconds since 1970 of the source snapshot (OSM replication time, dataset date) |
| 32 | u16 | `str_count` | Number of strings in the string table |
| 34 | u16 | `str_layer` | String id: layer name, e.g. `osm-water` |
| 36 | u16 | `str_credit` | String id: short credit drawn on the map, e.g. `© OpenStreetMap contributors` |
| 38 | u16 | `str_license` | String id: licence, e.g. `ODbL 1.0` |
| 40 | u16 | `str_source` | String id: source name for legends, e.g. `FWC` |
| 42 | u16 | `str_source_date` | String id: source date for legends, e.g. `2026-08` |
| 44 | u16 | `str_build` | String id: build tool version (git commit) |
| 46 | u16 | reserved | 0 |
| 48 | u32 | `str_table_offset` | Start of the string table (inside the header block) |
| 52 | u32 | `attr_count` | Number of attribute records; 0 if none |
| 56 | u32 | `attr_table_offset` | Start of the attribute table (inside the header block); 0 if `attr_count` is 0 |
| 60 | u32 | reserved | 0 |

String id `0xFFFF` means "no string".

## 4. Level table (offset 64, `level_count` records of 48 bytes)

| Offset | Type | Field | Notes |
|---|---|---|---|
| 0 | u8 | `tile_zoom` | Zoom the level's tiles are cut at, 0 to 16 |
| 1 | u8 | `zoom_min` | First display zoom served; `tile_zoom` ≤ `zoom_min` ≤ `zoom_max` |
| 2 | u8 | `zoom_max` | Last display zoom served, at most 22 |
| 3 | u8 | `coord_bits` | A tile side is 2^`coord_bits` units; 8 to 16 |
| 4 | u16 | `buffer` | Clip buffer in units: geometry may extend this far outside the tile; less than 2^(`coord_bits` − 3) |
| 6 | u8 | `flags` | Bit 0: a coverage grid is present (section 4.2). Other bits 0 |
| 7 | u8 | `full_class` | Class drawn over a whole tile that the coverage grid marks "full"; 0 if unused |
| 8 | u32 | `key_min` | Smallest tile key in the index (0 if empty) |
| 12 | u32 | `key_max` | Largest tile key in the index (0 if empty) |
| 16 | u32 | `entry_count` | Number of index entries |
| 20 | u32 | `page_table_offset` | Page table (inside the header block) |
| 24 | u32 | `index_offset` | Index, a multiple of 4096 |
| 28 | u32 | `data_offset` | Start of this level's tile data |
| 32 | u32 | `data_size` | Size of this level's tile data |
| 36 | u32 | `grid_offset` | Coverage grid (inside the header block); 0 if absent |
| 40 | u16 | `tolerance_dm` | Simplification tolerance in decimetres (information only) |
| 42 | u16 | reserved | 0 |
| 44 | u32 | reserved | 0 |

### 4.1 Tile keys, index and pages

- **Tile key**: the Morton code of the tile at `tile_zoom`: bit *i* of x goes to bit 2*i*, bit *i* of y to bit
  2*i* + 1. With `tile_zoom` ≤ 16 it fits a `u32`.
- **Index**: `entry_count` entries of 16 bytes, strictly ascending by key:

  | Offset | Type | Field |
  |---|---|---|
  | 0 | u32 | `key` |
  | 4 | u32 | `offset`: absolute offset of the tile blob |
  | 8 | u32 | `length`: tile blob length in bytes, at most 8 MiB; 0 is an empty tile |
  | 12 | u32 | `crc`: CRC-32 of the tile blob (0 for an empty tile) |

  The blob [`offset`, `offset` + `length`) lies inside the level's data region [`data_offset`,
  `data_offset` + `data_size`); an empty tile has `offset` = `data_offset`. Entries may share a blob (identical
  tiles, such as the inside of a large lake, are stored once).

- **Pages**: the index is read in pages of 256 entries (4096 bytes; each page starts on a 4096-byte boundary because
  the index does). The page table holds ceil(`entry_count` / 256) `u32` values: the key of each page's first
  entry, strictly ascending. A lookup is a binary search in the page table (in RAM), one 4 KiB read, and a binary
  search in the page.
- **Empty tile** (length 0): the tile is present and holds no features.
- **Absent tile** (no index entry): see the coverage grid. Without a grid, an absent tile is the same as an empty
  one.

### 4.2 Coverage grid (optional, only for levels with `tile_zoom` ≥ 8)

256 × 256 cells, one per zoom-8 tile of the world, row-major (cell index = cy × 256 + cx), 2 bits per cell, cell *i*
in byte *i* / 4 at bits 2 × (*i* mod 4) and up (16384 bytes).

| Value | Meaning for tiles of this level inside the cell |
|---|---|
| 0 | Not covered: this file has no data here. The reader falls back to a coarser layer (e.g. the overview) |
| 1 | Covered; an absent tile is empty |
| 2 | Covered; an absent tile is full: the whole tile (with its buffer) is `full_class` |
| 3 | Reserved; readers treat it as 0 |

This keeps open-ocean tiles out of the index.

## 5. String table and attribute table

- **String table** at `str_table_offset`: `u32 off[str_count + 1]` relative to the table start, then the bytes.
  String *i* is the bytes [`off[i]`, `off[i+1]`), UTF-8, ending with one 0 byte that is not part of the string.
  Offsets are ascending, the first is at least 4 × (`str_count` + 1), and the last is the table size.
- **Attribute table** at `attr_table_offset`: `u32 off[attr_count + 1]` relative to the table start, then the
  records. Record *i* is the bytes [`off[i]`, `off[i+1]`): a list of tag-length-value items, each `varint tag`,
  `varint length`, then `length` bytes. Readers skip tags they don't know. Tags for zones are in section 7.3.

## 6. Tile blob

A tile blob is a sequence of features that ends exactly at the blob's end. Points are delta coded: a running point
starts at (0, 0) at the start of the blob and carries across parts and features.

```
feature (polygon or line):
  varint  head            geometry = head & 3 (1 polygon, 2 line); has_attr = (head >> 2) & 1; class = head >> 3
  varint  attr            only if has_attr: attribute record index (< attr_count)
  varint  part_count      1 to 65535
  part_count times:
    varint  n             points in the part: polygon rings at least 3, lines at least 2
    n times:
      varint  zigzag(dx)  x = previous x + dx
      varint  zigzag(dy)  y = previous y + dy

feature (point; version 1.1, layer kind 5 only):
  varint  head            geometry = head & 3 = 3; has_attr = 0; class = head >> 3
  varint  zigzag(dx)      x = previous x + dx
  varint  zigzag(dy)      y = previous y + dy
  u8      name_len        1 to 127
  u8[name_len]            the name: UTF-8, not 0-terminated
```

- **Geometry by layer kind.** The layer kind of the file fixes which geometries its tiles may hold, and a reader
  fixes that pairing from the header before it decodes any tile of the file:

  | Layer kind | Geometries |
  |---|---|
  | 1 base, 2 water, 3 zones | polygon, line |
  | 4 roads | line only |
  | 5 places | point only |

  A feature of any other geometry makes the tile bad (it is skipped whole). Geometry 0 is never valid. A reader
  does not decode a tile of a kind that is not in this table.
- **Point names.** A name is 1 to 127 bytes and lies wholly inside the blob; a length of 0, over 127 or past the
  blob's end makes the tile bad. The bytes are strict UTF-8 (shortest form, no surrogates, at most U+10FFFF) with
  no control character (none of U+0000 to U+001F, U+007F, U+0080 to U+009F). A reader **skips a point whose name
  breaks that rule and keeps the rest of the tile**; the builder never writes such a name. A reader copies a name
  out of the tile buffer before it uses it, and never treats it as a format string or as markup.

- **varint**: unsigned LEB128, at most 5 bytes, value below 2^32.
- **zigzag**: 32-bit, `(v << 1) ^ (v >> 31)`; decode `(u >> 1) ^ -(u & 1)`.
- **Coordinates**: tile units, x to the east and y to the south (the same way as screen pixels), (0, 0) at the tile's
  north-west corner, 2^`coord_bits` units per side. Every point lies in [−`buffer`, 2^`coord_bits` + `buffer`]
  on both axes. Readers reject a tile with a point outside.
- **Polygons**: each part is a ring, closed implicitly (the last point joins the first; the first point is not
  repeated). Fill with the **non-zero winding rule**. Outer rings run clockwise on screen (the shoelace sum
  Σ (xᵢ·yᵢ₊₁ − xᵢ₊₁·yᵢ) is positive with y down) and holes run the other way, so overlapping polygons of the same
  class form their union.
- **Clipping**: geometry is clipped to the tile square grown by `buffer` on each side. Edges that the clip creates lie
  on that grown square, outside the tile, so a renderer that strokes outlines never shows them inside the tile.
  Neighbouring tiles overlap by the buffer; with the non-zero rule, filling the polygons of many tiles together gives
  the right union.
- **Limits** (readers enforce them): 65535 features per tile, 65535 parts per feature, 1,048,576 points per tile
  (a point feature counts as one point).
- **Draw order**: renderers draw polygon features in ascending class, then line features in ascending class. Roads
  are the exception (section 7.4): the largest road is drawn last.

## 7. Layers and classes

### 7.1 Base (`layer_kind` 1): the overview (Natural Earth)

The background is water. Classes: 1 land (polygon), 2 lake (polygon, drawn over land), 3 river (line).

### 7.2 Water (`layer_kind` 2): the detail (OpenStreetMap)

The background is land. Polygon classes: 1 sea, 2 lake or pond, 3 reservoir, 4 river area, 5 other water area. Line
classes: 16 river, 17 canal.

### 7.3 Zones (`layer_kind` 3): speed-restricted and no-wake areas

Polygons over the other layers, with an attribute record each. Classes: 1 idle speed / no wake, 2 slow speed /
minimum wake, 3 numeric speed limit, 4 no entry / vessel exclusion, 5 other restriction.

Attribute tags:

| Tag | Value | Meaning |
|---|---|---|
| 1 | UTF-8 | Zone name |
| 2 | varint | Speed limit in 0.1 km/h (class 3) |
| 3 | varint | Months in force, bit 0 = January; absent = all year |
| 4 | varint | Days in force, bit 0 = Monday; absent = every day |
| 5 | varint | Flags: bit 0 a marked channel is exempt, bit 1 the times are partial (see the source) |
| 6 | UTF-8 | Source reference: rule, ordinance or dataset object id |

Zone data is advisory. Where a zones file has no zone, the map says so ("no no-wake data here"); it never means that
no rules apply.

### 7.4 Roads (`layer_kind` 4, version 1.1): drivable roads (OpenStreetMap)

Lines only, drawn over land and water. No attribute records, no names. Classes, from the OSM `highway` tag:

| Class | OSM `highway` | Link class | OSM `highway` |
|---|---|---|---|
| 1 | motorway | 9 | motorway_link |
| 2 | trunk | 10 | trunk_link |
| 3 | primary | 11 | primary_link |
| 4 | secondary | 12 | secondary_link |
| 5 | tertiary | 13 | tertiary_link |
| 6 | unclassified, residential, living_street | | |

A **link** (a ramp, a slip road) has its road's class plus 8, so a renderer can draw it thinner than its road, or
leave it out. Left out of the data: service roads, tracks, paths, footways, cycleways, roads under construction or
proposed, and `area=yes`. A renderer draws class 6 first and class 1 last, each class's links just before the class
itself, so a larger road covers a smaller one where they meet; it does not draw a class it does not know.

The builder's levels (`tools/build_pack.py --layer roads`):

| Level | Tile zoom | Serves | Bits | Buffer | Tolerance | Classes |
|---|---|---|---|---|---|---|
| L0 | 8 | 8 to 9 | 12 | 64 | 150 m | 1, 2 |
| L1 | 10 | 10 to 11 | 12 | 64 | 40 m | 1 to 3 |
| L2 | 12 | 12 to 13 | 12 | 64 | 10 m | 1 to 5 |
| L3 | 12 | 14 to 16 | 16 | 512 | 4 m | 1 to 6, and the links 9 to 13 |

- Links are only in the level that serves zoom 14 and up: below that, two carriageways and their ramps would draw as
  one broad band.
- Each class's ways are joined end to end where exactly two meet, before cutting.
- A coverage grid as for water: 0 means the file has no roads data there, 1 means covered (an absent tile has no
  roads). The value 2 and `full_class` are not used: an absent roads tile never draws anything.
- The buffer matters to a renderer that draws each tile inside its own square: a road whose centre line runs just
  outside the square is in the tile as long as it is within the buffer, so its stroke is drawn up to the square's
  edge. A stroke's half width, in tile units at the display zoom, must not be more than the buffer.
- Every tile is within the display's budget (12,000 features or parts, 100,000 points); a tile over it leaves out its
  shortest lines first.

### 7.5 Places (`layer_kind` 5, version 1.1): named places (OpenStreetMap)

Points only (section 6), one per place, each with its name. No attribute records. Classes, from the OSM `place` tag of
a node that has a name: 1 city, 2 town, 3 village, 4 hamlet. A renderer draws a label for a point or leaves it out (by
class, by the room on the screen, or because its font lacks a character of the name); it never draws a class it does
not know.

The builder's levels (`tools/build_places.py`), with the roads' zooms:

| Level | Tile zoom | Serves | Bits | Buffer | Classes |
|---|---|---|---|---|---|
| L0 | 8 | 8 to 9 | 12 | 0 | 1 |
| L1 | 10 | 10 to 11 | 12 | 0 | 1, 2 |
| L2 | 12 | 12 to 13 | 12 | 0 | 1 to 3 |
| L3 | 12 | 14 to 16 | 16 | 0 | 1 to 4 |

- **No buffer:** a place is in exactly one tile of a level, so a renderer that gathers the tiles in view never meets
  the same place twice.
- **The name written** is the node's `name` when every character of it is one a display's label font is expected to
  hold (printable Basic Latin, Latin-1 Supplement, Latin Extended-A and -B, the curly quotes and the en and em dash),
  else its `name:en` under the same rule, else the place is left out (and counted in SOURCES.json). Names are
  stripped, their words separated by one space, and composed (NFC). U+02BB (the Hawaiian okina) and U+02BC are
  written as U+2018 and U+2019, the stand-ins the display's font has.
- A tile's features are in class order (cities first), then by OSM node id. Within the display's budget of 12,000
  features per tile; over it the lowest classes are left out.
- A coverage grid: 1 where the level has a place in the cell, 0 elsewhere (no places data: nothing to look up). The
  value 2 and `full_class` are not used.

### 7.6 The full packs (the builder's `--full` and `--minor`; work in progress, 2026-10-04)

The owner, 2026-10-04: "I want as much detail as possible for the USA." A full pack keeps the kinds and the tile
format above and adds classes and levels. A reader that does not know a class does not draw it, so a display that
knows only sections 7.2 and 7.4 still draws its own classes from a full pack.

**Water, full** (`build_pack.py --full`; layer kind 2): every water area from 50 m², and two more line classes:
18 stream, 19 drain or ditch (16 river and 17 canal as before). Intermittent water and culverts stay out.

| Level | Tile zoom | Serves | Bits | Buffer | Tolerance | Smallest inland area | Lines |
|---|---|---|---|---|---|---|---|
| L0 | 8 | 8 to 9 | 12 | 32 | 150 m | 25 ha | 16, 17 |
| L1 | 10 | 10 to 11 | 12 | 32 | 40 m | 1 ha | 16, 17 |
| L2 | 12 | 12 to 13 | 12 | 32 | 10 m | 2,000 m² | 16, 17 |
| L3 | 12 | 14 | 16 | 512 | 4 m | 400 m² | 16 to 18 |
| L4 | 14 | 15 to 16 | 14 | 256 | 2 m | all | all |

The last level is cut at zoom 14, so a display at zoom 16 reads a tile one sixteenth of a zoom-12 tile. The build
also writes `names.tsv` beside the pack (type A area, B bay or strait, L line; class; size in m² or m; a label point
in 1e-7 degrees; the name) for the places builder.

**Lines, full** (`build_pack.py --layer roads --full`; layer kind 4): the roads of section 7.4 and, as more classes:

| Class | What | Class | What |
|---|---|---|---|
| 17 | railway (not in a tunnel) | 21 | pier, breakwater, groyne |
| 18 | runway | 22 | dam, weir, lock gate |
| 19 | taxiway | 24 | state or province border (not at sea) |
| 20 | ferry route | 25 | country border (not at sea) |
| 23 | minor railway: yard, siding, spur, tram, light rail, subway above ground | | |

Class 17 is a main or branch line (`railway` = rail or narrow_gauge without a `service` tag). A border way tagged as a
sea border (`maritime=yes`, a `border_type` of the sea, `boundary=maritime`) or as coastline is left out.

| Level | Tile zoom | Serves | Bits | Buffer | Tolerance | Classes |
|---|---|---|---|---|---|---|
| L0 | 4 | 4 to 5 | 12 | 64 | 2,500 m | 1, 24, 25 |
| L1 | 6 | 6 to 7 | 12 | 64 | 600 m | 1, 2, 24, 25 |
| L2 | 8 | 8 to 9 | 12 | 64 | 150 m | 1, 2, 24, 25 |
| L3 | 10 | 10 to 11 | 12 | 64 | 40 m | 1 to 3, 17, 24, 25 |
| L4 | 12 | 12 to 13 | 12 | 64 | 10 m | 1 to 5, 17, 18, 20, 24, 25 |
| L5 | 12 | 14 | 16 | 512 | 4 m | 1 to 6, 9 to 13, 17 to 25 |
| L6 | 14 | 15 to 16 | 14 | 256 | 2 m | as L5 |

The two levels cut below zoom 8 have no coverage grid and are only in the first file of a pack. Their motorways and
trunk roads are joined into long chains through their junctions, and a piece shorter than twice the level's tolerance
(under a pixel there) is left out; over a tile's budget the borders are kept before the roads.

**Minor ways** (`build_pack.py --layer roads --minor`; layer kind 4, a file of its own): 7 service road, 8 track,
16 path (path, footway that is not a pavement or crossing, cycleway, bridleway, pedestrian street, steps). One level:
tile zoom 14, serving 15 to 16, 14 bits, buffer 256, tolerance 2 m. A display draws these under every road, and may
leave the file out when it is short of time.

A tile over the display's budget gives up its least class first (the shortest line first within a class), then its
smallest areas, never the sea.

**Names, full** (`build_places.py --full --names <the water build's names.tsv>`; layer kind 5). A class says what
kind of name it is and how much it matters, so a display can rank the names of the tiles in view:

| Class | Name of | First zoom |
|---|---|---|
| 1 to 4 | city, town, village, hamlet (section 7.5) | 8, 10, 12, 14 |
| 5 | state or province | 4 (to zoom 9 only) |
| 6, 7 | city of a million people or more; of 250,000 or more | 4, 6 |
| 16 to 22 | water area (lake, reservoir, bay), by size: from 20,000 km², 1,000 km², 50 km², 5 km², 25 ha, 2 ha, smaller | 4, 6, 8, 10, 12, 14, 15 |
| 24, 25 | river whose length in a tile cut at zoom 8 (24) or 10 (25) is 0.6 of the tile's width or more | 8, 10 |
| 26, 27, 28 | other river; canal; stream | 12, 14, 15 |
| 32, 33 | island; islet (mapped as points) | 12, 14 |

Seven levels, with the full lines pack's zooms: tile zoom 4 (zooms 4 to 5), 6 (6 to 7), 8 (8 to 9), 10 (10 to 11),
12 (12 to 13), 12 with 16 bits (14), 14 with 14 bits (15 to 16); no buffer. A bay mapped as a point is class 20.
A named waterway gives a point every 1.5 km along each of its ways; the points of one name in one tile are one name,
at the longest way's point. Names of one kind with the same text in one tile are written once. A tile's names are in
the order a display with room for a few should take them: 6, 5, 7, 16, 1, 17, 18, 2, 24, 19, 25, 3, 20, 32, 26, 21,
4, 27, 33, 22, 28; within a class the largest (people, area, length) first. `tools/names_at.py` lists a point's names.

## 8. Reader rules

On open a reader checks: the magic and major version; that a file of layer kind 4 or 5 states minor version 1 or
higher; `header_size` (a multiple of 4096, at most 16 MiB, not larger
than the file); the header CRC; `file_size` against the real size; `level_count`; for each level the field ranges in
section 4, that the page table, grid and index lie inside their regions, that the page table is strictly ascending
and matches ceil(`entry_count` / 256); the string table (bounds, ascending offsets, a 0 byte ending each string); the
attribute table (bounds, ascending offsets); and that every string id in the fixed header is below `str_count` or is
`0xFFFF`.

On each tile it checks that the entry's blob lies inside its level's data region, that its length is at most 8 MiB
and that the CRC matches; then while decoding, every varint, count, class, attribute index and coordinate against
section 6, and every feature's geometry against the file's layer kind. A bad tile is skipped and reported; it never
stops the map.

A reader that opens files from a card also bounds what it loads: a fixed number of files, and a ceiling on one header
block and on all opened header blocks together, checked before each allocation. A file over a bound is skipped and
reported, and the rest of the pack loads.

Packs are unencrypted and unsigned by design. Anyone may edit or rebuild them, so a reader treats every byte as
untrusted input.

## 9. Canonical dump (test vectors)

`python tools/pmt.py dump FILE` prints the file in this text form. Other readers print the same form and compare it
to `tests/vectors/*.txt`. Lines end with `\n`; numbers are decimal unless noted.

```
PMTF <major>.<minor> kind=<layer_kind> levels=<n> strings=<n> attrs=<n> build_time=<t> data_time=<t>
ids layer=<id> credit=<id> license=<id> source=<id> source_date=<id> build=<id>
string <i> "<text>"
attr <i> <record bytes as lowercase hex>
level <i> tz=<> zoom=<min>-<max> bits=<> buffer=<> flags=<> full_class=<> keys=<min>-<max> entries=<n> tol_dm=<>
grid <i> v0=<cells> v1=<cells> v2=<cells> v3=<cells>
tile <level> <x> <y> len=<> crc=<8 lowercase hex digits>
feature <polygon|line> class=<c> attr=<index or -> parts=<n>
part <n> <x>,<y> <x>,<y> ...
feature point class=<c> <x>,<y> name=<"<text>" or ->
```

A point's name prints as `-` when the reader skips it (section 6, point names).

In `<text>`, `\` prints as `\\`, `"` as `\"`, and bytes below 0x20 as `\xHH` (two uppercase hex digits); all other
bytes are printed as they are. A `grid` line follows its `level` line only when the level has a grid. Tiles are listed per level in key
order, each followed by its features and their parts.
