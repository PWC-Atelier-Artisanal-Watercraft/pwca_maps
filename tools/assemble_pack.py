"""Puts several built layers into ONE pack directory for a card: the display reads one pack directory (the layer files
side by side: water, roads, later places) and the card installer copies one directory and checks it against one
MANIFEST.sha256.

Usage: python tools/assemble_pack.py OUT_DIR PACK_DIR [PACK_DIR ...]

Each PACK_DIR is a built pack (build_pack.py's output: its .pmt files, LICENSE.txt, ATTRIBUTION.txt, SOURCES.json and
MANIFEST.sha256). Before anything is copied every PACK_DIR is checked against its own manifest. The .pmt files are
copied (never changed: a file's sha256 in the new manifest is the one in its own pack's manifest), each copy is
checked, LICENSE.txt and ATTRIBUTION.txt must be the same in every pack, and SOURCES.json lists every source and, per
file, the pack it came from with its format, tool commit and build time. OUT_DIR must not exist or must be empty.
"""

import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

TOOL = Path(__file__).resolve()
TEXTS = ("LICENSE.txt", "ATTRIBUTION.txt")
MAX_FILE = (1 << 31) - 1  # FORMAT.md: a file is under 2 GiB


class PackError(Exception):
    pass


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def read_manifest(pack):
    """{file name: sha256} of a pack's MANIFEST.sha256, after checking every listed file against it."""
    manifest = pack / "MANIFEST.sha256"
    if not manifest.is_file():
        raise PackError(f"{manifest} missing: not a built pack")
    expected = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, name = line.split(None, 1)
            expected[name.strip()] = digest
    for name, digest in expected.items():
        path = pack / name
        if "/" in name or "\\" in name or not path.is_file():
            raise PackError(f"{pack}: {name} is listed in the manifest and is not a file of the pack")
        if sha256(path) != digest:
            raise PackError(f"{path}: its sha256 is not the manifest's: the pack is damaged or was changed")
    return expected


def git_commit():
    try:
        return subprocess.run(["git", "-C", str(TOOL.parent), "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def assemble(out, packs, log=print):
    out = Path(out)
    packs = [Path(p) for p in packs]
    if not packs:
        raise PackError("no pack directories given")
    if out.exists() and any(out.iterdir()):
        raise PackError(f"{out} exists and is not empty")
    checked = []  # (pack, manifest, SOURCES.json)
    names = {}
    for pack in packs:
        manifest = read_manifest(pack)
        info = json.loads((pack / "SOURCES.json").read_text(encoding="utf-8"))
        pmts = sorted(n for n in manifest if n.lower().endswith(".pmt"))
        if not pmts:
            raise PackError(f"{pack}: no .pmt file in its manifest")
        for n in pmts:
            if n.lower() in names:
                raise PackError(f"{n} is in both {names[n.lower()]} and {pack}: two layers need two file names")
            if n not in info.get("files", {}) or info["files"][n].get("sha256") != manifest[n]:
                raise PackError(f"{pack}: SOURCES.json does not describe {n} as the manifest does")
            if (pack / n).stat().st_size > MAX_FILE:
                raise PackError(f"{pack / n} is over 2 GiB")
            names[n.lower()] = pack
        for t in TEXTS:
            if t not in manifest:
                raise PackError(f"{pack}: {t} missing from its manifest")
            if checked and manifest[t] != checked[0][1][t]:
                raise PackError(f"{t} differs between {checked[0][0]} and {pack}: the layers' terms must be the same")
        checked.append((pack, manifest, info, pmts))
        log(f"{pack}: manifest checked, {len(pmts)} layer file(s)")

    out.mkdir(parents=True, exist_ok=True)
    sources, files = [], {}
    for pack, manifest, info, pmts in checked:
        for src in info.get("sources", []):
            if src not in sources:
                sources.append(src)
        for n in pmts:
            log(f"copying {n} ({(pack / n).stat().st_size / 1e6:.1f} MB)")
            shutil.copyfile(pack / n, out / n)
            if sha256(out / n) != manifest[n]:
                raise PackError(f"{out / n}: the copy's sha256 is not the manifest's")
            files[n] = dict(info["files"][n], format=info.get("format"), tool=info.get("tool"),
                            built_utc=info.get("built_utc"))
    for t in TEXTS:
        shutil.copyfile(checked[0][0] / t, out / t)
    doc = {
        "format": "PMT (FORMAT.md): a pack of layers, each file as its own entry says",
        "assembled_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "assembled_with": {"repository": "https://github.com/PWC-Atelier-Artisanal-Watercraft/pwca_maps",
                           "commit": git_commit(), "tool": "tools/assemble_pack.py"},
        "sources": sources,
        "files": files,
    }
    (out / "SOURCES.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    lines = [f"{sha256(q)}  {q.name}" for q in sorted(out.iterdir()) if q.is_file() and q.name != "MANIFEST.sha256"]
    (out / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    total = sum((out / n).stat().st_size for n in files)
    log(f"assembled {len(files)} layer file(s), {total / 1e6:.1f} MB, in {out}")
    return doc


def main(argv):
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        assemble(argv[1], argv[2:])
    except PackError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
