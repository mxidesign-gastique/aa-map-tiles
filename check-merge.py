#!/usr/bin/env python3
"""Check that a merged archive holds every tile its block archives hold.

Usage: check-merge.py MERGED PART [PART ...] [--tiles z/x/y,z/x/y]
Prints per zoom the tiles of the parts (union) and of the merged archive, and every tile a part
has (non-empty) that the merged archive lacks or holds empty. Exit code 1 when one is missing.
"""

import importlib.util
import os
import sys
from collections import Counter

from pmtiles.tile import tileid_to_zxy, zxy_to_tileid

spec = importlib.util.spec_from_file_location("merge", os.path.join(os.path.dirname(os.path.abspath(__file__)), "merge-archives.py"))
merge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge)


def lengths(archive):
    out = {}
    for tile_id, _, length in archive.entries():
        out[tile_id] = length
    return out


def main():
    args = sys.argv[1:]
    probe = []
    if "--tiles" in args:
        i = args.index("--tiles")
        probe = [tuple(int(v) for v in t.split("/")) for t in args[i + 1].split(",") if t]
        del args[i : i + 2]
    merged_path, part_paths = args[0], args[1:]
    merged = lengths(merge.Archive(merged_path))
    union = set()
    parts = {}
    for path in part_paths:
        part = lengths(merge.Archive(path))
        parts[os.path.basename(path)] = part
        union.update(t for t, ln in part.items() if ln > 0)
    missing = sorted(t for t in union if merged.get(t, 0) == 0)
    by_zoom_union = Counter(tileid_to_zxy(t)[0] for t in union)
    by_zoom_merged = Counter(tileid_to_zxy(t)[0] for t, ln in merged.items() if ln > 0)
    by_zoom_missing = Counter(tileid_to_zxy(t)[0] for t in missing)
    print(f"{'z':>3} {'parts':>10} {'merged':>10} {'missing':>8}")
    for z in sorted(set(by_zoom_union) | set(by_zoom_merged)):
        print(f"{z:>3} {by_zoom_union[z]:>10,} {by_zoom_merged[z]:>10,} {by_zoom_missing[z]:>8,}")
    for t in missing[:40]:
        z, x, y = tileid_to_zxy(t)
        have = ", ".join(f"{name}:{part[t]}" for name, part in parts.items() if part.get(t, 0) > 0)
        print(f"missing {z}/{x}/{y} merged={merged.get(t)} parts: {have}")
    for z, x, y in probe:
        t = zxy_to_tileid(z, x, y)
        have = ", ".join(f"{name}:{part.get(t)}" for name, part in parts.items() if t in part)
        print(f"probe {z}/{x}/{y} merged={merged.get(t)} parts: {have or 'none'}")
    sys.exit(1 if missing else 0)


if __name__ == "__main__":
    main()
