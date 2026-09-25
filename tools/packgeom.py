"""Map pack geometry: Web Mercator world units, Douglas-Peucker simplification, ring orientation, and cutting polygons
into buffered tiles as FORMAT.md section 6 describes. numpy only.

World units: at a level with tile zoom tz and coordinate bits b, the world is 2^(tz + b) units wide, x to the east and
y to the south. Tile (tx, ty) covers [tx << b, (tx + 1) << b) on x, and likewise on y.
"""

import math

import numpy as np

EARTH_R = 6_378_137.0
MAX_LAT = 85.0511287798066


def world_xy(lon, lat, world_bits):
    """Web Mercator world coordinates (floats) of lon/lat degrees; the world is 2^world_bits units wide."""
    scale = float(1 << world_bits)
    x = (np.asarray(lon, float) + 180.0) / 360.0 * scale
    s = np.sin(np.radians(np.clip(np.asarray(lat, float), -MAX_LAT, MAX_LAT)))
    y = (0.5 - np.log((1 + s) / (1 - s)) / (4 * math.pi)) * scale
    return x, y


def lonlat(x, y, world_bits):
    """The inverse of world_xy."""
    scale = float(1 << world_bits)
    lon = np.asarray(x, float) / scale * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(math.pi * (1 - 2 * np.asarray(y, float) / scale))))
    return lon, lat


def units_per_metre(lat, world_bits):
    """World units per ground metre at a latitude."""
    return (1 << world_bits) / (2 * math.pi * EARTH_R * math.cos(math.radians(lat)))


# ---- simplification and rings ---------------------------------------------------------------------------------------

def douglas_peucker(x, y, tol):
    """Keep mask of an open polyline simplified to `tol` (same units as x, y); the end points are always kept."""
    n = x.size
    if n <= 2:
        return np.ones(n, bool)
    keep = np.zeros(n, bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        dx, dy = x[b] - x[a], y[b] - y[a]
        seg = math.hypot(dx, dy)
        px, py = x[a + 1:b] - x[a], y[a + 1:b] - y[a]
        d = np.abs(px * dy - py * dx) / seg if seg > 0 else np.hypot(px, py)
        i = int(np.argmax(d))
        if d[i] > tol:
            m = a + 1 + i
            keep[m] = True
            stack.append((a, m))
            stack.append((m, b))
    return keep


def simplify_ring(x, y, tol):
    """A closed ring (first point not repeated) simplified to `tol`, anchored at point 0 and the farthest point."""
    n = x.size
    if n <= 3:
        return x, y
    k = int(np.argmax((x - x[0]) ** 2 + (y - y[0]) ** 2))
    if k == 0:
        return x[:1], y[:1]
    keep_a = douglas_peucker(x[:k + 1], y[:k + 1], tol)
    keep_b = douglas_peucker(np.append(x[k:], x[0]), np.append(y[k:], y[0]), tol)
    keep = np.concatenate([keep_a, keep_b[1:-1]])
    return x[keep], y[keep]


def area2(x, y):
    """Twice the signed area; positive when the ring runs clockwise on screen (y down), as outer rings must."""
    return float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))


def clean_ring(x, y):
    """Integer ring without repeated consecutive points (including last == first); None if degenerate."""
    x = np.asarray(np.rint(x), np.int64)
    y = np.asarray(np.rint(y), np.int64)
    if x.size:
        keep = (x != np.roll(x, 1)) | (y != np.roll(y, 1))
        x, y = x[keep], y[keep]
    if x.size < 3 or area2(x, y) == 0:
        return None
    return x, y


def orient(x, y, outer):
    """Outer rings clockwise on screen (positive area2), holes the other way (FORMAT.md section 6)."""
    if (area2(x, y) > 0) != outer:
        return x[::-1], y[::-1]
    return x, y


# ---- clipping -------------------------------------------------------------------------------------------------------

def clip_half(x, y, axis, value, keep_greater):
    """Sutherland-Hodgman against one axis-aligned half-plane, vectorised. Points on the line count as inside."""
    c = x if axis == 0 else y
    inside = c >= value if keep_greater else c <= value
    if inside.all():
        return x, y
    if not inside.any():
        return x[:0], y[:0]
    xn, yn, cn = np.roll(x, -1), np.roll(y, -1), np.roll(c, -1)
    cross = inside != np.roll(inside, -1)
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(cross, (value - c) / np.where(cross, cn - c, 1), 0)
    ix, iy = x + t * (xn - x), y + t * (yn - y)
    if axis == 0:
        ix = np.where(cross, value, ix)
    else:
        iy = np.where(cross, value, iy)
    mask = np.stack([inside, cross], axis=1).ravel()
    return np.stack([x, ix], axis=1).ravel()[mask], np.stack([y, iy], axis=1).ravel()[mask]


