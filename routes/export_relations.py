#!/usr/bin/env python3
"""Non-public export of OSM hiking route relations as NDJSON (one line per relation, gzip).

    python3 routes/export_relations.py <in.osm.pbf> <out.ndjson.gz> --source-date YYYY-MM-DD

Input is the SAME filtered file the Planetiler profile reads ($WORK/routes.osm.pbf), so the
`ref` ("relation/<id>") of a row and the osm_id of a tile feature always belong together.
The line contract is backend/src/lib/route-import/record.ts of the main repository.

Selection (coarse on purpose, the fine quality rule lives in the backend):
  * relations with route=hiking or route=foot (exact value; "hiking;foot" lists are not exported)

Geometry (one LineString per relation):
  * only way members with role '' / forward / backward / main are used. Roles alternative,
    excursion, approach and every other role are not part of the main line
  * ways are joined greedily over shared end nodes (node id, not coordinates), reversing a way where
    needed; the result is a list of chains. Several chains are chained nearest-end-first (the
    first chain starts at the first member way, oriented towards the nearest other chain);
    the straight bridge between two chains is a gap. parts = number of chains,
    max_gap_m = longest bridge in metres (haversine, 0.1 m), 0 for a single chain
  * length_m = haversine sum over the whole line including bridges, rounded to metres
  * relations with relation members, or type=superroute, get superroute=true; they are exported
    only when they own way members that yield a line, otherwise skipped (counted)

Fields: name = name:de before name (like RoutesOverlay.java); ref_code = tag ref;
network = best of the ';' list iwn > nwn > rwn > lwn, else null; state = tag state;
disused = disused=yes or state=disused; osm_version = relation version;
tags = all tags of the relation.

content_hash = sha256 of the UTF-8 bytes of
    json.dumps({"geometry": {"type": "LineString", "coordinates": [[lon, lat], ...]}, "tags": {...}},
               sort_keys=True, separators=(",", ":"), ensure_ascii=False)
with lon/lat rounded to 7 decimals. It depends only on geometry and tags, so it is stable across
runs and unchanged by osm_version, source_date, parts and the other derived fields.

Output is sorted by relation id and the gzip header carries no timestamp, so equal input gives
byte-identical output. Two passes: relations first (ids of needed ways remembered), then the
geometry of exactly those ways via the node location index.
"""

import argparse
import gzip
import hashlib
import json
import math
import sys
from array import array
from collections import Counter

import osmium

ROUTE_VALUES = frozenset({"hiking", "foot"})
MAIN_ROLES = frozenset({"", "forward", "backward", "main"})
NETWORK_ORDER = ("iwn", "nwn", "rwn", "lwn")
EARTH_RADIUS_M = 6371008.8
COORD_DECIMALS = 7


def haversine_m(a, b):
    """Great-circle distance in metres between two (lon, lat) points."""
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def blank_to_none(value):
    if value is None:
        return None
    value = value.strip()
    return value or None


def best_network(raw):
    if raw is None:
        return None
    parts = {part.strip().lower() for part in raw.split(";")}
    for candidate in NETWORK_ORDER:
        if candidate in parts:
            return candidate
    return None


