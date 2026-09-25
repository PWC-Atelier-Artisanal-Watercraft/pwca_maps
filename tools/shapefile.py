"""A minimal ESRI shapefile reader (standard library + numpy) for the Natural Earth and OSM water-polygon downloads:
polygon and polyline records from the .shp, attributes from the .dbf, read straight out of the .zip.

Rings follow the ESRI rule: in lon/lat (y up) an outer ring runs clockwise and a hole counter-clockwise.
"""

import struct
import zipfile
from pathlib import PurePosixPath

import numpy as np

POLYLINE_TYPES = {3, 13, 23}
POLYGON_TYPES = {5, 15, 25}


def _members(zf, stem=None):
    names = zf.namelist()
    shp = [n for n in names if n.lower().endswith(".shp") and (stem is None or PurePosixPath(n).stem == stem)]
    if len(shp) != 1:
        raise ValueError(f"expected one .shp in the archive, found {shp}")
    base = shp[0][:-4]
    dbf = next((n for n in names if n.lower() == (base + ".dbf").lower()), None)
    return shp[0], dbf


def read_dbf(data):
    """[{field: value}] from dBase III bytes; numbers become float or int, text is stripped."""
    nrec, hlen, rlen = struct.unpack_from("<IHH", data, 4)
    fields, pos = [], 32
    while data[pos] != 0x0D:
        name = data[pos:pos + 11].split(b"\0", 1)[0].decode("ascii", "replace")
        ftype, flen, fdec = chr(data[pos + 11]), data[pos + 16], data[pos + 17]
        fields.append((name, ftype, flen, fdec))
        pos += 32
    out = []
    for i in range(nrec):
        rec = data[hlen + i * rlen:hlen + (i + 1) * rlen]
        if rec[:1] == b"*":  # deleted
            out.append(None)
            continue
        row, off = {}, 1
        for name, ftype, flen, fdec in fields:
            raw = rec[off:off + flen]
            off += flen
            text = raw.decode("utf-8", "replace").strip()
            if ftype in "NF":
                try:
                    row[name] = (float(text) if (fdec or "." in text) else int(text)) if text else None
                except ValueError:
                    row[name] = None
            else:
                row[name] = text
        out.append(row)
    return out


def records(data):
    """Yields (shape type, [(lon array, lat array) per part]) for each record of .shp bytes (null shapes skipped)."""
    pos = 100
    n = len(data)
    while pos + 8 <= n:
        _num, clen = struct.unpack_from(">ii", data, pos)
        body = pos + 8
        pos = body + 2 * clen
        stype = struct.unpack_from("<i", data, body)[0]
        if stype not in POLYGON_TYPES and stype not in POLYLINE_TYPES:
            yield stype, []
            continue
        nparts, npoints = struct.unpack_from("<ii", data, body + 36)
        parts = np.frombuffer(data, "<i4", nparts, body + 44).tolist() + [npoints]
        pts = np.frombuffer(data, "<f8", 2 * npoints, body + 44 + 4 * nparts).reshape(-1, 2)
        yield stype, [(pts[a:b, 0].copy(), pts[a:b, 1].copy()) for a, b in zip(parts, parts[1:])]


def records_in_box(path, box, stem=None):
    """Streams (attributes-free) polygon/polyline records of a zipped .shp whose bounding box meets
    box = (south, west, north, east), without reading the whole file into memory; yields (shape type, parts)."""
    s, w, n, e = box
    with zipfile.ZipFile(path) as zf:
        shp, _dbf = _members(zf, stem)
        with zf.open(shp) as f:
            f.read(100)
            while True:
                head = f.read(8)
                if len(head) < 8:
                    return
                _num, clen = struct.unpack(">ii", head)
                body = f.read(2 * clen)
                stype = struct.unpack_from("<i", body, 0)[0]
                if stype not in POLYGON_TYPES and stype not in POLYLINE_TYPES:
                    continue
                xmin, ymin, xmax, ymax = struct.unpack_from("<4d", body, 4)
                if xmax < w or xmin > e or ymax < s or ymin > n:
                    continue
                nparts, npoints = struct.unpack_from("<ii", body, 36)
                parts = np.frombuffer(body, "<i4", nparts, 44).tolist() + [npoints]
                pts = np.frombuffer(body, "<f8", 2 * npoints, 44 + 4 * nparts).reshape(-1, 2)
                yield stype, [(pts[a:b, 0].copy(), pts[a:b, 1].copy()) for a, b in zip(parts, parts[1:])]


def read_zip(path, stem=None):
    """[(attributes or None, shape type, parts)] from a zipped shapefile."""
    with zipfile.ZipFile(path) as zf:
        shp, dbf = _members(zf, stem)
        shapes = list(records(zf.read(shp)))
        attrs = read_dbf(zf.read(dbf)) if dbf else [None] * len(shapes)
    return [(a, t, parts) for a, (t, parts) in zip(attrs, shapes)]


def ring_is_outer(lon, lat):
    """ESRI rule: clockwise in lon/lat (y up) is an outer ring."""
    return float(np.dot(lon, np.roll(lat, -1)) - np.dot(np.roll(lon, -1), lat)) < 0


# ---- a writer, for tests ------------------------------------------------------------------------------------------

def write_zip(path, shapes, fields=(), rows=()):
    """Writes a zipped shapefile. shapes: [(shape type, [(lon list, lat list)])]; fields: [(name, 'C' or 'N', len)]."""
    recs = []
    for i, (stype, parts) in enumerate(shapes, 1):
        xs = np.concatenate([np.asarray(p[0], float) for p in parts])
        ys = np.concatenate([np.asarray(p[1], float) for p in parts])
        starts, s = [], 0
        for p in parts:
            starts.append(s)
            s += len(p[0])
        body = struct.pack("<i4d2i", stype, xs.min(), ys.min(), xs.max(), ys.max(), len(parts), len(xs))
        body += struct.pack(f"<{len(parts)}i", *starts)
        body += np.column_stack([xs, ys]).astype("<f8").tobytes()
        recs.append(struct.pack(">ii", i, len(body) // 2) + body)
    content = b"".join(recs)
    header = struct.pack(">i5ii", 9994, 0, 0, 0, 0, 0, (100 + len(content)) // 2) + struct.pack("<ii", 1000, 5)
    header += struct.pack("<8d", 0, 0, 0, 0, 0, 0, 0, 0)
    dbf = bytearray(struct.pack("<BBBBIHH", 3, 126, 1, 1, len(shapes), 32 + 32 * len(fields) + 1,
                                1 + sum(f[2] for f in fields)))
    dbf += bytes(20)
    for name, ftype, flen in fields:
        dbf += name.encode("ascii").ljust(11, b"\0") + ftype.encode() + bytes(4) + bytes([flen, 0]) + bytes(14)
    dbf += b"\x0D"
    for row in rows:
        dbf += b" " + b"".join(str(row.get(name, "")).encode().ljust(flen)[:flen] for name, _, flen in fields)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("test.shp", header + content)
        zf.writestr("test.dbf", bytes(dbf))
