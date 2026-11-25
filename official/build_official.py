#!/usr/bin/env python3
"""Fetch the official hazard zones and write them in our schema as GeoJSON sequences.

  python3 official/build_official.py OUT_DIR [--bbox min_lon,min_lat,max_lon,max_lat] [--sources wlv,bz,lfu]

Writes OUT_DIR/hazard_zone.geojsonl, OUT_DIR/hazard_coverage.geojsonl and OUT_DIR/sources.json
(per source: count, updated, licence, attribution). tippecanoe turns the two sequences into the
layers of the tile set `official` (.github/workflows/official.yml).

Coverage: LfU ships its own overview polygon (ghk_uebersicht). WLV and Bolzano do not; there the
coverage is the convex hull of the zones of each municipality, marked `approximate`, so the app can
say "no official zones here" outside it and never claims a gap is safe.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import date
from collections import defaultdict
from typing import Any, Iterator

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from hazard_zones import map_bz, map_lfu, map_wlv  # noqa: E402

AGENT = "all-active-tiles/1.0 (+https://all-active.net)"
WLV_BASE = "https://gis.lfrz.gv.at/api/geodata/i000901/ogc/features/v1"
WLV_COLLECTIONS = ["wlv_gzp_sz_la", "wlv_gzp_sz_wb", "wlv_gzp_bvb", "wlv_gzp_rrb", "wlv_gzp_bhb", "wlv_gzp_vhb"]
BZ_BASE = "https://geoservices1.civis.bz.it/geoserver/p_bz-TerritorialPlans/ows"
BZ_LAYERS = ["Avalanches", "Hydrology", "Landslides"]
LFU_ZIP = "https://www.lfu.bayern.de/gdi/dls/daten/georisiken/gefahrenhinweiskarten_epsg4258_shp.zip"

SOURCES = {
    "wlv": {
        "name": "Gefahrenzonenpläne der Wildbach- und Lawinenverbauung (BML)",
        "licence": "INSPIRE, keine Nutzungsbedingungen (noConditionsApply)",
        "attribution": "Gefahrenzonenpläne: die.wildbach / BML, in eigene Darstellung umgewandelt",
    },
    "bozen": {
        "name": "Gefahrenzonenpläne der Gemeinden, Autonome Provinz Bozen",
        "licence": "CC0 1.0",
        "attribution": "Gefahrenzonenpläne: Autonome Provinz Bozen - Südtirol, in eigene Darstellung umgewandelt",
    },
    "lfu-bayern": {
        "name": "Gefahrenhinweiskarte Bayern (Georisiken)",
        "licence": "CC BY 4.0",
        "attribution": "Datenquelle: Bayerisches Landesamt für Umwelt, www.lfu.bayern.de; in eigene Darstellung umgewandelt",
    },
}


def get_json(url: str, attempts: int = 5) -> Any:
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": "application/geo+json, application/json"})
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response)
        except Exception as error:  # noqa: BLE001 - retried, then raised
            if attempt == attempts - 1:
                raise
            print(f"retry {attempt + 1} for {url}: {error}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))


def wlv_features(bbox: str | None) -> Iterator[tuple[str, dict]]:
    for collection in WLV_COLLECTIONS:
        params = {"f": "json", "limit": "1000"}
        if bbox:
            params["bbox"] = bbox
        url = f"{WLV_BASE}/collections/i000901:{collection}/items?{urllib.parse.urlencode(params)}"
        while url:
            page = get_json(url)
            for feature in page.get("features", []):
                yield collection, feature
            url = next((link["href"] for link in page.get("links", []) if link.get("rel") == "next"), None)
            time.sleep(0.2)  # polite to a public service


def bz_features(bbox: str | None) -> Iterator[dict]:
    for layer in BZ_LAYERS:
        start = 0
        while True:
            params = {
                "service": "WFS", "version": "2.0.0", "request": "GetFeature",
                "typeNames": f"p_bz-TerritorialPlans:UrbanPlan-HazardZonePlan-{layer}",
                "outputFormat": "application/json", "srsName": "urn:ogc:def:crs:OGC:1.3:CRS84",
                "count": "5000", "startIndex": str(start), "sortBy": "OBJECTID",
            }
            if bbox:
                params["bbox"] = f"{bbox},urn:ogc:def:crs:OGC:1.3:CRS84"
            page = get_json(f"{BZ_BASE}?{urllib.parse.urlencode(params)}")
            features = page.get("features", [])
            yield from features
            if len(features) < 5000:
                break
            start += len(features)


def lfu_layers(work: pathlib.Path, bbox: str | None) -> Iterator[tuple[str, dict]]:
    archive = work / "lfu.zip"
    if not archive.exists():
        request = urllib.request.Request(LFU_ZIP, headers={"User-Agent": AGENT})
        with urllib.request.urlopen(request, timeout=600) as response, archive.open("wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
    with zipfile.ZipFile(archive) as bundle:
        layers = sorted(pathlib.PurePosixPath(name).stem for name in bundle.namelist() if name.lower().endswith(".shp"))
    for layer in layers:
        target = work / f"{layer}.geojsonl"
        command = ["ogr2ogr", "-f", "GeoJSONSeq", str(target), f"/vsizip/{archive}", layer, "-t_srs", "EPSG:4326", "-lco", "COORDINATE_PRECISION=6"]
        if bbox:
            command += ["-spat", *bbox.split(","), "-spat_srs", "EPSG:4326"]
        subprocess.run(command, check=True)
        with target.open() as lines:
            for line in lines:
                yield layer, json.loads(line.lstrip("\x1e"))
        target.unlink()


def points(geometry: dict | None) -> Iterator[tuple[float, float]]:
    if not geometry:
        return
    rings = geometry["coordinates"] if geometry["type"] == "Polygon" else [r for p in geometry["coordinates"] for r in p] if geometry["type"] == "MultiPolygon" else []
    for ring in rings[:1] if geometry["type"] == "Polygon" else rings:
        for x, y, *_ in ring:
            yield round(x, 4), round(y, 4)


def hull(coords: set[tuple[float, float]]) -> list[list[float]] | None:
    """Convex hull (monotone chain) as a closed ring, or None for fewer than three points."""
    pts = sorted(coords)
    if len(pts) < 3:
        return None

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    ring = lower[:-1] + upper[:-1]
    return [list(p) for p in ring + [ring[0]]] if len(ring) >= 3 else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("--bbox")
    parser.add_argument("--sources", default="wlv,bozen,lfu-bayern")
    args = parser.parse_args()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    wanted = set(args.sources.split(","))
    counts: dict[str, int] = defaultdict(int)
    updated: dict[str, str] = {}
    by_municipality: dict[tuple[str, str], set] = defaultdict(set)

    with (out / "hazard_zone.geojsonl").open("w") as zones, (out / "hazard_coverage.geojsonl").open("w") as coverage:
        def write(zone: dict | None) -> None:
            if not zone or not zone.get("geometry"):
                return
            props = zone["properties"]
            zones.write(json.dumps(zone, ensure_ascii=False, separators=(",", ":")) + "\n")
            counts[props["source"]] += 1
            if props.get("source_date") and props["source_date"] > updated.get(props["source"], ""):
                updated[props["source"]] = props["source_date"]
            if props["source"] in ("wlv", "bozen") and props.get("municipality"):
                by_municipality[(props["source"], props["municipality"])].update(points(zone["geometry"]))

        if "wlv" in wanted:
            for collection, feature in wlv_features(args.bbox):
                write(map_wlv(feature, collection))
        if "bozen" in wanted:
            for feature in bz_features(args.bbox):
                write(map_bz(feature))
        if "lfu-bayern" in wanted:
            with tempfile.TemporaryDirectory(dir=out) as work:
                for index, (layer, record) in enumerate(lfu_layers(pathlib.Path(work), args.bbox)):
                    if layer.startswith("ghk_uebersicht"):
                        coverage.write(json.dumps({"type": "Feature", "geometry": record["geometry"], "properties": {"source": "lfu-bayern", "zone_type": "hint"}}) + "\n")
                    else:
                        write(map_lfu(record, layer, index))

        for (source, municipality), coords in by_municipality.items():
            ring = hull(coords)
            if ring:
                props = {"source": source, "zone_type": "plan", "municipality": municipality, "approximate": True}
                coverage.write(json.dumps({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}, "properties": props}, ensure_ascii=False) + "\n")

    if counts.get("bozen"):
        updated["bozen"] = date.today().isoformat()  # the service is updated daily and carries no date per zone
    meta = {key: {**SOURCES[key], "count": counts[key], "updated": updated.get(key)} for key in sorted(wanted)}
    (out / "sources.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    if not any(counts.values()):
        raise SystemExit("no hazard zones written")


if __name__ == "__main__":
    main()