def content_hash(coordinates, tags):
    canonical = json.dumps(
        {"geometry": {"type": "LineString", "coordinates": coordinates}, "tags": tags},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class WayShape:
    """First and last node id and the coordinates of one way."""

    __slots__ = ("first", "last", "points")

    def __init__(self, first, last, points):
        self.first = first
        self.last = last
        self.points = points  # list of (lon, lat), or a flat array('d') lon, lat, lon, lat ... when stored

    def materialized(self):
        """The shape with a list of (lon, lat); the stored form is a flat array to keep memory low."""
        flat = self.points
        return WayShape(self.first, self.last, [(flat[i], flat[i + 1]) for i in range(0, len(flat), 2)])


def join_ways(shapes):
    """Joins way shapes (in member order) into chains over shared end nodes.

    Returns a list of chains; a chain is (first_node, last_node, points). Greedy: a chain grows at
    both ends with the lowest-index unused way touching the end node; a new chain starts at the
    lowest-index unused way.
    """
    by_node = {}
    for index, shape in enumerate(shapes):
        by_node.setdefault(shape.first, []).append(index)
        if shape.last != shape.first:
            by_node.setdefault(shape.last, []).append(index)
    used = [False] * len(shapes)

    def take(node):
        for index in by_node.get(node, ()):
            if not used[index]:
                used[index] = True
                return shapes[index]
        return None

    chains = []
    for start, shape in enumerate(shapes):
        if used[start]:
            continue
        used[start] = True
        first, last, points = shape.first, shape.last, list(shape.points)
        while True:
            nxt = take(last)
            if nxt is None:
                break
            if nxt.first == last:
                points.extend(nxt.points[1:])
                last = nxt.last
            else:  # nxt.last == last: the way runs against the chain, reverse it
                points.extend(reversed(nxt.points[:-1]))
                last = nxt.first
        while True:
            prev = take(first)
            if prev is None:
                break
            if prev.last == first:
                points = prev.points[:-1] + points
                first = prev.first
            else:  # prev.first == first: reverse it
                points = list(reversed(prev.points[1:])) + points
                first = prev.last
        chains.append((first, last, points))
    return chains


def order_chains(chains):
    """Chains one after another, nearest end first. Returns (points, parts, max_gap_m)."""
    if len(chains) == 1:
        return chains[0][2], 1, 0.0
    rest = chains[1:]
    first_points = chains[0][2]
    # Orient the first chain so that its end is the one closer to any other chain.
    others = [p for chain in rest for p in (chain[2][0], chain[2][-1])]
    end_gap = min(haversine_m(first_points[-1], p) for p in others)
    start_gap = min(haversine_m(first_points[0], p) for p in others)
    points = list(first_points) if end_gap <= start_gap else list(reversed(first_points))
    max_gap = 0.0
    while rest:
        tail = points[-1]
        best = None
        for index, chain in enumerate(rest):
            pts = chain[2]
            for reverse in (False, True):
                gap = haversine_m(tail, pts[-1] if reverse else pts[0])
                if best is None or gap < best[0]:
                    best = (gap, index, reverse)
        gap, index, reverse = best
        pts = rest.pop(index)[2]
        if reverse:
            pts = list(reversed(pts))
        max_gap = max(max_gap, gap)
        points.extend(pts)
    return points, len(chains), round(max_gap, 1)


def dedupe(points):
    """Drops consecutive equal vertices (after rounding)."""
    out = []
    for lon, lat in points:
        point = [round(lon, COORD_DECIMALS), round(lat, COORD_DECIMALS)]
        if not out or out[-1] != point:
            out.append(point)
    return out


def length_m(coordinates):
    return round(sum(haversine_m(a, b) for a, b in zip(coordinates, coordinates[1:])))


def build_row(relation, shapes, source_date):
    """One output row, or a skip reason string."""
    tags = relation["tags"]
    is_super = relation["has_relation_members"] or tags.get("type") == "superroute"
    way_shapes = [shapes[ref].materialized() for ref in relation["ways"] if ref in shapes]
    if not way_shapes:
        return "superroute_without_ways" if is_super else "no_way_geometry"
    points, parts, max_gap = order_chains(join_ways(way_shapes))
    coordinates = dedupe(points)
    if len(coordinates) < 2:
        return "degenerate_geometry"
    state = blank_to_none(tags.get("state"))
    return {
        "source": "osm",
        "ref": f"relation/{relation['id']}",
        "name": blank_to_none(tags.get("name:de")) or blank_to_none(tags.get("name")),
        "ref_code": blank_to_none(tags.get("ref")),
        "route": tags["route"],
        "network": best_network(tags.get("network")),
        "state": state,
        "disused": tags.get("disused") == "yes" or state == "disused",
        "superroute": is_super,
        "geometry": {"type": "LineString", "coordinates": coordinates},
        "parts": parts,
        "max_gap_m": max_gap,
        "length_m": length_m(coordinates),
        "osm_version": relation["version"],
        "source_date": source_date,
        "tags": tags,
        "content_hash": content_hash(coordinates, tags),
    }


def read_relations(path):
    """Pass 1: selected relations and the ids of the member ways they use."""
    relations = []
    needed = set()
    for obj in osmium.FileProcessor(path, osmium.osm.RELATION):
        tags = {tag.k: tag.v for tag in obj.tags}
        if tags.get("route") not in ROUTE_VALUES:
            continue
        ways = [m.ref for m in obj.members if m.type == "w" and m.role in MAIN_ROLES]
        needed.update(ways)
        relations.append({
            "id": obj.id,
            "version": obj.version,
            "tags": tags,
            "ways": ways,
            "has_relation_members": any(m.type == "r" for m in obj.members),
        })
    relations.sort(key=lambda r: r["id"])
    return relations, needed


def read_way_shapes(path, needed):
    """Pass 2: geometry of the needed ways. Ways with a missing node are left out and counted."""
    shapes = {}
    missing = 0
    processor = osmium.FileProcessor(path, osmium.osm.NODE | osmium.osm.WAY).with_locations()
    for obj in processor:
        if not obj.is_way() or obj.id not in needed:
            continue
        try:
            flat = array("d")
            for node in obj.nodes:
                flat.append(node.lon)
                flat.append(node.lat)
            first, last = obj.nodes[0].ref, obj.nodes[len(obj.nodes) - 1].ref
        except (osmium.InvalidLocationError, IndexError):
            missing += 1
            continue
        if len(flat) < 4:
            missing += 1
            continue
        shapes[obj.id] = WayShape(first, last, flat)
    return shapes, missing


def export(in_path, out_path, source_date):
    relations, needed = read_relations(in_path)
    shapes, ways_missing = read_way_shapes(in_path, needed)
    counts = Counter()
    with open(out_path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as gz:
        for relation in relations:
            row = build_row(relation, shapes, source_date)
            if isinstance(row, str):
                counts["skipped_" + row] += 1
                continue
            counts["written"] += 1
            if row["superroute"]:
                counts["written_superroute"] += 1
            if row["parts"] > 1:
                counts["written_with_gaps"] += 1
            gz.write((json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
    counts["relations_selected"] = len(relations)
    counts["ways_missing_nodes"] = ways_missing
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--source-date", required=True, help="date of the Geofabrik data, YYYY-MM-DD")
    args = parser.parse_args(argv)
    if len(args.source_date) != 10 or args.source_date[4] != "-" or args.source_date[7] != "-":
        parser.error("--source-date must look like YYYY-MM-DD")
    counts = export(args.input, args.output, args.source_date)
    for key in sorted(counts):
        print(f"{key}={counts[key]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
