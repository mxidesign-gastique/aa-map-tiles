#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# generate-tiles.sh — build the basemap archive (PMTiles, OpenMapTiles schema)
# with Planetiler from Geofabrik extracts.
#
# Runs natively: Java 22+ and osmium-tool, no Docker. The usual place to run it
# is the GitHub workflow .github/workflows/map-tiles.yml (a fresh 16 GB runner
# with enough disk); a local run needs roughly 6x the input size free on WORK_DIR.
#
# Why Planetiler and not tilemaker: tilemaker kept the whole region set in
# memory and stalled in a 7.6 GiB Docker VM. Planetiler keeps node locations and
# features in memory-mapped files on disk and only needs a JVM heap of about
# half the input size.
#
# Environment:
#   WORK_DIR          downloads, merged PBF, Planetiler sources (default /tmp/aa-tiles)
#   TMP_DIR           Planetiler scratch files, the largest part (default $WORK_DIR/tmp)
#   OUTPUT            finished archive (default $WORK_DIR/europe.pmtiles)
#   REGIONS           Geofabrik paths, space separated (default: DE AT CH LI IT)
#   BOUNDS            minLon,minLat,maxLon,maxLat of the emitted tiles; must cover
#                     REGIONS (default fits DE AT CH LI IT)
#   PLANETILER_HEAP   JVM heap (default 4g)
#   DELETE_SOURCES=1  delete the single-region PBFs after merging (saves disk)
#
# Besides the OSM extracts it downloads water polygons, Natural Earth and lake
# centerlines (~1.5 GB) into WORK_DIR/data/sources.
# ---------------------------------------------------------------------------

PLANETILER_VERSION="0.10.2"
PLANETILER_SHA256="f310bd0413e2e4512b27f4046d418664e8e1d3bf31603c2a70e23de06c167e4d"

WORK_DIR="${WORK_DIR:-/tmp/aa-tiles}"
TMP_DIR="${TMP_DIR:-$WORK_DIR/tmp}"
OUTPUT="${OUTPUT:-$WORK_DIR/europe.pmtiles}"
REGIONS="${REGIONS:-europe/germany europe/austria europe/switzerland europe/liechtenstein europe/italy}"
# West: Aachen 5.87, north: Sylt 55.06, south: Lampedusa 35.49, east: Otranto 18.52.
BOUNDS="${BOUNDS:-5.5,35.2,19.0,55.2}"
PLANETILER_HEAP="${PLANETILER_HEAP:-4g}"
GEOFABRIK="https://download.geofabrik.de"

for tool in java osmium curl; do
  if ! command -v "$tool" &>/dev/null; then
    echo "ERROR: $tool is required (macOS: brew install openjdk osmium-tool; Ubuntu: apt install osmium-tool)" >&2
    exit 1
  fi
done

mkdir -p "$WORK_DIR/pbf" "$TMP_DIR" "$(dirname "$OUTPUT")"
WORK_DIR="$(cd "$WORK_DIR" && pwd)"

# ---------------------------------------------------------------------------
# 1. Planetiler jar, pinned and checksummed
# ---------------------------------------------------------------------------
JAR="$WORK_DIR/planetiler-$PLANETILER_VERSION.jar"
if [[ ! -f "$JAR" ]]; then
  echo "==> Downloading Planetiler $PLANETILER_VERSION"
  curl -fsSL -o "$JAR.part" \
    "https://github.com/onthegomap/planetiler/releases/download/v$PLANETILER_VERSION/planetiler.jar"
  mv "$JAR.part" "$JAR"
fi
echo "$PLANETILER_SHA256  $JAR" | shasum -a 256 -c - >/dev/null || {
  echo "ERROR: checksum mismatch for $JAR" >&2
  rm -f "$JAR"
  exit 1
}

# ---------------------------------------------------------------------------
# 2. Region extracts (resumable, checked against Geofabrik's md5)
# ---------------------------------------------------------------------------
md5_of() {
  if command -v md5sum &>/dev/null; then md5sum "$1" | cut -d' ' -f1; else md5 -q "$1"; fi
}

