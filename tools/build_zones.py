"""Builds a zones layer (PMT layer kind 3, FORMAT.md section 7.3) from the Florida Fish and Wildlife Conservation
Commission's public GIS layers: State Boating Safety Zones (Florida Administrative Code 68D-24) and State Manatee
Protection Zones (FAC 68C-22), as GeoJSON from gis.myfwc.com (ArcGIS query with outSR=4326, f=geojson).

Usage: python tools/build_zones.py <out_dir> --boating boating.geojson --manatee manatee.geojson --date YYYY-MM

Zone data is advisory and never complete (county and city zones are not in these layers); the display says so. The
mapping errs towards caution:
- boating zones: "Idle Speed No Wake" -> class 1, "Slow Speed Minimum Wake" -> 2, "Max Speed N MPH" -> 3 (limit),
  "No Entry" -> 4; "Conditional" ones carry the partial-times flag; anchoring and propeller rules are not speed zones
  and are left out;
- manatee zones: each seasonal pair (CLASS1_68C in DATE1_68C, CLASS2_68C in DATE2_68C) becomes one zone per season
  (the same area twice when both seasons restrict); IS/ISAY idle -> 1, SS/SSAY slow -> 2, "N MPH" -> 3, NE/NEAY and
  MP/MPAY (motorboats prohibited) -> 4, UN (unregulated) -> no zone; a season that starts or ends mid-month covers the
  whole month and carries the partial-times flag; "SS WKND" is Saturdays and Sundays; "SSAY_25CH" is slow speed with a
  25 mph channel (flag bit 0); "35 DY/25 N" is a 35 mph limit, 25 at night (partial times).
The coverage grid marks the zoom-8 cells holding any zone: elsewhere the display says "NO NO-WAKE DATA HERE".
"""

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import numpy as np

import packgeom as pg
import pmt

LEVELS = [  # tile_zoom, zoom_min, zoom_max, coord_bits, buffer, tolerance (m)
    (10, 10, 11, 12, 32, 10.0),
    (12, 12, 16, 16, 512, 1.0),
]
MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV",
                                      "DEC"])}
MONTH_DAYS = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
MPH_DKPH = 16.09344  # 1 mph in 0.1 km/h
FLAG_CHANNEL, FLAG_PARTIAL = 1, 2
WEEKEND = 0b1100000  # bit 0 Monday


def season(text):
    """(months bitmask, partial) of a date range like 'NOV 15 - MAR 31' (inclusive, may wrap the year); months
    touched partly count whole (caution) and set partial. Unreadable: all year, partial."""
    m = re.fullmatch(r"\s*([A-Z]{3})\s*(\d*)\s*-\s*([A-Z]{3})\s*(\d*)\s*", (text or "").upper())
    if not m or m.group(1) not in MONTHS or m.group(3) not in MONTHS:
        return 0x0FFF, True
    m0, d0 = MONTHS[m.group(1)], int(m.group(2) or 1)
    m1, d1 = MONTHS[m.group(3)], int(m.group(4) or MONTH_DAYS[MONTHS[m.group(3)]])
    bits, i = 0, m0
    while True:
        bits |= 1 << i
        if i == m1:
            break
        i = (i + 1) % 12
    partial = d0 != 1 or d1 < MONTH_DAYS[m1] - (1 if m1 == 1 else 0)  # February's 28th ends it
    return bits, partial


def manatee_class(code):
    """(class, limit 0.1 km/h, flags, days, name) for a manatee-zone code, or None for no restriction."""
    c = (code or "").strip().upper()
    if c in ("", "UN"):
        return None
    if c.startswith("SSAY_25CH"):
        return 2, 0, FLAG_CHANNEL, None, "SLOW SPEED (25 MPH IN CHANNEL)"
    if c == "SS WKND":
        return 2, 0, 0, WEEKEND, "SLOW SPEED (WEEKENDS)"
    if c in ("IS", "ISAY"):
        return 1, 0, 0, None, "IDLE SPEED"
    if c in ("SS", "SSAY"):
        return 2, 0, 0, None, "SLOW SPEED"
    if c in ("NE", "NEAY"):
        return 4, 0, 0, None, "NO ENTRY"
    if c in ("MP", "MPAY"):
        return 4, 0, 0, None, "MOTORBOATS PROHIBITED"
    m = re.fullmatch(r"(\d+)\s*DY\s*/\s*(\d+)\s*N", c)
    if m:
        return 3, round(int(m.group(1)) * MPH_DKPH), FLAG_PARTIAL, None, f"{m.group(1)} MPH DAY / {m.group(2)} NIGHT"
    m = re.fullmatch(r"(\d+)\s*MPH", c)
    if m:
        return 3, round(int(m.group(1)) * MPH_DKPH), 0, None, f"{m.group(1)} MPH"
    return 5, 0, FLAG_PARTIAL, None, c  # unknown code: shown as a restriction, never dropped


