#!/usr/bin/env python3
"""Merge several PMTiles archives (MVT, same schema) into one.

Europe does not fit one Planetiler run on a 16 GB runner, so the workflow builds it in
blocks of countries and merges the block archives here.

- A tile that only one archive has is copied as is.
- A tile that several archives have with identical bytes (open sea, Natural Earth at
  low zooms) is copied once.
- Otherwise the tiles are merged at the protobuf level: layers with the same name are
  joined, keys and values are re-indexed, geometry bytes are copied unchanged, and a
  feature that several archives carry identically (a road or lake along a block border,
  present in both extracts) is kept once.

Usage: merge-archives.py OUTPUT INPUT [INPUT ...]
Needs the `pmtiles` package (pip install pmtiles). The writer keeps its tile data in a
temporary file until the end: set TMPDIR to a disk with room for the output twice.
"""

import gzip
import heapq
import json
import mmap
import sys
import time

from pmtiles.tile import (
    Compression,
    deserialize_directory,
    deserialize_header,
    tileid_to_zxy,
)
from pmtiles.writer import Writer


# --------------------------------------------------------------------------- protobuf


def read_varint(buf, pos):
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, pos
        shift += 7


def write_varint(out, value):
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)


def fields(buf):
    """Yield (field number, wire type, value); value is bytes for wire type 2."""
    pos = 0
    end = len(buf)
    while pos < end:
        key, pos = read_varint(buf, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = read_varint(buf, pos)
        elif wire == 2:
            length, pos = read_varint(buf, pos)
            value = buf[pos : pos + length]
            pos += length
        elif wire == 5:
            value = buf[pos : pos + 4]
            pos += 4
        elif wire == 1:
            value = buf[pos : pos + 8]
            pos += 8
        else:
            raise ValueError(f"unsupported wire type {wire}")
        yield number, wire, value


def put_bytes(out, number, data):
    write_varint(out, (number << 3) | 2)
    write_varint(out, len(data))
    out += data


def put_varint(out, number, value):
    write_varint(out, number << 3)
    write_varint(out, value)


def packed(buf):
    values = []
    pos = 0
    while pos < len(buf):
        v, pos = read_varint(buf, pos)
        values.append(v)
    return values


# --------------------------------------------------------------------------- MVT merge


class Layer:
    __slots__ = ("name", "extent", "version", "keys", "values", "key_index", "value_index", "features", "seen")

    def __init__(self, name, extent, version):
        self.name = name
        self.extent = extent
        self.version = version
        self.keys = []
        self.values = []
        self.key_index = {}
        self.value_index = {}
        self.features = []
        self.seen = set()

    def key_id(self, key):
        i = self.key_index.get(key)
        if i is None:
            i = self.key_index[key] = len(self.keys)
            self.keys.append(key)
        return i

    def value_id(self, value):
        i = self.value_index.get(value)
        if i is None:
            i = self.value_index[value] = len(self.values)
            self.values.append(value)
        return i

    def add(self, keys, values, feature_buf):
        fid = None
        tags = []
        gtype = 0
        geometry = b""
        for number, _, value in fields(feature_buf):
            if number == 1:
                fid = value
            elif number == 2:
                tags = packed(value)
            elif number == 3:
                gtype = value
            elif number == 4:
                geometry = bytes(value)
        pairs = tuple((keys[tags[i]], values[tags[i + 1]]) for i in range(0, len(tags) - 1, 2))
        identity = (gtype, geometry, pairs)
        if identity in self.seen:
            return
        self.seen.add(identity)
        self.features.append((fid, pairs, gtype, geometry))

    def encode(self):
        out = bytearray()
        put_bytes(out, 1, self.name)
        for fid, pairs, gtype, geometry in self.features:
            f = bytearray()
            if fid is not None:
                put_varint(f, 1, fid)
            tags = bytearray()
            for k, v in pairs:
                write_varint(tags, self.key_id(k))
                write_varint(tags, self.value_id(v))
            if tags:
                put_bytes(f, 2, tags)
            put_varint(f, 3, gtype)
            put_bytes(f, 4, geometry)
            put_bytes(out, 2, f)
        for k in self.keys:
            put_bytes(out, 3, k)
        for v in self.values:
            put_bytes(out, 4, v)
        put_varint(out, 5, self.extent)
        put_varint(out, 15, self.version)
        return out


def merge_mvt(tiles):
    """Merge raw (uncompressed) MVT tiles; layer order follows first appearance."""
    layers = {}
    for tile in tiles:
        for number, _, layer_buf in fields(tile):
            if number != 3:
                continue
            name = b""
            extent = 4096
            version = 2
            keys = []
            values = []
            features = []
            for n, _, v in fields(layer_buf):
                if n == 1:
                    name = bytes(v)
                elif n == 2:
                    features.append(v)
                elif n == 3:
                    keys.append(bytes(v))
                elif n == 4:
                    values.append(bytes(v))
                elif n == 5:
                    extent = v
                elif n == 15:
                    version = v
            layer = layers.get(name)
            if layer is None:
                layer = layers[name] = Layer(name, extent, version)
            elif layer.extent != extent:
                raise ValueError(f"layer {name!r} has extents {layer.extent} and {extent}")
            for feature in features:
                layer.add(keys, values, feature)
    out = bytearray()
    for layer in layers.values():
        put_bytes(out, 3, layer.encode())
    return bytes(out)


# --------------------------------------------------------------------------- archives


class Archive:
    def __init__(self, path):
        self.path = path
        self.file = open(path, "rb")
        self.map = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_READ)
        self.header = deserialize_header(self.map[0:127])
        meta = self.map[self.header["metadata_offset"] : self.header["metadata_offset"] + self.header["metadata_length"]]
        if self.header["internal_compression"] == Compression.GZIP:
            meta = gzip.decompress(meta)
        self.metadata = json.loads(meta)

    def entries(self, offset=None, length=None):
        """Yield (tile_id, data offset, data length) in tile id order, runs expanded."""
        h = self.header
        if offset is None:
            offset, length = h["root_offset"], h["root_length"]
        for e in deserialize_directory(self.map[offset : offset + length]):
            if e.run_length == 0:
                yield from self.entries(h["leaf_directory_offset"] + e.offset, e.length)
            else:
                for i in range(e.run_length):
                    yield e.tile_id + i, e.offset, e.length

    def data(self, offset, length):
        start = self.header["tile_data_offset"] + offset
        return self.map[start : start + length]


