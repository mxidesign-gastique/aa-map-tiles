"""Stops the run when the archive tippecanoe wrote breaks an attribute type the app relies on.

Reads the output of `tippecanoe-decode` (tiles -> layers -> features, nested FeatureCollections) and
checks every hazard_coverage feature: `approximate` must be the number 0 or 1. tippecanoe 2.53 wrote a
coalesced boolean `true` as another feature's zone id string; the build writes integers since, and
this check makes sure the archive still carries them.

    tippecanoe-decode -l hazard_coverage -Z6 -z9 official.pmtiles | python3 official/check_tiles.py
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator

LAYER = "hazard_coverage"


def layer_features(node: dict, layer: str = LAYER) -> Iterator[tuple[dict, dict]]:
    """(tile properties, feature) for every feature of `layer`, at any depth of the decode output."""
    def walk(n: dict, tile: dict) -> Iterator[tuple[dict, dict]]:
        props = n.get("properties") or {}
        if "zoom" in props:
            tile = props
        if n.get("type") == "FeatureCollection" and props.get("layer") == layer:
            for feature in n.get("features", []):
                yield tile, feature
            return
        for child in n.get("features", []):
            if isinstance(child, dict):
                yield from walk(child, tile)
    yield from walk(node, {})


def check(decoded: dict) -> tuple[int, list[str]]:
    """Number of coverage features seen, and one problem line per bad value (at most 20)."""
    seen = 0
    problems: list[str] = []
    for tile, feature in layer_features(decoded):
        seen += 1
        value = (feature.get("properties") or {}).get("approximate")
        # bool is an int in Python: name it so a `true` never passes as 1.
        if isinstance(value, bool) or value not in (0, 1) or not isinstance(value, int):
            if len(problems) < 20:
                where = f"{tile.get('zoom')}/{tile.get('x')}/{tile.get('y')}"
                problems.append(f"{where}: approximate={json.dumps(value)} ({type(value).__name__}), expected the number 0 or 1")
    if seen == 0:
        problems.append(f"no {LAYER} feature in the decoded tiles")
    return seen, problems


def main() -> None:
    seen, problems = check(json.load(sys.stdin))
    for line in problems:
        print(f"::error::{line}")
    print(f"{LAYER}: {seen} features checked, {len(problems)} problems")
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