def boating_class(restriction):
    """(class, limit, flags, name) for a boating-safety RESTRICTION, or None (not a speed or entry rule)."""
    r = (restriction or "").strip()
    flags = FLAG_PARTIAL if "conditional" in r.lower() else 0
    lr = r.lower()
    if lr.startswith("idle speed"):
        return 1, 0, flags, "IDLE SPEED NO WAKE"
    if lr.startswith("slow speed"):
        return 2, 0, flags, "SLOW SPEED MINIMUM WAKE"
    m = re.match(r"max speed (\d+)\s*mph", lr)
    if m:
        return 3, round(int(m.group(1)) * MPH_DKPH), flags, f"MAX {m.group(1)} MPH"
    if lr.startswith("no entry"):
        return 4, 0, flags, "NO ENTRY"
    return None


def polygons(geometry):
    """[[ (lon array, lat array, outer) ]] per polygon of a GeoJSON Polygon or MultiPolygon."""
    if not geometry:
        return []
    kind, coords = geometry.get("type"), geometry.get("coordinates") or []
    polys = [coords] if kind == "Polygon" else coords if kind == "MultiPolygon" else []
    out = []
    for poly in polys:
        rings = []
        for k, ring in enumerate(poly):
            a = np.asarray(ring, np.float64)
            if a.ndim != 2 or len(a) < 4:
                continue
            if (a[0] == a[-1]).all():
                a = a[:-1]
            rings.append((a[:, 0], a[:, 1], k == 0))
        if rings and rings[0][2]:
            out.append(rings)
    return out


def zone_features(boating, manatee, log):
    """[(class, attr record bytes, rings)]."""
    feats = []
    skipped = {}
    for f in boating.get("features", []):
        p = f.get("properties") or {}
        c = boating_class(p.get("RESTRICTION"))
        if c is None:
            skipped[p.get("RESTRICTION")] = skipped.get(p.get("RESTRICTION"), 0) + 1
            continue
        cls, limit, flags, what = c
        area = (p.get("AREA_NAME") or "").strip()
        items = [(pmt.TAG_NAME, f"{what} · {area}" if area else what)]
        if limit:
            items.append((pmt.TAG_SPEED_DKMH, limit))
        if flags:
            items.append((pmt.TAG_FLAGS, flags))
        items.append((pmt.TAG_SOURCE_REF, f"FAC {p.get('RULE_NUM') or '68D-24'}"))
        for rings in polygons(f.get("geometry")):
            feats.append((cls, pmt.encode_attr(items), rings))
    for f in manatee.get("features", []):
        p = f.get("properties") or {}
        where = (p.get("COUNTY") or "").strip()
        for code_key, date_key in (("CLASS1_68C", "DATE1_68C"), ("CLASS2_68C", "DATE2_68C")):
            c = manatee_class(p.get(code_key))
            if c is None:
                continue
            cls, limit, flags, days, what = c
            months, partial = season(p.get(date_key))
            flags |= FLAG_PARTIAL if partial else 0
            items = [(pmt.TAG_NAME, f"MANATEE ZONE · {what}" + (f" · {where} COUNTY" if where else ""))]
            if limit:
                items.append((pmt.TAG_SPEED_DKMH, limit))
            if months != 0x0FFF:
                items.append((pmt.TAG_MONTHS, months))
            if days is not None:
                items.append((pmt.TAG_DAYS, days))
            if flags:
                items.append((pmt.TAG_FLAGS, flags))
            items.append((pmt.TAG_SOURCE_REF, f"FAC {p.get('SECT_68C') or '68C-22'}"))
            for rings in polygons(f.get("geometry")):
                feats.append((cls, pmt.encode_attr(items), rings))
    if skipped:
        log("left out (not speed or entry rules): " + "; ".join(f"{k} x{v}" for k, v in skipped.items()))
    return feats


