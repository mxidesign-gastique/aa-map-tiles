#!/usr/bin/env python3
"""Fetch the official hazard zones and write them in our schema as GeoJSON sequences.

  python3 official/build_official.py OUT_DIR [--bbox min_lon,min_lat,max_lon,max_lat] [--sources wlv-gzp,provinz-bozen,lfu-bayern]

Writes OUT_DIR/hazard_zone.geojsonl, OUT_DIR/hazard_coverage.geojsonl, OUT_DIR/route.geojsonl and OUT_DIR/sources.json
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
from email.utils import parsedate_to_datetime
from collections import Counter, defaultdict
from typing import Any, Iterator

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from hazard_zones import map_bz, map_lfu, map_wlv  # noqa: E402
from routes_bvv import map_bvv, unknown_reason  # noqa: E402
import protected_areas  # noqa: E402

AGENT = "all-active-tiles/1.0 (+https://all-active.net)"
WLV_BASE = "https://gis.lfrz.gv.at/api/geodata/i000901/ogc/features/v1"
WLV_COLLECTIONS = ["wlv_gzp_sz_la", "wlv_gzp_sz_wb", "wlv_gzp_bvb", "wlv_gzp_rrb", "wlv_gzp_bhb", "wlv_gzp_vhb"]
BZ_BASE = "https://geoservices1.civis.bz.it/geoserver/p_bz-TerritorialPlans/ows"
BZ_LAYERS = ["Avalanches", "Hydrology", "Landslides"]
BVV_ZIP = "https://geodaten.bayern.de/odd/m/2/basisdlm/plus/by_basisdlm_plus.zip"
BVV_LAYER = "L_53901_Freizeitwegachse_BY"
LFU_ZIP = "https://www.lfu.bayern.de/gdi/dls/daten/georisiken/gefahrenhinweiskarten_epsg4258_shp.zip"

SOURCES = {
    "wlv-gzp": {
        "name": "Gefahrenzonenpläne der Wildbach- und Lawinenverbauung (BML)",
        "licence": "INSPIRE, keine Nutzungsbedingungen (noConditionsApply)",
        "attribution": "Gefahrenzonenpläne: die.wildbach / BML, in eigene Darstellung umgewandelt",
    },
    "provinz-bozen": {
        "name": "Gefahrenzonenpläne der Gemeinden, Autonome Provinz Bozen",
        "licence": "CC0 1.0",
        "attribution": "Gefahrenzonenpläne: Autonome Provinz Bozen - Südtirol, in eigene Darstellung umgewandelt",
    },
    "bvv-freizeitwege": {
        "name": "Freizeitwege Bayern (ATKIS Basis-DLM plus)",
        "licence": "CC BY 4.0",
        "attribution": "Bayerische Vermessungsverwaltung – www.geodaten.bayern.de; in eigene Darstellung umgewandelt",
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


def download(url: str, target: pathlib.Path) -> pathlib.Path:
    if not target.exists():
        request = urllib.request.Request(url, headers={"User-Agent": AGENT})
        with urllib.request.urlopen(request, timeout=1800) as response, target.open("wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
            data_date = date_of(response.headers.get("Last-Modified"))
        if data_date:
            target.with_name(target.name + ".date").write_text(data_date)
    return target


def date_of(last_modified: str | None) -> str:
    """An HTTP date (`Last-Modified`) as YYYY-MM-DD, '' when absent or unreadable."""
    try:
        return parsedate_to_datetime(last_modified).date().isoformat() if last_modified else ""
    except (TypeError, ValueError):
        return ""


def download_date(target: pathlib.Path) -> str:
    """The data date `download` noted for a file: its Last-Modified, else the day of the build."""
    noted = target.with_name(target.name + ".date")
    return noted.read_text().strip() if noted.exists() else date.today().isoformat()


def bvv_archive(work: pathlib.Path) -> pathlib.Path:
    return download(BVV_ZIP, work / "by_basisdlm_plus.zip")


def bvv_features(work: pathlib.Path, bbox: str | None) -> Iterator[dict]:
    """The leisure trail axes of the Basis-DLM plus (1.75 GiB zip, one GeoPackage inside)."""
    archive = bvv_archive(work)
    target = work / "bvv_trails.geojsonl"
    command = ["ogr2ogr", "-f", "GeoJSONSeq", str(target), f"/vsizip/{archive}/by_basisdlm_plus.gpkg", BVV_LAYER,
               "-t_srs", "EPSG:4326", "-lco", "COORDINATE_PRECISION=6", "-explodecollections"]
    if bbox:
        command += ["-spat", *bbox.split(","), "-spat_srs", "EPSG:4326"]
    subprocess.run(command, check=True)
    with target.open() as lines:
        for line in lines:
            yield json.loads(line.lstrip("\x1e"))
    target.unlink()


def _in_bbox(geometry: dict | None, bbox: str | None) -> bool:
    """True when the geometry's extent meets the bbox (or there is no bbox)."""
    if not bbox:
        return True
    west, south, east, north = (float(value) for value in bbox.split(","))
    coords = list(points(geometry))
    if not coords:
        return False
    xs, ys = [x for x, _ in coords], [y for _, y in coords]
    return min(xs) <= east and max(xs) >= west and min(ys) <= north and max(ys) >= south


