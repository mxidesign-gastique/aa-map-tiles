#!/usr/bin/env python3
"""Print what a few tiles of one or more PMTiles archives hold, to check block seams.

Usage: inspect-tiles.py "lon lat;lon lat;..." ZOOMS ARCHIVE [ARCHIVE ...]
For every point and zoom it prints, per archive, the tile size and per layer the number of
features and of features whose geometry and tags repeat (should be 0 after a merge).
"""

import gzip
import importlib.util
import math
import os
import sys
from collections import Counter

from pmtiles.reader import MmapSource, Reader

spec = importlib.util.spec_from_file_location("merge", os.path.join(os.path.dirname(os.path.abspath(__file__)), "merge-archives.py"))
merge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge)


def layers(tile):
    out = {}
    for number, _, layer_buf in merge.fields(tile):
        if number != 3:
            continue
        name, feats, keys, values = b"", [], [], []
        for n, _, v in merge.fields(layer_buf):
            if n == 1:
                name = bytes(v)
            elif n == 2:
                feats.append(v)
            elif n == 3:
                keys.append(bytes(v))
            elif n == 4:
                values.append(bytes(v))
        ids = Counter()
        for f in feats:
            tags, gtype, geom = [], 0, b""
            for n, _, v in merge.fields(f):
                if n == 2:
                    tags = merge.packed(v)
                elif n == 3:
                    gtype = v
                elif n == 4:
                    geom = bytes(v)
            pairs = tuple(sorted((keys[tags[i]], values[tags[i + 1]]) for i in range(0, len(tags) - 1, 2)))
            ids[(gtype, geom, pairs)] += 1
        out[name.decode()] = (len(feats), sum(c - 1 for c in ids.values() if c > 1))
    return out


def main():
    points = [tuple(map(float, p.split())) for p in sys.argv[1].split(";") if p.strip()]
    zooms = [int(z) for z in sys.argv[2].split(",")]
    readers = [(os.path.basename(p), Reader(MmapSource(open(p, "rb")))) for p in sys.argv[3:]]
    for lon, lat in points:
        for z in zooms:
            n = 2**z
            x = int((lon + 180) / 360 * n)
            y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
            print(f"== {lon},{lat} z{z} {z}/{x}/{y}")
            for name, r in readers:
                data = r.get(z, x, y)
                if data is None:
                    print(f"  {name:28s} -")
                    continue
                raw = gzip.decompress(data)
                summary = " ".join(f"{k}:{c}" + (f"(dup {d})" if d else "") for k, (c, d) in sorted(layers(raw).items()))
                print(f"  {name:28s} {len(data):>7} B  {summary}")


if __name__ == "__main__":
    main()
