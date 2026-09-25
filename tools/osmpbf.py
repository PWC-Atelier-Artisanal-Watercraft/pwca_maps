"""A minimal streaming reader for OpenStreetMap PBF extracts (standard library + numpy): the header, relations with
their members and roles, ways with tags and node references, and the coordinates of chosen nodes.

It reads the file once per pass, as measure_water.py always has; nothing is kept but what the caller selects. The
*_parallel passes parse the data blocks in worker processes (the file is indexed first and each worker reads its own
blocks); their selectors must be module-level functions so they can be sent to the workers.
"""

import multiprocessing
import os
import struct
import tempfile
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


# ---- parallel passes ------------------------------------------------------------------------------------------------

def blob_index(path):
    """[(data offset, size, type)] of every blob, reading only the blob headers."""
    out = []
    with open(path, "rb") as f:
        while True:
            h = f.read(4)
            if len(h) < 4:
                return out
            hdr = f.read(struct.unpack(">I", h)[0])
            btype, size = None, 0
            for fn, v in fields(hdr):
                if fn == 1:
                    btype = bytes(v).decode()
                elif fn == 3:
                    size = v
            out.append((f.tell(), size, btype))
            f.seek(size, 1)


def _blob_data(f, offset, size):
    f.seek(offset)
    blob = f.read(size)
    for fn, v in fields(blob):
        if fn == 1:
            return bytes(v)
        if fn == 3:
            return zlib.decompress(v)
    return None


def _primitive(data):
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
    return strings, groups, gran, lat_off, lon_off


def _run_chunk(job):
    """Worker: one chunk of data blocks through `func`, which returns a list per block."""
    path, chunk, func, args = job
    out = []
    with open(path, "rb") as f:
        for offset, size in chunk:
            data = _blob_data(f, offset, size)
            if data is not None:
                out.extend(func(_primitive(data), *args))
    return out