def protected_features(source: str, work: pathlib.Path, bbox: str | None, found: dict[str, list[dict]]) -> Iterator[tuple[dict, dict]]:
    """The records of one protected-area source, read as `protected_areas.FETCH` names them: a list of
    {"format", "input", "layer", "args"}. "ogr" goes through ogr2ogr ("input" an OGR data source such
    as "WFS:https://…", "layer" the type name); "geoadmin_find" is one geo.admin find call whose
    `results` are the features (fetched once per URL into `found`, BAFU parks feed two sources).
    "args" are extra keyword arguments for the map function (type_name, type_key)."""
    for index, entry in enumerate(protected_areas.FETCH.get(source, [])):
        if entry.get("format") == "geoadmin_find":
            if entry["input"] not in found:
                found[entry["input"]] = get_json(entry["input"]).get("results", [])
            for feature in found[entry["input"]]:
                if _in_bbox(feature.get("geometry"), bbox):
                    yield feature, entry.get("args", {})
            continue
        target = work / f"protected-{source}-{index}.geojsonl"
        command = ["ogr2ogr", "-f", "GeoJSONSeq", str(target), entry["input"], *([entry["layer"]] if entry.get("layer") else []),
                   "-t_srs", "EPSG:4326", "-lco", "COORDINATE_PRECISION=6", "-lco", "ID_FIELD=gml_id"]
        if bbox:
            command += ["-spat", *bbox.split(","), "-spat_srs", "EPSG:4326"]
        subprocess.run(command, check=True)
        with target.open() as lines:
            for line in lines:
                yield json.loads(line.lstrip("\x1e")), entry.get("args", {})
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