REGION_FILES=()
for region in $REGIONS; do
  name="${region//\//-}"
  pbf="$WORK_DIR/pbf/$name-latest.osm.pbf"
  url="$GEOFABRIK/$region-latest.osm.pbf"
  REGION_FILES+=("$pbf")
  if [[ -f "$pbf" ]]; then
    echo "==> $region already downloaded"
    continue
  fi
  echo "==> Downloading $region"
  curl -fL -C - --progress-bar -o "$pbf.part" "$url"
  # Geofabrik does not publish an .md5 for every extract (germany had none on
  # 05.11.2026). Without one, osmium reads the whole file, which catches a
  # truncated or corrupt download just as well.
  if expected="$(curl -fsSL "$url.md5" 2>/dev/null | cut -d' ' -f1)" && [[ -n "$expected" ]]; then
    if [[ "$(md5_of "$pbf.part")" != "$expected" ]]; then
      echo "ERROR: md5 mismatch for $region, deleting the partial file" >&2
      rm -f "$pbf.part"
      exit 1
    fi
  else
    echo "==> No md5 published for $region, checking the file with osmium"
    if ! osmium fileinfo -e -F pbf "$pbf.part" >/dev/null; then
      echo "ERROR: $region is not a readable PBF, deleting the partial file" >&2
      rm -f "$pbf.part"
      exit 1
    fi
  fi
  mv "$pbf.part" "$pbf"
done

# ---------------------------------------------------------------------------
# 3. Merge into one PBF (Planetiler reads one OSM file)
# ---------------------------------------------------------------------------
if [[ ${#REGION_FILES[@]} -eq 1 ]]; then
  INPUT="${REGION_FILES[0]}"
else
  INPUT="$WORK_DIR/merged.osm.pbf"
  if [[ -f "$INPUT" && "$(cat "$INPUT.regions" 2>/dev/null)" == "$REGIONS" ]]; then
    echo "==> Merged PBF already built for these regions"
  else
    echo "==> Merging ${#REGION_FILES[@]} regions"
    # Extracts are cut at different times, so an object along a border can sit in two
    # extracts with different versions; osmium merge keeps both and Planetiler then stops
    # with "Nodes must be sorted ascending by ID" (05.11.2026, DE/AT/CH/LI/IT). time-filter
    # without a time keeps only the newest version of every object.
    osmium merge "${REGION_FILES[@]}" -f pbf -o - \
      | osmium time-filter -F pbf - -o "$INPUT" --overwrite
    if [[ "$(osmium fileinfo -e -g data.multiple_versions "$INPUT")" != "no" ]]; then
      echo "ERROR: $INPUT still holds several versions of some objects" >&2
      rm -f "$INPUT"
      exit 1
    fi
    echo "$REGIONS" > "$INPUT.regions"
  fi
  if [[ "${DELETE_SOURCES:-0}" == "1" ]]; then
    rm -f "${REGION_FILES[@]}"
  fi
fi
ls -lh "$INPUT"

# ---------------------------------------------------------------------------
# 4. Planetiler's other sources, fetched here with retries
# ---------------------------------------------------------------------------
# Planetiler's own --download gives up after one slow answer: on 05.11.2026 the
# size request to osmdata.openstreetmap.de timed out and killed a run after the
# merge. These are the paths Planetiler reads by default, relative to WORK_DIR.
SOURCES_DIR="$WORK_DIR/data/sources"
mkdir -p "$SOURCES_DIR"
while read -r file url; do
  if [[ -s "$SOURCES_DIR/$file" ]]; then
    echo "==> $file already downloaded"
    continue
  fi
  echo "==> Downloading $file"
  curl -fL --retry 8 --retry-all-errors --retry-delay 20 --connect-timeout 30 --progress-bar \
    -o "$SOURCES_DIR/$file.part" "$url"
  mv "$SOURCES_DIR/$file.part" "$SOURCES_DIR/$file"
done <<'SOURCES'
water-polygons-split-3857.zip https://osmdata.openstreetmap.de/download/water-polygons-split-3857.zip
natural_earth_vector.sqlite.zip https://naciscdn.org/naturalearth/packages/natural_earth_vector.sqlite.zip
lake_centerline.shp.zip https://github.com/acalcutt/osm-lakelines/releases/download/v12/lake_centerline.shp.zip
SOURCES

# ---------------------------------------------------------------------------
# 5. Planetiler (OpenMapTiles profile)
# ---------------------------------------------------------------------------
# Sources come from step 4, so no --download. --bounds is required: a merged PBF has
# no usable bounding box and Planetiler would emit ocean tiles for the whole world.
# The style reads name:latin only; --languages keeps the other name keys small.
# The building merge at z13 costs a lot of CPU and the style shows buildings at
# z14 only.
echo "==> Running Planetiler (heap $PLANETILER_HEAP, bounds $BOUNDS)"
(
  cd "$WORK_DIR"
  java "-Xmx$PLANETILER_HEAP" -jar "$JAR" \
    --osm-path="$INPUT" \
    --output="$OUTPUT" \
    --force \
    --tmpdir="$TMP_DIR" \
    --bounds="$BOUNDS" \
    --maxzoom=14 \
    --languages=de,en,it,fr \
    --building-merge-z13=false \
    --nodemap-type=sparsearray \
    --storage=mmap
)

rm -rf "$TMP_DIR"
echo "==> Done: $OUTPUT ($(du -h "$OUTPUT" | cut -f1))"
