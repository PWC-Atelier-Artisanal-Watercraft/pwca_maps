"""Writes a synthetic water layer (PMT) for renderer benches and soak tests: fractal lakes with islands and a sea
along one side, cut into the three detail levels. It is invented geometry, not map data, and says so in its strings.

Usage: python tools/make_synthetic_pack.py [out.pmt] [--lat 45.0 --lon -93.3 --km 60x45 --seed 7]
       [--lakes 140 --rough 0.18]   (more lakes and a rougher shoreline make a heavier stress pack)
"""

import argparse
import math
import sys
import time

import numpy as np

import packgeom as pg
import pmt

LEVELS = [  # tile_zoom, zoom_min, zoom_max, coord_bits, buffer, tolerance (m)
    (10, 10, 11, 12, 32, 40.0),
    (12, 12, 13, 12, 32, 10.0),
    (12, 14, 16, 16, 512, 4.0),
]
M_PER_DEG = 111_320.0


def fractal_ring(rng, cx, cy, radius, min_seg, roughness=0.18, corners=10):
    """A closed shoreline-like ring (metres) by midpoint displacement until segments are shorter than min_seg."""
    t = np.sort(rng.uniform(0, 2 * math.pi, corners))
    r = radius * rng.uniform(0.6, 1.3, corners)
    x, y = cx + r * np.cos(t), cy + r * np.sin(t)
    while True:
        xn, yn = np.roll(x, -1), np.roll(y, -1)
        seg = np.hypot(xn - x, yn - y)
        if seg.max() < min_seg:
            return x, y
        off = seg * roughness * rng.uniform(-1, 1, seg.size)
        mx = (x + xn) / 2 - (yn - y) / np.maximum(seg, 1e-9) * off
        my = (y + yn) / 2 + (xn - x) / np.maximum(seg, 1e-9) * off
        x = np.stack([x, mx], axis=1).ravel()
        y = np.stack([y, my], axis=1).ravel()


def make_features(rng, w_m, h_m, lakes, rough):
    """Polygons in metres from the region's centre (x east, y NORTH): [(class, [outer, holes...])]."""
    feats = []
    # Sea: the west third, with a rough coastline running north-south.
    ys = np.linspace(-h_m / 2 - 2000, h_m / 2 + 2000, 12)
    xs = -w_m / 6 + rng.uniform(-3000, 3000, ys.size)
    coast_x, coast_y = [], []
    for i in range(ys.size - 1):  # roughen each coast segment by midpoint displacement
        px, py = np.array([xs[i], xs[i + 1]]), np.array([ys[i], ys[i + 1]])
        while np.hypot(np.diff(px), np.diff(py)).max() > 3.0:
            mx = (px[:-1] + px[1:]) / 2 + np.diff(py) * rough * rng.uniform(-1, 1, px.size - 1)
            my = (py[:-1] + py[1:]) / 2
            px = np.insert(px, np.arange(1, px.size), mx)
            py = np.insert(py, np.arange(1, py.size), my)
        coast_x.append(px[:-1])
        coast_y.append(py[:-1])
    cx, cy = np.concatenate(coast_x + [[xs[-1]]]), np.concatenate(coast_y + [[ys[-1]]])
    sea_x = np.concatenate([cx, [-w_m / 2 - 3000, -w_m / 2 - 3000]])
    sea_y = np.concatenate([cy, [ys[-1], ys[0]]])
    feats.append((1, [(sea_x, sea_y)]))
    # Lakes east of the coast, some with islands.
    for _ in range(lakes):
        r = float(np.exp(rng.uniform(math.log(150), math.log(3500))))
        lx = rng.uniform(-w_m / 6 + r + 3000, w_m / 2 - r)
        ly = rng.uniform(-h_m / 2 + r, h_m / 2 - r)
        rings = [fractal_ring(rng, lx, ly, r, min_seg=3.0, roughness=rough)]
        if r > 900:
            for _ in range(int(rng.integers(1, 4))):
                ir = r * rng.uniform(0.05, 0.18)
                a, d = rng.uniform(0, 2 * math.pi), rng.uniform(0, 0.45) * r
                rings.append(fractal_ring(rng, lx + d * math.cos(a), ly + d * math.sin(a), ir, min_seg=3.0,
                                          roughness=rough))
        feats.append((2, rings))
    return feats


