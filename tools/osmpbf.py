"""A minimal streaming reader for OpenStreetMap PBF extracts (standard library + numpy): the header, relations with
their members and roles, ways with tags and node references, and the coordinates of chosen nodes.

It reads the file once per pass, as measure_water.py always has; nothing is kept but what the caller selects.
"""

import struct
import zlib

import numpy as np


def read_varint(buf, pos):
    result = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, pos
        shift += 7


def fields(buf):
    """(field number, value) pairs of a protobuf message; value is an int or a memoryview."""
    buf = memoryview(buf)
    pos, n = 0, len(buf)
    while pos < n:
        key, pos = read_varint(buf, pos)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, pos = read_varint(buf, pos)
        elif wt == 2:
            ln, pos = read_varint(buf, pos)
            v = buf[pos:pos + ln]
            pos += ln
        elif wt == 1:
            v = buf[pos:pos + 8]
            pos += 8
        elif wt == 5:
            v = buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError(f"wire type {wt}")
        yield fn, v


def varints(b):
    """All the varints of a packed field, vectorised."""
    a = np.frombuffer(b, dtype=np.uint8)
    if a.size == 0:
        return np.zeros(0, np.uint64)
    ends = np.flatnonzero(a < 0x80)
    starts = np.concatenate(([0], ends[:-1] + 1))
    grp = np.repeat(np.arange(ends.size), ends - starts + 1)
    k = (np.arange(a.size) - starts[grp]).astype(np.uint64)
    return np.add.reduceat((a & 0x7F).astype(np.uint64) << (np.uint64(7) * k), starts)


def zigzag(v):
    return (v >> np.uint64(1)).astype(np.int64) ^ -((v & np.uint64(1)).astype(np.int64))


def blobs(path):
    """(type, decompressed data) of each blob: 'OSMHeader' first, then 'OSMData'."""
    with open(path, "rb") as f:
        while True:
            h = f.read(4)
            if len(h) < 4:
                return
            hdr = f.read(struct.unpack(">I", h)[0])
            btype, size = None, 0
            for fn, v in fields(hdr):
                if fn == 1:
                    btype = bytes(v).decode()
                elif fn == 3:
                    size = v
            blob = f.read(size)
            data = None
            for fn, v in fields(blob):
                if fn == 1:
                    data = bytes(v)
                elif fn == 3:
                    data = zlib.decompress(v)
            if data is not None:
                yield btype, data


def header(path):
    """The file header: {'replication_timestamp': int or None, 'writing_program': str, 'source': str}."""
    for btype, data in blobs(path):
        if btype != "OSMHeader":
            break
        out = {"replication_timestamp": None, "writing_program": "", "source": ""}
        for fn, v in fields(data):
            if fn == 32:
                out["replication_timestamp"] = v
            elif fn == 16:
                out["writing_program"] = bytes(v).decode("utf-8", "replace")
            elif fn == 17:
                out["source"] = bytes(v).decode("utf-8", "replace")
        return out
    return {"replication_timestamp": None, "writing_program": "", "source": ""}


def primitive_blocks(path):
    """(strings, groups, granularity, lat_offset, lon_offset) per data block."""
    for btype, data in blobs(path):
        if btype != "OSMData":
            continue
        strings, groups, gran, lat_off, lon_off = [], [], 100, 0, 0
        for fn, v in fields(data):
            if fn == 1:
                strings = [bytes(s).decode("utf-8", "replace") for f2, s in fields(v) if f2 == 1]
            elif fn == 2:
                groups.append(v)
            elif fn == 17:
                gran = v
            elif fn == 19:
                lat_off = v
            elif fn == 20:
                lon_off = v
        yield strings, groups, gran, lat_off, lon_off


def _tags(keys, vals, strings):
    return {strings[int(k)]: strings[int(v)] for k, v in zip(keys, vals)}


