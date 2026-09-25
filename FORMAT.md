# Map tile file format (PMT), version 1.0

This is the on-card and in-flash format of the offline map packs. It is published with the build tools as part of the
build recipe (ODbL section 4.6). The files are plain, documented and unencrypted (ODbL section 4.7). The
integrity checks below (CRC-32 per tile and per header, the pack's SHA-256 manifest) detect damage; they never lock
anything.

`tools/pmt.py` is the reference encoder and decoder. The test vectors in `tests/vectors/` pin the format: any reader
must produce the same canonical dump (section 9) for each vector.

## 1. Overview

- A pack is a directory of `.pmt` files plus `ATTRIBUTION.txt`, `LICENSE.txt`, `SOURCES.json` and
  `MANIFEST.sha256` (see `README.md`).
- Each `.pmt` file is self-describing: one **layer** (base, water or zones), one or more **levels**, and for each
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
| 6 | u16 | `version_minor` | 0. Minor versions only add optional content; a reader accepts any minor of its major |
| 8 | u32 | `header_size` | Bytes in the header block, a multiple of 4096, at most 16 MiB |
| 12 | u32 | `header_crc` | CRC-32 of bytes [0, `header_size`) computed with this field as 0 |
| 16 | u32 | `file_size` | Total file size; a shorter file is a truncated copy and is rejected |
| 20 | u8 | `layer_kind` | 1 base, 2 water, 3 zones (section 7) |
| 21 | u8 | `level_count` | 1 to 8 |
| 22 | u16 | `flags` | 0 in version 1.0 |
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
feature:
  varint  head            geometry = head & 3 (1 polygon, 2 line); has_attr = (head >> 2) & 1; class = head >> 3
  varint  attr            only if has_attr: attribute record index (< attr_count)
  varint  part_count      1 to 65535
  part_count times:
    varint  n             points in the part: polygon rings at least 3, lines at least 2
    n times:
      varint  zigzag(dx)  x = previous x + dx
      varint  zigzag(dy)  y = previous y + dy
```

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
- **Limits** (readers enforce them): 65535 features per tile, 65535 parts per feature, 1,048,576 points per tile.
- **Draw order**: renderers draw polygon features in ascending class, then line features in ascending class.

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

## 8. Reader rules

On open a reader checks: the magic and major version; `header_size` (a multiple of 4096, at most 16 MiB, not larger
than the file); the header CRC; `file_size` against the real size; `level_count`; for each level the field ranges in
section 4, that the page table, grid and index lie inside their regions, that the page table is strictly ascending
and matches ceil(`entry_count` / 256); the string table (bounds, ascending offsets, a 0 byte ending each string); the
attribute table (bounds, ascending offsets); and that every string id in the fixed header is below `str_count` or is
`0xFFFF`.

On each tile it checks that the entry's blob lies inside its level's data region, that its length is at most 8 MiB
and that the CRC matches; then while decoding, every varint, count, class, attribute index and coordinate against
section 6. A bad tile is skipped and reported; it never stops the map.

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
```

In `<text>`, `\` prints as `\\`, `"` as `\"`, and bytes below 0x20 as `\xHH` (two uppercase hex digits); all other
bytes are printed as they are. A `grid` line follows its `level` line only when the level has a grid. Tiles are listed per level in key
order, each followed by its features and their parts.