def build(lat0, lon0, w_km, h_km, seed, lakes=140, rough=0.18):
    rng = np.random.default_rng(seed)
    w_m, h_m = w_km * 1000.0, h_km * 1000.0
    feats = make_features(rng, w_m, h_m, lakes, rough)
    cos0 = math.cos(math.radians(lat0))

    def to_world(mx, my, bits):
        return pg.world_xy(lon0 + mx / (M_PER_DEG * cos0), lat0 + my / M_PER_DEG, bits)

    # Region bounds in world units (for the clip of the sea and the coverage grid).
    levels = []
    for tz, zmin, zmax, bits, buf, tol_m in LEVELS:
        wb = tz + bits
        tol = tol_m * pg.units_per_metre(lat0, wb)
        bx0, by1 = to_world(-w_m / 2, -h_m / 2, wb)
        bx1, by0 = to_world(w_m / 2, h_m / 2, wb)
        tiles = {}
        for cls, rings in feats:
            world = []
            for k, (mx, my) in enumerate(rings):
                x, y = to_world(mx, my, wb)
                x, y = pg.clip_rect(x, y, bx0, by0, bx1, by1)  # the region is all there is
                if x.size < 3:
                    continue
                x, y = pg.simplify_ring(x, y, tol)
                r = pg.clean_ring(x, y)
                if r is None:
                    continue
                world.append(pg.orient(*r, outer=(k == 0)))
            if not world or pg.area2(*world[0]) <= 0:
                continue
            for key, parts in pg.cut_polygon(world, tz, bits, buf).items():
                tiles.setdefault(key, []).append(
                    (pmt.POLYGON, cls, None, [list(zip(x.tolist(), y.tolist())) for x, y in parts]))
        # Coverage: the zoom-8 cells the region touches are covered, and an absent tile there is land.
        s = wb - 8
        grid = {(cx, cy): 1 for cx in range(int(bx0) >> s, (int(bx1) >> s) + 1)
                for cy in range(int(by0) >> s, (int(by1) >> s) + 1)}
        levels.append({"tile_zoom": tz, "zoom_min": zmin, "zoom_max": zmax, "coord_bits": bits, "buffer": buf,
                       "tolerance_dm": int(tol_m * 10), "grid": grid, "tiles": tiles})
    strings = ["synthetic-water", "Synthetic test data (not a map)", "CC0 (test data)", "synthetic",
               time.strftime("%Y-%m-%d", time.gmtime()), "make_synthetic_pack.py"]
    ids = dict(zip(pmt.ID_FIELDS, range(6)))
    return pmt.build_pmt(layer_kind=pmt.KIND_WATER, levels=levels, strings=strings, ids=ids,
                         build_time=int(time.time()), data_time=int(time.time()))


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", nargs="?", default="out/synthetic_test.pmt")
    ap.add_argument("--lat", type=float, default=45.0)
    ap.add_argument("--lon", type=float, default=-93.3)
    ap.add_argument("--km", default="60x45")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--lakes", type=int, default=140)
    ap.add_argument("--rough", type=float, default=0.18)
    a = ap.parse_args(argv[1:])
    w_km, h_km = (float(v) for v in a.km.split("x"))
    t0 = time.time()
    data = build(a.lat, a.lon, w_km, h_km, a.seed, a.lakes, a.rough)
    with open(a.out, "wb") as f:
        f.write(data)
    pf = pmt.PmtFile(data)
    counts = ", ".join(f"L{i + 1} {lv.entry_count} tiles" for i, lv in enumerate(pf.levels))
    print(f"{a.out}: {len(data)} bytes ({counts}) in {time.time() - t0:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
