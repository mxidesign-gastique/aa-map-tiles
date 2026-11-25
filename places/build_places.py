#!/usr/bin/env python3
"""Build the place list (`data/regions/dach-places.json.gz`) from Overture divisions.

The list holds the municipality-like places of the map area with name, centre, population
and parent, so that search and the where-dialog can name places that hold no entries yet
(`src/map/projection/dach-places-seed.ts` writes it, idempotently). Germany, Austria and
Switzerland (FULL_COUNTRIES) come complete; the other countries of the European basemap
(PLACE_COUNTRIES) only with localities and municipalities of at least MIN_POPULATION
inhabitants (or, where Overture has no population, its cities and towns) plus their counties
and regions, so the list stays small enough for one seed.
Boundaries are NOT part of it; `import_overture_divisions.py` adds them when content arrives.

  uv run --with duckdb python scripts/build_dach_places.py OUTPUT.json.gz

The file name keeps "dach" for history; the seed state and revision say which list it is.

OVERTURE_RELEASE (default: the newest release in the bucket; Overture keeps only the last
few) names the release. Overture ids (GERS) are meant to stay stable across releases, but a
place whose id changed would come twice, so a newer list adds countries, it does not replace
the stored DACH places (see `merge_places.py`). Data comes from the official Overture S3 bucket.
"""

from __future__ import annotations

import gzip
import json
import os
import sys

import duckdb

# Every country of the European basemap (aa-map-tiles blocks.json) with Overture divisions.
EUROPE = (
    "DE AT CH LI IT FR MC AD SM VA SI HR BA ME MT PL CZ SK HU LT LV EE NL BE LU DK GB IE GG JE IM FO "
    "NO SE FI IS ES PT GI GR RO RS BG AL XK MK MD UA BY CY TR GE"
)
COUNTRIES = tuple(os.environ.get("PLACE_COUNTRIES", EUROPE).split())
FULL_COUNTRIES = tuple(os.environ.get("FULL_COUNTRIES", "DE AT CH").split()) or ("--",)
MIN_POPULATION = int(os.environ.get("MIN_POPULATION", "2000"))
SUBTYPES = ("locality", "localadmin", "county", "region")
EXCLUDED_CLASSES = ("hamlet",)  # a hamlet is no municipality; its name is too often an ordinary word


def latest_release() -> str:
    """The newest release in the Overture bucket; Overture keeps only the last few."""
    import re
    import urllib.request

    url = "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/?list-type=2&prefix=release/&delimiter=/"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "all-active-places"}), timeout=30) as response:
        releases = re.findall(r"<Prefix>release/([^/<]+)/</Prefix>", response.read().decode())
    if not releases:
        raise SystemExit("no Overture release found")
    return sorted(releases)[-1]


def main(output: str) -> None:
    release = os.environ.get("OVERTURE_RELEASE") or latest_release()
    base = f"s3://overturemaps-us-west-2/release/{release}/theme=divisions"
    db = duckdb.connect()
    db.execute("INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2'")
    db.execute(
        """CREATE TABLE area AS
           SELECT a.id, a.division_id, a.subtype, a.country, a.names.primary AS name,
                  a.names.common['de'] AS name_de, v.class AS class, v.population AS population,
                  ST_PointOnSurface(a.geometry) AS point,
                  CASE WHEN a.subtype IN ('county', 'region') THEN a.geometry END AS shape
             FROM read_parquet(?, hive_partitioning=1) a
             LEFT JOIN read_parquet(?, hive_partitioning=1) v ON v.id = a.division_id
            WHERE list_contains(?, a.country) AND a.subtype IN ('locality','localadmin','county','region')
              AND a.names.primary IS NOT NULL AND a.names.primary <> ''
              AND coalesce(v.class, '') <> 'hamlet'
              AND (list_contains(?, a.country) OR a.subtype IN ('county', 'region')
                   OR coalesce(v.population, 0) >= ?
                   OR (v.population IS NULL AND v.class IN ('megacity', 'city', 'town')))""",
        [f"{base}/type=division_area/*", f"{base}/type=division/*", list(COUNTRIES), list(FULL_COUNTRIES), MIN_POPULATION],
    )
    # one place per division (a divided area, e.g. islands, appears more than once)
    db.execute(
        """CREATE TABLE place AS
           SELECT * FROM (SELECT *, row_number() OVER (PARTITION BY coalesce(division_id, id) ORDER BY id) AS n FROM area)
            WHERE n = 1"""
    )
    rows = db.execute(
        """SELECT p.id, p.subtype, p.country, p.name, nullif(p.name_de, p.name), p.population::bigint,
                  round(ST_Y(p.point), 5), round(ST_X(p.point), 5),
                  (SELECT c.id FROM place c
                    WHERE c.shape IS NOT NULL AND c.id <> p.id AND c.country = p.country
                      AND c.subtype = CASE p.subtype WHEN 'region' THEN 'none' WHEN 'county' THEN 'region' ELSE 'county' END
                      AND ST_Covers(c.shape, p.point)
                    ORDER BY ST_Area(c.shape) LIMIT 1)
             FROM place p ORDER BY p.id"""
    ).fetchall()
    places = [
        {"id": r[0], "class": r[1], "country": r[2], "name": r[3], "nameDe": r[4], "population": r[5],
         "lat": r[6], "lon": r[7], "parent": r[8]}
        for r in rows
    ]
    with gzip.open(output, "wt", encoding="utf-8", compresslevel=9) as handle:
        json.dump({"release": release, "places": places}, handle, ensure_ascii=False, separators=(",", ":"))
    counts: dict[str, int] = {}
    for place in places:
        key = f"{place['country']}/{place['class']}"
        counts[key] = counts.get(key, 0) + 1
    print(json.dumps({"release": release, "countries": COUNTRIES, "full": FULL_COUNTRIES,
                      "minPopulation": MIN_POPULATION, "places": len(places), "byClass": counts,
                      "withoutParent": sum(1 for p in places if p["parent"] is None)}))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: build_dach_places.py OUTPUT.json.gz")
    main(sys.argv[1])