def relations(path, want):
    """[(relation id, tags, [(member type, member id, role)])] for relations whose tags `want` accepts.
    Member types: 0 node, 1 way, 2 relation."""
    out = []
    for strings, groups, *_ in primitive_blocks(path):
        for g in groups:
            for fn, rel in fields(g):
                if fn != 4:
                    continue
                rid = 0
                keys = vals = roles = memids = types = np.zeros(0, np.uint64)
                for f2, v in fields(rel):
                    if f2 == 1:
                        rid = v
                    elif f2 == 2:
                        keys = varints(v)
                    elif f2 == 3:
                        vals = varints(v)
                    elif f2 == 8:
                        roles = varints(v)
                    elif f2 == 9:
                        memids = np.cumsum(zigzag(varints(v)))
                    elif f2 == 10:
                        types = varints(v)
                tags = _tags(keys, vals, strings)
                if want(tags):
                    members = [(int(t), int(m), strings[int(r)]) for t, m, r in zip(types, memids, roles)]
                    out.append((rid, tags, members))
    return out


def ways(path, want, extra_ids=None):
    """[(way id, tags, node refs)] for ways whose tags `want` accepts, or whose id is in the sorted array
    `extra_ids` (e.g. members of wanted relations)."""
    out = []
    extra = extra_ids if extra_ids is not None else np.zeros(0, np.int64)
    for strings, groups, *_ in primitive_blocks(path):
        for g in groups:
            for fn, w in fields(g):
                if fn != 3:
                    continue
                wid, keys, vals, refs = 0, np.zeros(0, np.uint64), np.zeros(0, np.uint64), None
                for f2, v in fields(w):
                    if f2 == 1:
                        wid = v
                    elif f2 == 2:
                        keys = varints(v)
                    elif f2 == 3:
                        vals = varints(v)
                    elif f2 == 8:
                        refs = v
                if refs is None:
                    continue
                tags = _tags(keys, vals, strings)
                ok = want(tags)
                if not ok and extra.size:
                    i = np.searchsorted(extra, wid)
                    ok = i < extra.size and extra[i] == wid
                if ok:
                    out.append((wid, tags, np.cumsum(zigzag(varints(refs)))))
    return out


def nodes(path, needed, cell_zoom=None):
    """(lat, lon) arrays for the sorted node ids `needed` (NaN where missing). With cell_zoom, also the set of
    Web Mercator tiles at that zoom holding any node of the file (where the extract has data)."""
    lat = np.full(needed.size, np.nan)
    lon = np.full(needed.size, np.nan)
    cells = set()
    for strings, groups, gran, lat_off, lon_off in primitive_blocks(path):
        for g in groups:
            for fn, dense in fields(g):
                if fn != 2:
                    continue
                ids = la = lo = None
                for f2, v in fields(dense):
                    if f2 == 1:
                        ids = np.cumsum(zigzag(varints(v)))
                    elif f2 == 8:
                        la = np.cumsum(zigzag(varints(v)))
                    elif f2 == 9:
                        lo = np.cumsum(zigzag(varints(v)))
                if ids is None:
                    continue
                dlat = 1e-9 * (lat_off + gran * la)
                dlon = 1e-9 * (lon_off + gran * lo)
                if cell_zoom is not None:
                    n = 1 << cell_zoom
                    cx = np.clip(((dlon + 180.0) / 360.0 * n).astype(np.int64), 0, n - 1)
                    s = np.sin(np.radians(np.clip(dlat, -85.0511, 85.0511)))
                    cy = np.clip(((0.5 - np.log((1 + s) / (1 - s)) / (4 * np.pi)) * n).astype(np.int64), 0, n - 1)
                    cells.update(np.unique(cx * n + cy).tolist())
                if needed.size:
                    pos = np.searchsorted(needed, ids)
                    pos[pos >= needed.size] = 0
                    hit = needed[pos] == ids
                    lat[pos[hit]] = dlat[hit]
                    lon[pos[hit]] = dlon[hit]
    if cell_zoom is not None:
        n = 1 << cell_zoom
        return lat, lon, {(c // n, c % n) for c in cells}
    return lat, lon