def tagged(archive_index, archive):
    for tile_id, offset, length in archive.entries():
        yield tile_id, archive_index, offset, length


def merge_metadata(archives):
    meta = dict(archives[0].metadata)
    layers = {}
    for a in archives:
        for layer in a.metadata.get("vector_layers", []):
            known = layers.get(layer["id"])
            if known is None:
                layers[layer["id"]] = dict(layer)
                continue
            known["minzoom"] = min(known.get("minzoom", 0), layer.get("minzoom", 0))
            known["maxzoom"] = max(known.get("maxzoom", 0), layer.get("maxzoom", 0))
            fields_ = dict(known.get("fields", {}))
            fields_.update(layer.get("fields", {}))
            known["fields"] = fields_
    if layers:
        meta["vector_layers"] = list(layers.values())
    meta["description"] = "Merged from " + ", ".join(a.metadata.get("name", a.path) for a in archives)
    return meta


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    output, inputs = sys.argv[1], sys.argv[2:]
    archives = [Archive(p) for p in inputs]
    first = archives[0].header
    for a in archives:
        h = a.header
        if h["tile_type"] != first["tile_type"] or h["tile_compression"] != first["tile_compression"]:
            sys.exit(f"{a.path}: tile type or compression differs from {archives[0].path}")
    gz = first["tile_compression"] == Compression.GZIP
    if not gz and first["tile_compression"] != Compression.NONE:
        sys.exit(f"tile compression {first['tile_compression']} not supported")

    def decode(b):
        return gzip.decompress(b) if gz else bytes(b)

    def encode(b):
        return gzip.compress(b, compresslevel=6, mtime=0) if gz else b

    stats = {"single": 0, "identical": 0, "merged": 0}
    started = time.time()
    merged_cache = {}

    with open(output, "wb") as f:
        writer = Writer(f)
        stream = heapq.merge(*(tagged(i, a) for i, a in enumerate(archives)))
        group = []
        current = None

        def flush():
            if len(group) == 1:
                _, i, off, ln = group[0]
                writer.write_tile(current, archives[i].data(off, ln))
                stats["single"] += 1
                return
            blobs = [archives[i].data(off, ln) for _, i, off, ln in group]
            if all(b == blobs[0] for b in blobs[1:]):
                writer.write_tile(current, blobs[0])
                stats["identical"] += 1
                return
            # Open sea and the like repeat the same pair of tiles thousands of times.
            key = tuple((i, off, ln) for _, i, off, ln in group)
            data = merged_cache.get(key)
            if data is None:
                data = encode(merge_mvt([decode(b) for b in blobs]))
                if len(data) < 16_384:
                    if len(merged_cache) > 50_000:
                        merged_cache.clear()
                    merged_cache[key] = data
            writer.write_tile(current, data)
            stats["merged"] += 1

        count = 0
        for item in stream:
            if item[0] != current:
                if group:
                    flush()
                group = []
                current = item[0]
            group.append(item)
            count += 1
            if count % 2_000_000 == 0:
                z = tileid_to_zxy(current)[0]
                print(f"{time.time() - started:7.0f}s  {count:,} entries, z{z}, {stats}", flush=True)
        if group:
            flush()

        header = dict(first)
        header["min_lon_e7"] = min(a.header["min_lon_e7"] for a in archives)
        header["min_lat_e7"] = min(a.header["min_lat_e7"] for a in archives)
        header["max_lon_e7"] = max(a.header["max_lon_e7"] for a in archives)
        header["max_lat_e7"] = max(a.header["max_lat_e7"] for a in archives)
        header["center_lon_e7"] = (header["min_lon_e7"] + header["max_lon_e7"]) // 2
        header["center_lat_e7"] = (header["min_lat_e7"] + header["max_lat_e7"]) // 2
        writer.finalize(header, merge_metadata(archives))

    print(f"Done in {time.time() - started:.0f}s: {stats}, {writer.addressed_tiles:,} tiles, "
          f"{len(writer.hash_to_offset):,} distinct contents", flush=True)


if __name__ == "__main__":
    main()
