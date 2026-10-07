# Map tile file format (PMT), version 1.3: symbols and street names

This file adds to `FORMAT.md` (version 1.1). Everything there stands; only what is new is written here. A reader
for the two new layer kinds is built from `FORMAT.md` and this file. `tools/pmt.py` is the reference encoder and
decoder for both, and the test vectors `tests/vectors/symbols.pmt` and `tests/vectors/street_names.pmt` (with their
canonical dumps, `.txt`) pin them.

The owner, 2026-10-06 (Jira THES-118): "rider symbols such as boat ramps, marinas and fuel." "And street names."
"... marinas, points of interest, etc."

## 1. What version 1.3 adds, and what it leaves alone

- Two layer kinds, each in **files of its own**: **7 symbols** and **8 street names**. A file of kind 7 or 8 states
  `version_minor` 3 or higher; a reader refuses kind 7 or 8 in a file that states less.
- Nothing else. A base, water, zones, roads or places file is written exactly as before (1.0 or 1.1, byte for
  byte), and a pack gains the new files beside its old ones. No built file is rebuilt.
- **Layer kind 6 and minor version 2 are not defined here.** They are held for the land cover work (branch
  `panoptes/format-1.1` of this repository, not merged). A reader of this version treats kind 6 as unknown.
- **A reader that does not know a kind never opens it.** The display firmware 0.4.0 to 0.4.4 reads the 64-byte
  fixed header, sees a kind outside water, roads and places, logs one line and skips the file: no memory, no file
  slot, nothing on the glass (`components/panoptes_map_store/map_store.c` of pwca_panoptes at 9f84717). Each
  skipped file counts towards that firmware's limit of 8 skipped files in a pack; a pack therefore holds at most a
  few files of kinds an old display does not know (two today, one per kind unless a file passes 2 GiB).
- `MANIFEST.sha256` and `SOURCES.json` list the new files as they list every file. No display reads either.

The header (`FORMAT.md` 3), the level table, tile keys, index, pages and coverage grid (4), the string table (5),
the varint, zigzag and coordinate rules (6) and the reader rules (8) apply to both kinds unchanged. Both kinds have
no attribute records (`attr_count` 0), `flags` 0, no buffer (`buffer` 0) and `full_class` 0. In the fixed header's
`layer_kind` row read: 7 symbols, 8 street names.

## 2. Symbols (`layer_kind` 7)

Points only. A symbol is a place a rider looks for, drawn as a small picture at its point; its class says which
picture.

### 2.1 Feature

```
feature (symbol):
  varint  head            geometry = head & 3 = 3 (point); has_attr = (head >> 2) & 1 = 0; class = head >> 3
  varint  zigzag(dx)      x = previous x + dx
  varint  zigzag(dy)      y = previous y + dy
  u8      name_len        0 to 127; 0 = the symbol has no name
  u8[name_len]            the name: UTF-8, not 0-terminated
```

This is the point feature of `FORMAT.md` 6 with one change: **the name may be empty**. The running point starts at
(0, 0) at the start of the blob and carries from feature to feature. A feature of another geometry, a point with
`has_attr` set, a `name_len` over 127 or past the blob's end, or a point outside [0, 2^`coord_bits`] makes the tile
bad (it is skipped whole). A name follows the name rules of `FORMAT.md` 6 (strict UTF-8, no control character); a
reader that meets a name breaking them **keeps the symbol and drops only its name**. A symbol counts as one feature
and one point towards the tile limits.

### 2.2 Classes

`class` is the whole of `head >> 3`: any value from 1 to 2^29 − 1 fits, so **a new class never needs a new format
version**. A reader draws the classes it has a picture for and passes over every other class without an error, as
it does for a road class it does not know. Class 0 is never written.

| Class | What | From OpenStreetMap |
|---|---|---|
| 1 | Fuel a rider reaches from the water: a fuel dock | `waterway=fuel`; `amenity=fuel` with `boat=yes` or a `seamark:type`; `seamark:small_craft_facility:category` holding `fuel_station`; a marina with `fuel=yes` or a `fuel:*=yes` |
| 2 | Marina | `leisure=marina`; `seamark:harbour:category=marina` |
| 3 | Boat ramp | `leisure=slipway`; `seamark:small_craft_facility:category` holding `slipway` |
| 4 | Filling station on a road | `amenity=fuel` that is not class 1. Written only when the builder is told to (`--road-fuel`); no style exists for it yet, so no display draws it |
| 5 to 15 | Held for further services at the water | |
| 16 and up | Free for further points of interest | |

A class added later is given a row here; the version stays 1.3.

- An object tagged `access=private` or `access=no` is left out.
- An object mapped as a point is at its point; one mapped as an area is at a point inside the area (the middle of
  its widest stretch); one mapped as a line (a ramp) at its middle point. Areas mapped as relations are not read.