def clip_rect(x, y, x0, y0, x1, y1):
    for axis, value, greater in ((0, x0, True), (0, x1, False), (1, y0, True), (1, y1, False)):
        x, y = clip_half(x, y, axis, value, greater)
        if x.size == 0:
            break
    return x, y


def _is_rect(x, y, x0, y0, x1, y1):
    return x.size == 4 and set(np.unique(x)) == {x0, x1} and set(np.unique(y)) == {y0, y1}


def _overlaps(z, nx, ny, region):
    """Whether quadtree node (z, nx, ny) and the region node (rz, rx, ry) overlap (one contains the other)."""
    if region is None:
        return True
    rz, rx, ry = region
    if z <= rz:
        return (rx >> (rz - z)) == nx and (ry >> (rz - z)) == ny
    return (nx >> (z - rz)) == rx and (ny >> (z - rz)) == ry


def cut_polygon(rings, tile_zoom, coord_bits, buffer, region=None):
    """Cuts one polygon into buffered tiles.

    rings: a list of (x, y) integer world-unit arrays at world bits tile_zoom + coord_bits, already oriented (outer
    rings clockwise on screen, holes the other way). Returns {(tx, ty): [(x, y) tile-local int arrays]}; each ring is
    clipped to the tile grown by `buffer` units and keeps its orientation. A node that the polygon covers completely is
    not clipped further: its tiles get the buffered square. With region = (rz, rx, ry) (rz <= tile_zoom), only the
    tiles inside that quadtree node are made; each is the same as without the region.
    """
    rings = [(np.asarray(x, np.float64), np.asarray(y, np.float64)) for x, y in rings]
    if not rings:
        return {}
    side = 1 << coord_bits
    xmin = min(float(x.min()) for x, _ in rings)
    xmax = max(float(x.max()) for x, _ in rings)
    ymin = min(float(y.min()) for _, y in rings)
    ymax = max(float(y.max()) for _, y in rings)
    # The deepest node (zoom z, index nx, ny) holding the whole bounding box.
    z = tile_zoom
    tx0, ty0 = int(xmin) >> coord_bits, int(ymin) >> coord_bits
    tx1, ty1 = int(xmax) >> coord_bits, int(ymax) >> coord_bits
    while z > 0 and (tx0 != tx1 or ty0 != ty1):
        z -= 1
        tx0, ty0, tx1, ty1 = tx0 >> 1, ty0 >> 1, tx1 >> 1, ty1 >> 1
    out = {}
    _cut_node(rings, z, tx0, ty0, tile_zoom, coord_bits, buffer, side, out, region)
    return out


def _cut_node(rings, z, nx, ny, tile_zoom, coord_bits, buffer, side, out, region=None):
    if not _overlaps(z, nx, ny, region):
        return
    shift = coord_bits + tile_zoom - z  # node side = 2^shift world units
    x0, y0 = (nx << shift) - buffer, (ny << shift) - buffer
    x1, y1 = ((nx + 1) << shift) + buffer, ((ny + 1) << shift) + buffer
    kept = []
    for x, y in rings:
        cx, cy = clip_rect(x, y, x0, y0, x1, y1)
        if cx.size >= 3:
            kept.append((cx, cy))
    if not kept:
        return
    if len(kept) == 1 and _is_rect(*kept[0], x0, y0, x1, y1) and area2(*kept[0]) > 0:
        # Fully inside the polygon: every tile below is the full buffered square (only the region's, if it is smaller).
        fz, fx, fy = (z, nx, ny) if region is None or z >= region[0] else region
        n = 1 << (tile_zoom - fz)
        sq = (np.array([-buffer, side + buffer, side + buffer, -buffer], np.int64),
              np.array([-buffer, -buffer, side + buffer, side + buffer], np.int64))
        for dx in range(n):
            for dy in range(n):
                out.setdefault(((fx << (tile_zoom - fz)) + dx, (fy << (tile_zoom - fz)) + dy), []).append(sq)
        return
    if z == tile_zoom:
        ox, oy = nx << coord_bits, ny << coord_bits
        for cx, cy in kept:
            ring = clean_ring(cx - ox, cy - oy)
            if ring is not None:
                out.setdefault((nx, ny), []).append(ring)
        return
    for dx in (0, 1):
        for dy in (0, 1):
            _cut_node(kept, z + 1, 2 * nx + dx, 2 * ny + dy, tile_zoom, coord_bits, buffer, side, out, region)


