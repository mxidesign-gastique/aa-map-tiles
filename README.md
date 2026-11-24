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