def build(feats, log):
    """PMT levels: the zones cut per level, one attribute record each (identical records shared), and the coverage
    grid of the zoom-8 cells holding any zone."""
    attrs, attr_index = [], {}
    for _, rec, _ in feats:
        if rec not in attr_index:
            attr_index[rec] = len(attrs)
            attrs.append(rec)
    cells = set()
    for _, _, rings in feats:
        lon, lat = rings[0][0], rings[0][1]
        x, y = pg.world_xy(lon, lat, 8)
        for cx in range(int(np.floor(x.min())), int(np.floor(x.max())) + 1):
            for cy in range(int(np.floor(y.min())), int(np.floor(y.max())) + 1):
                cells.add((cx, cy))
    levels = []
    for tz, zmin, zmax, bits, buf, tol in LEVELS:
        wb = tz + bits
        tiles = {}
        for cls, rec, rings in feats:
            world = []
            for lon, lat, outer in rings:
                x, y = pg.world_xy(lon, lat, wb)
                x, y = pg.simplify_ring(x, y, tol * pg.units_per_metre(float(np.mean(lat)), wb))
                r = pg.clean_ring(x, y)
                if r is not None:
                    world.append(pg.orient(*r, outer=outer))
            if not world or not any(pg.area2(x, y) > 0 for x, y in world):
                continue
            for key, parts in pg.cut_polygon(world, tz, bits, buf).items():
                tiles.setdefault(key, []).append(
                    (pmt.POLYGON, cls, attr_index[rec], [list(zip(px.tolist(), py.tolist())) for px, py in parts]))
        levels.append({"tile_zoom": tz, "zoom_min": zmin, "zoom_max": zmax, "coord_bits": bits, "buffer": buf,
                       "tolerance_dm": int(tol * 10), "full_class": 1, "grid": {c: 1 for c in cells},
                       "tiles": tiles})
        log(f"level z{zmin}-{zmax}: {len(tiles)} tiles")
    return levels, attrs, cells


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out_dir")
    ap.add_argument("--boating", required=True)
    ap.add_argument("--manatee", required=True)
    ap.add_argument("--date", required=True, help="the data's date as YYYY-MM (shown in the legend)")
    ap.add_argument("--name", default="fwc-zones")
    a = ap.parse_args(argv[1:])
    t0 = time.time()
    log = lambda msg: print(f"[{time.time() - t0:5.0f} s] {msg}", flush=True)
    boating = json.loads(Path(a.boating).read_text(encoding="utf-8"))
    manatee = json.loads(Path(a.manatee).read_text(encoding="utf-8"))
    feats = zone_features(boating, manatee, log)
    log(f"{len(feats)} zone polygons")
    levels, attrs, cells = build(feats, log)
    strings = ["fwc-zones", "FWC", "Public record (Florida FWC); not for navigation", "FWC", a.date,
               f"pwca_maps build_zones.py"]
    data = pmt.build_pmt(layer_kind=pmt.KIND_ZONES, levels=levels, strings=strings,
                         ids=dict(zip(pmt.ID_FIELDS, range(6))), attrs=attrs, build_time=int(time.time()),
                         data_time=int(time.time()))
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pack = out / f"{a.name}.pmt"
    pack.write_bytes(data)
    sources = {
        "format": "PMT 1.0 (FORMAT.md)",
        "sources": [{"name": "Florida FWC State Boating Safety Zones (FAC 68D-24)", "file": Path(a.boating).name,
                     "sha256": hashlib.sha256(Path(a.boating).read_bytes()).hexdigest()},
                    {"name": "Florida FWC State Manatee Protection Zones (FAC 68C-22)", "file": Path(a.manatee).name,
                     "sha256": hashlib.sha256(Path(a.manatee).read_bytes()).hexdigest()}],
        "terms": "FWC: acknowledgment of FWC-FWRI as the source is appreciated; not a legal document for navigation.",
        "files": {pack.name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}},
    }
    (out / "SOURCES.json").write_text(json.dumps(sources, indent=2) + "\n", encoding="utf-8")
    log(f"wrote {pack}: {len(data) / 1e6:.2f} MB, {len(attrs)} attribute records, {len(cells)} zoom-8 cells covered")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