def map_blocks(path, func, args=(), processes=1):
    """func(primitive block, *args) -> list over every data block, results concatenated in file order."""
    blocks = [(o, s) for o, s, t in blob_index(path) if t == "OSMData"]
    if processes <= 1:
        return _run_chunk((path, blocks, func, args))
    # Small chunks: relations sit at the end of the file, so large chunks leave one worker with all of them.
    n = max(1, len(blocks) // (processes * 64))
    jobs = [(path, blocks[i:i + n], func, args) for i in range(0, len(blocks), n)]
    out = []
    with multiprocessing.get_context("spawn").Pool(processes) as pool:
        for part in pool.imap(_run_chunk, jobs):
            out.extend(part)
    return out


def _relations_in(block, want):
    strings, groups, *_ = block
    out = []
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
                out.append((rid, tags, [(int(t), int(m), strings[int(r)]) for t, m, r in zip(types, memids, roles)]))
    return out


def _ways_in(block, classify, extra_path):
    strings, groups, *_ = block
    extra = np.load(extra_path, mmap_mode="r") if extra_path else np.zeros(0, np.int64)
    out = []
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
            kind = classify(_tags(keys, vals, strings))
            if kind is None and extra.size:
                i = int(np.searchsorted(extra, wid))
                if i < extra.size and extra[i] == wid:
                    kind = ()
            if kind is not None:
                out.append((wid, kind, np.cumsum(zigzag(varints(refs)))))
    return out


def _nodes_in(block, needed_path, cell_zoom):
    strings, groups, gran, lat_off, lon_off = block
    needed = np.load(needed_path, mmap_mode="r")
    out = []
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
            cells = set()
            if cell_zoom is not None:
                n = 1 << cell_zoom
                cx = np.clip(((dlon + 180.0) / 360.0 * n).astype(np.int64), 0, n - 1)
                s = np.sin(np.radians(np.clip(dlat, -85.0511, 85.0511)))
                cy = np.clip(((0.5 - np.log((1 + s) / (1 - s)) / (4 * np.pi)) * n).astype(np.int64), 0, n - 1)
                cells = set(np.unique(cx * n + cy).tolist())
            if needed.size:
                pos = np.searchsorted(needed, ids)
                pos[pos >= needed.size] = 0
                hit = needed[pos] == ids
                out.append((pos[hit], dlat[hit], dlon[hit], cells))
            else:
                out.append((np.zeros(0, np.int64), np.zeros(0), np.zeros(0), cells))
    return out


def imap_blocks(path, func, args=(), processes=1):
    """Like map_blocks, but yields each chunk's results (in file order) as they arrive, for passes whose results the
    caller folds into arrays instead of keeping them all."""
    blocks = [(o, s) for o, s, t in blob_index(path) if t == "OSMData"]
    n = max(1, len(blocks) // (max(1, processes) * 32))
    jobs = [(path, blocks[i:i + n], func, args) for i in range(0, len(blocks), n)]
    if processes <= 1:
        for job in jobs:
            yield _run_chunk(job)
        return
    with multiprocessing.get_context("spawn").Pool(processes) as pool:
        yield from pool.imap(_run_chunk, jobs)


def _e7(nano):
    """Nanodegrees (int64) to 1e-7 degrees (int32), rounded; exact for OSM's usual granularity of 100."""
    q, r = np.divmod(nano, 100)
    return (q + (r >= 50)).astype(np.int32)


def _nodes_e7_in(block, needed_path, cell_zoom):
    strings, groups, gran, lat_off, lon_off = block
    needed = np.load(needed_path, mmap_mode="r")
    out = []
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
            nlat = lat_off + gran * la
            nlon = lon_off + gran * lo
            cells = set()
            if cell_zoom is not None:
                dlat, dlon = 1e-9 * nlat, 1e-9 * nlon
                n = 1 << cell_zoom
                cx = np.clip(((dlon + 180.0) / 360.0 * n).astype(np.int64), 0, n - 1)
                s = np.sin(np.radians(np.clip(dlat, -85.0511, 85.0511)))
                cy = np.clip(((0.5 - np.log((1 + s) / (1 - s)) / (4 * np.pi)) * n).astype(np.int64), 0, n - 1)
                cells = set(np.unique(cx * n + cy).tolist())
            if needed.size:
                pos = np.searchsorted(needed, ids)
                pos[pos >= needed.size] = 0
                hit = needed[pos] == ids
                out.append((pos[hit], _e7(nlat[hit]), _e7(nlon[hit]), cells))
            else:
                out.append((np.zeros(0, np.int64), np.zeros(0, np.int32), np.zeros(0, np.int32), cells))
    return out


def nodes_e7_parallel(path, needed, processes, cell_zoom=None):
    """Like nodes_parallel, with the coordinates in 1e-7 degrees (int32, INT32_MIN where missing) and folded into the
    arrays as the workers deliver them: 8 bytes per needed node instead of 16, and no list of every block's result."""
    missing = np.iinfo(np.int32).min
    lat = np.full(needed.size, missing, np.int32)
    lon = np.full(needed.size, missing, np.int32)
    cells = set()
    with tempfile.TemporaryDirectory() as d:
        needed_path = os.path.join(d, "needed.npy")
        np.save(needed_path, needed)
        for part in imap_blocks(path, _nodes_e7_in, (needed_path, cell_zoom), processes):
            for pos, la, lo, c in part:
                lat[pos] = la
                lon[pos] = lo
                cells |= c
    n = 1 << (cell_zoom or 0)
    return lat, lon, ({(c // n, c % n) for c in cells} if cell_zoom is not None else None)


def relations_parallel(path, want, processes):
    """Like relations(); `want` must be a module-level function."""
    return map_blocks(path, _relations_in, (want,), processes)


def ways_parallel(path, classify, extra_ids, processes):
    """[(way id, classify(tags), refs)] for ways that classify accepts (anything but None), plus the ways whose id is in
    the sorted array extra_ids with kind (); classify must be a module-level function."""
    with tempfile.TemporaryDirectory() as d:
        extra_path = None
        if extra_ids is not None and len(extra_ids):
            extra_path = os.path.join(d, "extra.npy")
            np.save(extra_path, np.asarray(extra_ids, np.int64))
        return map_blocks(path, _ways_in, (classify, extra_path), processes)


def nodes_parallel(path, needed, processes, cell_zoom=None):
    """Like nodes(), with `needed` shared with the workers through a memory-mapped file."""
    lat = np.full(needed.size, np.nan)
    lon = np.full(needed.size, np.nan)
    cells = set()
    with tempfile.TemporaryDirectory() as d:
        needed_path = os.path.join(d, "needed.npy")
        np.save(needed_path, needed)
        for pos, la, lo, c in map_blocks(path, _nodes_in, (needed_path, cell_zoom), processes):
            lat[pos] = la
            lon[pos] = lo
            cells |= c
    if cell_zoom is not None:
        n = 1 << cell_zoom
        return lat, lon, {(c // n, c % n) for c in cells}
    return lat, lon
