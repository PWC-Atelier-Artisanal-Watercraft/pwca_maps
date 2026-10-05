"""Lists the names a places file holds around a point, level by level: a look at what a display would be offered.

Usage: python tools/names_at.py <places.pmt> <lat> <lon> [most per level, default 25]
"""

import sys
from pathlib import Path

import numpy as np

import packgeom as pg
import pmt


def main(argv):
    if len(argv) < 4:
        print(__doc__, file=sys.stderr)
        return 2
    f = pmt.PmtFile(Path(argv[1]).read_bytes())
    lat, lon = float(argv[2]), float(argv[3])
    most = int(argv[4]) if len(argv) > 4 else 25
    for i, lv in enumerate(f.levels):
        x, y = pg.world_xy(lon, lat, lv.tile_zoom)
        tx, ty = int(np.floor(x)), int(np.floor(y))
        entry = f.find(i, tx, ty)
        feats = f.tile(i, entry) if entry is not None else []
        print(f"level {i}: tile zoom {lv.tile_zoom}, zooms {lv.zoom_min} to {lv.zoom_max}: {len(feats)} names in the tile")
        for _geom, cls, _attr, (_px, _py, name) in feats[:most]:
            print(f"    {cls:2d}  {bytes(name).decode('utf-8')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
