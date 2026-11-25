# aa-map-tiles

Builds the basemap archive (PMTiles, OpenMapTiles schema) for map.all-active.net with
[Planetiler](https://github.com/onthegomap/planetiler) from Geofabrik extracts of all of Europe
and uploads it to Bunny storage under a new key.

Europe does not fit one 16 GB runner, so `blocks.json` splits it into blocks of countries
(at most ~5 GB PBF each). Each block builds in its own job; `merge-archives.py` then joins
the block archives into one (border tiles are merged feature by feature).

Run: Actions -> Map Tiles -> Run workflow. With `upload` off the merged archive stays a
workflow artifact for 7 days; Actions -> Publish Archive copies it to the zone later.

Map data © OpenStreetMap contributors (ODbL).

## routes (Wegenetze)

A separate overlay tile set (PMTiles, MVT, extent 4096, zoom 6-14) with the hiking, cycling,
mountain-bike, horse and winter-sports networks of OpenStreetMap. It is independent of the
basemap: own profile (`routes/`, Java, Planetiler 0.10.2), own workflow
(`.github/workflows/routes.yml`), own keys (`routes/<YYYYMMDD>/routes.pmtiles`,
`routes/current.json`). The basemap workflows and `tiles/current.json` are not touched.

Layer `route` (route relations laid onto their member ways, merged):
`type` (hiking, cycling, mtb, horse, winter), `network` (international, national, regional,
local), `rank` (1-4), `name`, `ref`, `osmc_symbol`, `colour`, `distance` (km), `piste_type`
(winter only), `osm_id` (relation), `route_count` (only if more than one relation of the same
type runs on the way; the feature carries the best-ranked one). Min zoom: rank 1-2 z6, rank 3 z9,
rank 4 z11, winter z10.

Layer `way` (single ways with a grading, at least one of `sac`, `mtb`, `piste_type`):
`sac` (1-6), `mtb` (0-6), `piste_type`, `piste_difficulty`, `piste_grooming`,
`trail_visibility`, `name`, `osm_id`. Min zoom z11, pistes z10.

Pilot area: Alps / Allgaeu. One Geofabrik extract (`europe/alps`, md5 checked) is filtered with
`osmium tags-filter`, cut to the box 5.5,45.5,16.5,48.2 with `--strategy smart` and fed to the
profile; no merge of country extracts, so routes crossing borders stay complete.

Run: `gh workflow run routes.yml -f area=pilot -f upload=false`. The pilot archive stays a
workflow artifact (`routes-pilot`, 3 days), a cut of Oberstdorf (`routes-sample`, 7 days) comes
along with `sample=true`. `upload=true` writes `routes/<YYYYMMDD>/routes.pmtiles` and points
`routes/current.json` at it (the last three archives stay).

Licence: the data is © OpenStreetMap contributors under the ODbL. The archive is a derived
database with its own layer; on request it is provided as PMTiles or as the filtered PBF the
tiles were built from. Attribution in the metadata: `© OpenStreetMap contributors (ODbL)`.

## official (Amtliche Daten)

A separate overlay tile set (PMTiles, MVT, zoom 6-14) with official data mapped onto our own
schema, so the app draws, explains and combines every source alike. Own code (`official/`,
Python, stdlib only), own workflow (`.github/workflows/official.yml`), own keys
(`official/<YYYYMMDD>/official.pmtiles`, `official/<YYYYMMDD>/sources.json`,
`official/current.json`). Separate from the ODbL sets because licences and update cycles differ.

Fields shared by every zone layer (`hazard_zone` now, `protected_area` next; agreed in
`karte-zonen-schema-gemeinsam.md`, empty string when the source has nothing): `id`
(`<source>:<id in the source>`, stable, for promoteId), `name`, `kind` (wording of the source
class), `source` (data source id: `wlv`, `bozen`, `lfu-bayern`), `source_date`, `binding`
(`binding`/`recommended`, only with proof from the source), `link`, `rule_text`.

Layer `hazard_zone` (polygons), in addition: `hazard` (avalanche, water, mass_movement,
unspecified), `level` (high, medium, low, reserved, hint), `label` (wording of the level, e.g.
H3, Rote Zone), `zone_type` (plan, reserved_area, hint), `rank` (1-5), and when the source has
them `process` (flowing_avalanche, powder_avalanche, snow_glide, torrent, flood, debris_flow,
rockfall, landslide, deep_landslide, slope_failure, sinkhole, subsidence, waterlogging), `plan`,
`municipality`, `approved` (YYYY-MM-DD), `status`, `depth` (Bolzano: level of study).
Hazard zones are spatial planning, not the avalanche bulletin.

Layer `hazard_coverage` (polygons): where a source has data; `source`, `zone_type`,
`municipality`, `approximate` (true = convex hull of the zones of a municipality; LfU ships its
own overview). Outside coverage the app says it has no official zones, never that it is safe.

Sources and licences (also per source in `sources.json`):
- WLV Austria, hazard zone plans, OGC API Features `gis.lfrz.gv.at/api/geodata/i000901`:
  INSPIRE, no conditions apply. Red/yellow zones = plan, blue and space-relevant areas =
  reserved_area, brown/violet areas = hint.
- Autonomous Province of Bolzano, hazard zone plans, WFS `p_bz-TerritorialPlans`: CC0 1.0.
  H4/H3 high, H2 medium, H1 low; "examined and not dangerous" is dropped.
- LfU Bavaria, hazard indication map (Georisiken), shapefile download: CC BY 4.0,
  "Datenquelle: Bayerisches Landesamt für Umwelt, www.lfu.bayern.de". Hints only.

Run: `gh workflow run official.yml -f upload=false` (optional `-f bbox=...` and `-f sources=wlv,bozen,lfu-bayern`
for a trial). Tests: `cd official && python3 -m unittest`.
