"""Fetches the Florida FWC zone layers that tools/build_zones.py reads, as GeoJSON in WGS 84:
State Boating Safety Zones (FAC 68D-24) and State Manatee Protection Zones (FAC 68C-22), from gis.myfwc.com.

Usage: python tools/fetch_fwc_zones.py <out_dir>

The server cuts large responses short (a 100-feature page of the manatee layer came back as broken JSON), so each
layer is fetched in pages ordered by OBJECTID, a page that fails to parse is fetched again in smaller pages, and the
merged file is checked to hold every OBJECTID the layer reports. Writes boating.geojson, manatee.geojson and
SOURCES.fwc.json (URLs, retrieval time, feature counts, SHA-256).
"""

import datetime
import hashlib
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://gis.myfwc.com/hosting/rest/services/Open_Data"
LAYERS = {
    "boating": "State_Boating_Safety_Zones_Florida/MapServer/10",
    "manatee": "State_Manatee_Protection_Zones_in_Florida/MapServer/9",
}
PAGE = 100


def get_json(url):
    with urllib.request.urlopen(url, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def query_url(layer, **params):
    q = {"where": "1=1", "f": "json"}
    q.update(params)
    return f"{BASE}/{layer}/query?{urllib.parse.urlencode(q)}"


def fetch_page(layer, offset, count):
    """Features [offset, offset + count) by OBJECTID; halves the page while the server's answer doesn't parse."""
    url = query_url(layer, outFields="*", outSR=4326, returnGeometry="true", orderByFields="OBJECTID",
                    resultOffset=offset, resultRecordCount=count, f="geojson")
    try:
        return get_json(url)["features"]
    except (ValueError, KeyError):
        if count <= 1:
            raise
        half = count // 2
        return fetch_page(layer, offset, half) + fetch_page(layer, offset + half, count - half)


def fetch_layer(layer):
    count = get_json(query_url(layer, returnCountOnly="true"))["count"]
    ids = sorted(get_json(query_url(layer, returnIdsOnly="true"))["objectIds"])
    feats = []
    for offset in range(0, count, PAGE):
        feats += fetch_page(layer, offset, min(PAGE, count - offset))
    got = sorted(f["id"] for f in feats)
    if got != ids:
        raise SystemExit(f"{layer}: {len(got)} features fetched, the layer lists {len(ids)} (ids differ)")
    return feats


def main(argv):
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    out = Path(argv[1])
    out.mkdir(parents=True, exist_ok=True)
    record = {"retrieved_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "terms": "Florida FWC open data; for general reference only (the FAC prevails); not for navigation",
              "files": {}}
    for name, layer in LAYERS.items():
        feats = fetch_layer(layer)
        path = out / f"{name}.geojson"
        path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
        record["files"][path.name] = {
            "url": f"{BASE}/{layer}/query (outSR=4326, f=geojson, pages of {PAGE} by OBJECTID)",
            "features": len(feats),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        print(f"{path}: {len(feats)} features")
    (out / "SOURCES.fwc.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
