"""Per-level tile maxima of a PMT file against the display's draw limits.

Usage: python tools/tile_limits.py <file.pmt> [<file.pmt> ...]

The display (pwca_argos components/panoptes_map_view/map_view.c) decodes a tile into fixed scratch: a 1 MiB blob,
131,072 points and 16,384 features or parts. A bigger tile is skipped and shows as land. This tool prints, per level,
the tile count, the largest blob, the most points and the most features or parts, and how many tiles pass a limit.
"""

import sys

import pmt

BLOB_CAP = 1 << 20
POINT_CAP = 131_072
FEATURE_CAP = 16_384


def main(argv):
    if len(argv) < 2:
        print(__doc__.splitlines()[2])
        return 2
    bad_total = 0
    for path in argv[1:]:
        pf = pmt.open_pmt(path)
        print(path)
        for li, lv in enumerate(pf.levels):
            n = big_blob = big_pts = big_feat = over = 0
            for entry in pf.entries(li):
                n += 1
                blob = pf.tile_blob(li, entry)
                feats = pf.tile(li, entry)
                pts = sum(len(part) for f in feats for part in f[3])
                parts = sum(len(f[3]) for f in feats)
                big_blob = max(big_blob, len(blob))
                big_pts = max(big_pts, pts)
                big_feat = max(big_feat, len(feats), parts)
                over += len(blob) > BLOB_CAP or pts > POINT_CAP or len(feats) > FEATURE_CAP or parts > FEATURE_CAP
            print(f"  level {li}: tz {lv.tile_zoom} z{lv.zoom_min}-{lv.zoom_max}: {n} tiles; largest blob "
                  f"{big_blob} B, most points {big_pts}, most features/parts {big_feat}; over a limit: {over}")
            bad_total += over
    return 1 if bad_total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