- **One object, two classes:** a marina that sells fuel is written as two symbols at the same point, class 1 and
  class 2 (Branding's style, section 8: the display's giving-way rule then shows fuel).
- Two symbols of one class nearer than 60 m are one (the one with a name stays).
- **The name written** is the object's `name`, else `name:en`, else `brand`, else `operator`, the first that is
  drawable under the places rule of `FORMAT.md` 7.5 (the display's Latin characters, composed, single spaces, 1 to
  127 bytes); none: `name_len` 0.

### 2.3 Levels

The names file's levels from zoom 10 (`FORMAT.md` 7.6), so a display reads symbols exactly as it reads names:

| Level | Tile zoom | Serves | Bits | Classes | Names |
|---|---|---|---|---|---|
| L0 | 10 | 10 to 11 | 12 | 1, 2 | none (`name_len` 0) |
| L1 | 12 | 12 to 13 | 12 | 1 to 4 | none |
| L2 | 12 | 14 | 16 | 1 to 4 | written |
| L3 | 14 | 15 to 16 | 14 | 1 to 4 | written |

- Branding's style (pwca_logos d536870, `ux/PANOPTES_MAP_NEW_LAYERS_STYLE.md` 3): fuel and marina from zoom 10, the
  boat ramp from 12, a symbol's name from 14. The levels hold exactly that; a display need not filter by zoom, but
  it still decides what it draws.
- **No buffer:** a symbol is in exactly one tile of a level, so a display that gathers the tiles in view never
  meets a symbol twice. A display that draws tile by tile must let a symbol's picture cross the tile's edge.
- A tile's symbols are in class order (1 first), then by OpenStreetMap id: the same bytes on every build. At most
  12,000 symbols in a tile (the highest classes are left out beyond that; it does not happen with today's data).
- A coverage grid on every level: 1 where the level has a symbol in the cell, 0 elsewhere. The value 2 is not used.
- `tolerance_dm` is 0. The string `str_layer` is `osm-symbols`.

### 2.4 What a display does with it (Branding's style, not part of the format)

A light disc with a dark drawing, 24 px across on the 7-inch and 40 px on the 5-inch, centred on the point, never
turned, never scaled by zoom, the same in portrait and landscape; drawn underway too; never over a keep-out area;
no two overlap (fuel wins over marina, marina over boat ramp, then the one nearer the boat); at most 16 on a
screen; the name to the right of the disc from zoom 14 when stopped. The numbers are in Branding's file.

## 3. Street names (`layer_kind` 8)

Lines only, each carrying its name. A feature is a **run**: a stretch of a named road along which the name may be
drawn as straight text. The roads themselves stay in the roads files (kind 4); this file holds no road that is
drawn, only where names go.

### 3.1 Feature

```
feature (named line):
  varint  head            geometry = head & 3 = 2 (line); has_attr = (head >> 2) & 1 = 0; class = head >> 3
  u8      name_len        1 to 127
  u8[name_len]            the name: UTF-8, not 0-terminated
  varint  n               points in the line, at least 2
  n times:
    varint  zigzag(dx)    x = previous x + dx
    varint  zigzag(dy)    y = previous y + dy
```

This is **not** the line feature of `FORMAT.md` 6: there is no part count, the name comes before the points, and a
feature is one line. The layer kind of the file fixes the form, as it fixes the geometries: a reader decodes a tile
of a kind 8 file only with this form, and a tile of any other kind never with it. The running point starts at (0, 0)
at the start of the blob and carries from feature to feature.

A feature of another geometry, `has_attr` set, a `name_len` of 0, over 127 or past the blob's end, `n` below 2, or
a point outside [0, 2^`coord_bits`] makes the tile bad (it is skipped whole). A name follows the name rules of
`FORMAT.md` 6; a reader **skips a feature whose name breaks them and keeps the rest of the tile**. A reader copies
a name out of the tile buffer before it uses it and never treats it as a format string or as markup. A feature
counts as one feature and one part, and its points count, towards the tile limits.

### 3.2 Classes

The road classes of `FORMAT.md` 7.4, so a display styles and ranks a name by the same number as its road:
1 motorway, 2 trunk, 3 primary, 4 secondary, 5 tertiary, 6 unclassified, residential or living street. Links
(9 to 13), service roads, tracks and paths have no names here. A reader passes over a class it does not know.

### 3.3 What the builder writes

- **The name** is the way's `name`, else its `name:en`, under the places rule of `FORMAT.md` 7.5, spelled as it is
  mapped ("North Washington Avenue"). Route numbers (`ref`) are not written. A road without a drawable name gives
  nothing.
- The ways of one name and one class are joined end to end where exactly two of them meet, so a street split at
  every bridge is one line; a crossing street has another name and does not break it.
- Each joined line is cut into **straight runs**: a run goes on while every point of the road in it stays within
  the level's straightness of the straight line from the run's first point to its last, and while the road does
  not turn back. **A run is written as its two end points only**, so a display has no curve to follow: the text
  goes on the straight line between the two points, and the road is never further from that line than the level's
  straightness. `tolerance_dm` of the level holds the straightness in decimetres, on the ground.
- A run is cut at the tile edges (no buffer): each piece is in exactly one tile, with its end on the tile's edge
  (coordinate 0 or 2^`coord_bits`); the piece in the next tile starts at the same place.
- A run, or a piece of one, shorter than its name could ever be drawn on is left out: under 12 m (zoom 14 level) or
  1.5 m (zoom 15 to 16 level) for each character of the name.
- At most the 3 longest runs of one name and class in a tile; at most 12,000 runs in a tile.
- A tile's runs are in the order a display with room for a few should take them: class ascending (the larger road
  first), then the longer run first.