def clip_line_half(x, y, axis, value, keep_greater):
    """Clips an open polyline to one axis-aligned half-plane; returns a list of (x, y) parts (points on the line count
    as inside). Vectorised like clip_half: each point is kept if inside, and each crossing segment adds its
    intersection, which also ends a part when the segment leaves."""
    c = x if axis == 0 else y
    inside = c >= value if keep_greater else c <= value
    if inside.all():
        return [(x, y)]
    if not inside.any():
        return []
    n = x.size
    cross = np.zeros(n, bool)
    cross[:-1] = inside[:-1] != inside[1:]
    xn, yn, cn = np.roll(x, -1), np.roll(y, -1), np.roll(c, -1)
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(cross, (value - c) / np.where(cross, cn - c, 1), 0)
    ix, iy = x + t * (xn - x), y + t * (yn - y)
    if axis == 0:
        ix = np.where(cross, value, ix)
    else:
        iy = np.where(cross, value, iy)
    leaving = cross & inside  # the part ends at this segment's intersection
    mask = np.stack([inside, cross], axis=1).ravel()
    px = np.stack([x, ix], axis=1).ravel()[mask]
    py = np.stack([y, iy], axis=1).ravel()[mask]
    ends = np.stack([np.zeros(n, bool), leaving], axis=1).ravel()[mask]
    cuts = np.flatnonzero(ends) + 1
    return [(a, b) for a, b in zip(np.split(px, cuts), np.split(py, cuts)) if a.size >= 2]


def clip_line_rect(x, y, x0, y0, x1, y1):
    parts = [(x, y)]
    for axis, value, greater in ((0, x0, True), (0, x1, False), (1, y0, True), (1, y1, False)):
        parts = [q for px, py in parts for q in clip_line_half(px, py, axis, value, greater)]
        if not parts:
            break
    return parts


def clean_line(x, y):
    """Integer polyline without repeated consecutive points; None if fewer than 2 points remain."""
    x = np.asarray(np.rint(x), np.int64)
    y = np.asarray(np.rint(y), np.int64)
    if x.size:
        keep = np.ones(x.size, bool)
        keep[1:] = (x[1:] != x[:-1]) | (y[1:] != y[:-1])
        x, y = x[keep], y[keep]
    return (x, y) if x.size >= 2 else None


def cut_line(parts, tile_zoom, coord_bits, buffer, region=None):
    """Cuts polylines (a list of (x, y) integer world-unit arrays, as cut_polygon) into buffered tiles:
    {(tx, ty): [(x, y) tile-local int arrays]}; `region` as in cut_polygon."""
    parts = [(np.asarray(x, np.float64), np.asarray(y, np.float64)) for x, y in parts if len(x) >= 2]
    if not parts:
        return {}
    xmin = min(float(x.min()) for x, _ in parts)
    xmax = max(float(x.max()) for x, _ in parts)
    ymin = min(float(y.min()) for _, y in parts)
    ymax = max(float(y.max()) for _, y in parts)
    z = tile_zoom
    tx0, ty0 = int(xmin) >> coord_bits, int(ymin) >> coord_bits
    tx1, ty1 = int(xmax) >> coord_bits, int(ymax) >> coord_bits
    while z > 0 and (tx0 != tx1 or ty0 != ty1):
        z -= 1
        tx0, ty0, tx1, ty1 = tx0 >> 1, ty0 >> 1, tx1 >> 1, ty1 >> 1
    out = {}
    _cut_line_node(parts, z, tx0, ty0, tile_zoom, coord_bits, buffer, out, region)
    return out


def _cut_line_node(parts, z, nx, ny, tile_zoom, coord_bits, buffer, out, region=None):
    if not _overlaps(z, nx, ny, region):
        return
    shift = coord_bits + tile_zoom - z
    x0, y0 = (nx << shift) - buffer, (ny << shift) - buffer
    x1, y1 = ((nx + 1) << shift) + buffer, ((ny + 1) << shift) + buffer
    kept = [q for x, y in parts for q in clip_line_rect(x, y, x0, y0, x1, y1)]
    if not kept:
        return
    if z == tile_zoom:
        ox, oy = nx << coord_bits, ny << coord_bits
        for cx, cy in kept:
            line = clean_line(cx - ox, cy - oy)
            if line is not None:
                out.setdefault((nx, ny), []).append(line)
        return
    for dx in (0, 1):
        for dy in (0, 1):
            _cut_line_node(kept, z + 1, 2 * nx + dx, 2 * ny + dy, tile_zoom, coord_bits, buffer, out, region)
