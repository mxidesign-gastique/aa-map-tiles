#!/usr/bin/env bash
# verify-archive.sh ARCHIVE PLACES
# PLACES: "Name lon lat;Name lon lat;..." The z10 tile of every place must hold map data:
# an empty or tiny tile means the region is missing from the archive.
set -euo pipefail
archive="$1"
places="$2"
failed=0
while read -r place lon lat; do
  [[ -n "$place" ]] || continue
  read -r x y < <(python3 -c "import math; z=10; n=2**z; r=math.radians($lat); print(int(($lon+180)/360*n), int((1-math.asinh(math.tan(r))/math.pi)/2*n))")
  bytes=$(pmtiles tile "$archive" 10 "$x" "$y" </dev/null 2>/dev/null | wc -c)
  echo "$place 10/$x/$y: $bytes bytes"
  if [[ "$bytes" -lt 5000 ]]; then failed=1; echo "::error::$place has no map data (10/$x/$y)"; fi
done < <(tr ';' '\n' <<< "$places")
exit "$failed"