A reader must accept any line of 2 or more points (a later builder may write a run with its bends); it may use only
the first and last point.

### 3.4 Levels

| Level | Tile zoom | Serves | Bits | Classes | Straightness (`tolerance_dm`) |
|---|---|---|---|---|---|
| L0 | 12 | 14 | 16 | 1 to 3 | 24 m (240) |
| L1 | 14 | 15 to 16 | 14 | 1 to 6 | 6 m (60) |

- Both levels have 2^28 units round the world, as the roads' and names' closest levels have.
- Branding's style (section 4): motorway, trunk and primary named from zoom 14; secondary and tertiary from 15;
  residential and unclassified from 16; zooms 17 and 18 as 16. L1 holds classes 1 to 6 for zooms 15 and 16
  together: **the display leaves class 6 out at zoom 15.**
- The straightness is half the height of the style's 14 px text at the level's closest zoom, with room: a name
  drawn on a run's straight line lies on its road.
- A coverage grid on every level: 1 where the level has a run in the cell, 0 elsewhere. The value 2 is not used.
- The string `str_layer` is `osm-street-names`.
- A file is never over 2 GiB − 1 byte. A street names layer over the builder's limit (2,000,000,000 bytes) is
  written as several files (`-01`, `-02`, ...), cut between regions of zoom-7 tiles, each with all levels; a
  display then has several files of kind 8, as it has several of water.

### 3.5 What a display does with it (Branding's style, not part of the format)

For a run in view at zoom z: the run's length on the screen is known from its two points. The name is drawn when
the text's length plus 8 px at each end fits the part of the run that is on the screen, centred on that part,
along the line from one point to the other, turned so that it is never upside down on the glass as it is mounted
(the baseline's angle between −90 and +90 degrees on the screen, in portrait and in landscape alike). Never cut
short, never abbreviated, never curved. Jost 500, 14 px (5-inch: 24 px), `0xC8C5BD`, halo `0x0B1820`. Not drawn
underway. A name at most once on a screen (the same text from two runs or two tiles is one name); at most 12 on a
screen; a street name gives way to keep-out areas, zone labels, symbols and their names, and every place and water
name; among street names the larger road wins, then the one nearer the boat. A display that lacks a character of
the name in its font does not draw the name.

## 4. Canonical dump

`FORMAT.md` 9 with one more feature line. A kind 7 point prints as every point does; its name prints as `""` when
the symbol has none and as `-` when the reader drops a bad name. A kind 8 feature prints as:

```
feature nline class=<c> name=<"<text>" or ->
part <n> <x>,<y> <x>,<y> ...
```

`name=-` when the reader skips the feature for its name (the `part` line is still printed).

## 5. Test vectors

| File | What it pins |
|---|---|
| `tests/vectors/symbols.pmt`, `.txt` | Kind 7, 1.3: a level without names; every class with and without a name; a marina and its fuel at one point; a name with an accent; a symbol on the tile's last unit; classes 4 and 40, which a display passes over |
| `tests/vectors/street_names.pmt`, `.txt` | Kind 8, 1.3: both levels; every road class; one name twice in a tile; a run that ends on the tile's edge and goes on in the next tile; a vertical run; a line of three points; a name with an accent; class 7, which a display passes over |

`tests/test_extras.py` holds the byte-level cases (a symbol's and a named line's bytes, every refusal).

## 6. The builder

`python tools/build_extras.py <extract.osm.pbf> <out_dir> [--name N] [--only symbols|streets] [--road-fuel]`
writes `<name>-symbols.pmt` and `<name>-streets.pmt` with `LICENSE.txt`, `ATTRIBUTION.txt`, `SOURCES.json` and
`MANIFEST.sha256`: a pack directory that `tools/assemble_pack.py` puts beside a pack's other layers.
`python tools/check_extras.py <pack_dir>` reads the files back, decodes every tile and counts the symbols by class
and the street names. `tools/add_symbols_and_street_names.ps1` runs build, check and the assembly of a new card
folder in one go.