def build_meta(wanted: set[str], counts: dict[str, int], updated: dict[str, str]) -> dict[str, dict]:
    """The sources.json content. `updated` is the newest date the authority gave, else None: never the
    day of the build (the Bolzano service and several protected-area sources carry no date)."""
    meta = {key: {**SOURCES[key], "count": counts[key], "updated": updated.get(key)} for key in sorted(wanted)}
    for key in sorted(k for k in counts if k.startswith("protected:")):
        meta[key] = {"count": counts[key], "updated": updated.get(key)}
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("--bbox")
    parser.add_argument("--sources", default="wlv-gzp,provinz-bozen,lfu-bayern,bvv-freizeitwege")
    parser.add_argument("--protected", default=",".join(protected_areas.SOURCES),
                        help="protected-area sources of official/protected_areas.py ('' = none)")
    parser.add_argument("--work", help="where big downloads go (default: OUT_DIR/work); outside the repo")
    args = parser.parse_args()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    wanted = {name for name in args.sources.split(",") if name}
    counts: dict[str, int] = defaultdict(int)
    updated: dict[str, str] = {}
    by_municipality: dict[tuple[str, str], set] = defaultdict(set)

    work_dir = pathlib.Path(args.work) if args.work else out / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    unknown_trails: Counter[str] = Counter()

    with (out / "hazard_zone.geojsonl").open("w") as zones, (out / "hazard_coverage.geojsonl").open("w") as coverage, \
            (out / "route.geojsonl").open("w") as routes, (out / "protected_area.geojsonl").open("w") as protected, \
            (out / "zone_label.geojsonl").open("w") as labels:
        def write(zone: dict | None) -> None:
            if not zone or not zone.get("geometry"):
                return
            props = zone["properties"]
            zones.write(json.dumps(zone, ensure_ascii=False, separators=(",", ":")) + "\n")
            counts[props["source"]] += 1
            if props.get("source_date") and props["source_date"] > updated.get(props["source"], ""):
                updated[props["source"]] = props["source_date"]
            if props["source"] in ("wlv-gzp", "provinz-bozen") and props.get("municipality"):
                by_municipality[(props["source"], props["municipality"])].update(points(zone["geometry"]))

        if "wlv-gzp" in wanted:
            for collection, feature in wlv_features(args.bbox):
                write(map_wlv(feature, collection))
        if "provinz-bozen" in wanted:
            for feature in bz_features(args.bbox):
                write(map_bz(feature))
        if "lfu-bayern" in wanted:
            with tempfile.TemporaryDirectory(dir=out) as work:
                for index, (layer, record) in enumerate(lfu_layers(pathlib.Path(work), args.bbox)):
                    if layer.startswith("ghk_uebersicht"):
                        coverage.write(json.dumps({"type": "Feature", "geometry": record["geometry"], "properties": {"source": "lfu-bayern", "zone_type": "hint"}}) + "\n")
                    else:
                        write(map_lfu(record, layer, index))

        if "bvv-freizeitwege" in wanted:
            bvv_date = download_date(bvv_archive(work_dir))
            updated["bvv-freizeitwege"] = bvv_date
            for feature in bvv_features(work_dir, args.bbox):
                route = map_bvv(feature, bvv_date)
                if route is None:
                    reason = unknown_reason(feature)
                    unknown_trails[reason or "(skipped: no trail type)"] += 1
                    continue
                routes.write(json.dumps(route, ensure_ascii=False, separators=(",", ":")) + "\n")
                counts["bvv-freizeitwege"] += 1

        found: dict[str, list[dict]] = {}
        for source in [name for name in args.protected.split(",") if name]:
            mapper = protected_areas.SOURCES[source]
            for record, extra in protected_features(source, work_dir, args.bbox, found):
                zone = mapper(record, **extra)  # source_date only from the record itself, never the build day
                if not zone:
                    continue
                props = zone["properties"]
                protected.write(json.dumps(zone, ensure_ascii=False, separators=(",", ":")) + "\n")
                counts[f"protected:{source}"] += 1
                if props.get("source_date") and props["source_date"] > updated.get(f"protected:{source}", ""):
                    updated[f"protected:{source}"] = props["source_date"]
                point = protected_areas.label_point(zone)
                if point and props.get("name"):
                    label = {"id": props["id"], "name": props["name"], "zone_class": props.get("zone_class", "")}
                    labels.write(json.dumps({"type": "Feature", "geometry": {"type": "Point", "coordinates": point}, "properties": label}, ensure_ascii=False) + "\n")

        for (source, municipality), coords in by_municipality.items():
            ring = hull(coords)
            if ring:
                props = {"source": source, "zone_type": "plan", "municipality": municipality, "approximate": True}
                coverage.write(json.dumps({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}, "properties": props}, ensure_ascii=False) + "\n")

    meta = build_meta(wanted, counts, updated)
    (out / "sources.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    skipped_trails = unknown_trails.pop("(skipped: no trail type)", 0)
    if skipped_trails:
        print(f"{skipped_trails} BVV axes without a trail type left out", file=sys.stderr)
    unknown_total = sum(unknown_trails.values())
    if counts.get("bvv-freizeitwege") and unknown_total > counts["bvv-freizeitwege"] / 100:
        listed = ", ".join(f"{value!r}: {count}" for value, count in unknown_trails.most_common(20))
        raise SystemExit(f"{unknown_total} BVV trails with an unknown FW_TYP ({listed}): check the value list in routes_bvv.py")
    if not any(counts.values()):
        raise SystemExit("no hazard zones written")


if __name__ == "__main__":
    main()
