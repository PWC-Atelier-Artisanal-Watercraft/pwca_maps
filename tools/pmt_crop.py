"""Cuts a region out of a PMT file by tile ranges, for small test packs (e.g. one that fits in a board's flash).

Usage: python tools/pmt_crop.py <in.pmt> <out.pmt> <south> <west> <north> <east>

Every level keeps the tiles that touch the box, unchanged; nothing is re-clipped, so there are no new edges. The
coverage grid keeps the zoom-8 cells that touch the box, so outside the box but inside those cells the cropped file
shows land where the full file had water: a test pack, not a product pack.
"""

import math
import sys

import pmt


def tile_range(south, west, north, east, zoom):
    n = 1 << zoom

    def tx(lon):
        return min(n - 1, max(0, int((lon + 180.0) / 360.0 * n)))

    def ty(lat):
        s = math.sin(math.radians(max(-85.0511, min(85.0511, lat))))
        return min(n - 1, max(0, int((0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n)))

    return tx(west), ty(north), tx(east), ty(south)


def main(argv):
    if len(argv) != 7:
        print(__doc__, file=sys.stderr)
        return 2
    src = pmt.open_pmt(argv[1])
    south, west, north, east = (float(v) for v in argv[3:7])
    levels = []
    for i, lv in enumerate(src.levels):
        x0, y0, x1, y1 = tile_range(south, west, north, east, lv.tile_zoom)
        tiles = {}
        for entry in src.entries(i):
            x, y = pmt.unmorton(entry[0])
            if x0 <= x <= x1 and y0 <= y <= y1:
                tiles[(x, y)] = src.tile(i, entry)
        grid = None
        if lv.grid is not None:
            cx0, cy0, cx1, cy1 = tile_range(south, west, north, east, 8)
            grid = {(cx, cy): src.coverage(i, cx << (lv.tile_zoom - 8), cy << (lv.tile_zoom - 8))
                    for cx in range(cx0, cx1 + 1) for cy in range(cy0, cy1 + 1)}
            grid = {k: v for k, v in grid.items() if v}
        levels.append({"tile_zoom": lv.tile_zoom, "zoom_min": lv.zoom_min, "zoom_max": lv.zoom_max,
                       "coord_bits": lv.coord_bits, "buffer": lv.buffer, "tolerance_dm": lv.tolerance_dm,
                       "full_class": lv.full_class, "grid": grid, "tiles": tiles})
    strings = [s.decode("utf-8") for s in src.strings]
    ids = {k: (None if v == pmt.NO_STRING else v) for k, v in src.ids.items()}
    data = pmt.build_pmt(layer_kind=src.kind, levels=levels, strings=strings, ids=ids,
                         attrs=src.attrs, build_time=src.build_time, data_time=src.data_time)
    with open(argv[2], "wb") as f:
        f.write(data)
    counts = ", ".join(f"L{i + 1} {len(lv['tiles'])} tiles" for i, lv in enumerate(levels))
    print(f"{argv[2]}: {len(data)} bytes ({counts})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
