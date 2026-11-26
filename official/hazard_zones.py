"""Map official natural-hazard zones onto the tile set schema `official`, layer `hazard_zone`.

One schema for every source, so the app draws, explains and filters all of them alike
(README.md, section "official (Amtliche Daten)"):

  shared with protected_area (karte-zonen-schema-gemeinsam.md; '' when the source has nothing):
  id            <source>:<id in the source>, stable (promoteId)
  name          proper name, '' for hazard zones (they have none)
  kind          wording of the source class ("Summenzone - Rote Zone Lawine")
  source        data source id of the register: wlv-gzp | provinz-bozen | lfu-bayern
  source_date   data date of the source (YYYY-MM-DD or a year), else '' (never the build day; Bolzano gives none)
  binding       binding | recommended | '' - only with proof from the source, so '' here
  link, rule_text  '' (no rule text for hazard zones)
  hazard_zone only:
  hazard        avalanche | water | mass_movement | unspecified
  level         high | medium | low | reserved | hint
  label         wording of the level in the source ("Rote Zone", "H3", "Gefahrenhinweis")
  zone_type     plan (zone of an approved plan) | reserved_area | hint (indication map)
  rank          1 (high) ... 5 (hint), draw order
  process, plan, municipality, approved (YYYY-MM-DD), status, depth  when the source has them

Sources: WLV Austria (OGC API Features), Autonomous Province of Bolzano (WFS, CC0),
LfU Bavaria (hazard indication map, shapefile, CC BY 4.0). Rows a source marks as
"examined and not dangerous" are dropped: they are no zone.
"""

from __future__ import annotations

import re
from typing import Any, Optional

RANK = {"high": 1, "medium": 2, "low": 3, "reserved": 4, "hint": 5}

Feature = dict[str, Any]


def _date(value: Any) -> Optional[str]:
    """dd.mm.yyyy -> YYYY-MM-DD; a bare year stays a year; anything else -> None."""
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if match:
        day, month, year = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    if re.fullmatch(r"\d{4}", text):
        return text
    return None


SHARED = ("id", "name", "kind", "source", "source_date", "binding", "link", "rule_text")


def _zone(geometry: Any, **props: Any) -> Feature:
    props["rank"] = RANK[props["level"]]
    out = {key: ("" if props.get(key) is None else props.get(key)) for key in SHARED}
    out.update({k: v for k, v in props.items() if k not in SHARED and v not in (None, "")})
    return {"type": "Feature", "geometry": geometry, "properties": out}


# --- WLV Austria -------------------------------------------------------------------------------

WLV_PROCESS = {
    "rutschung": ("mass_movement", "landslide"),
    "steinschlag": ("mass_movement", "rockfall"),
    "vernässung": ("water", "waterlogging"),
}


def map_wlv(feature: Feature, collection: str) -> Optional[Feature]:
    """One feature of a WLV collection (`i000901:wlv_gzp_*`) -> hazard_zone, or None."""
    p = feature.get("properties") or {}
    label = str(p.get("objektart") or "").strip()
    name = collection.split(":")[-1]
    hazard, process, level, zone_type, level_raw = "unspecified", None, None, None, None

    if name in ("wlv_gzp_sz_la", "wlv_gzp_sz_wb"):
        hazard = "avalanche" if name.endswith("_la") else "water"
        process = None if hazard == "avalanche" else "torrent"
        if "Rote Zone" in label:
            level, level_raw = "high", "Rote Zone"
        elif "Gelbe Zone" in label:
            level, level_raw = "medium", "Gelbe Zone"
        zone_type = "plan"
    elif name == "wlv_gzp_bvb":
        level, zone_type, level_raw = "reserved", "reserved_area", "Blauer Vorbehaltsbereich"
    elif name == "wlv_gzp_rrb":
        level, zone_type, level_raw = "reserved", "reserved_area", "Raumrelevanter Bereich"
    elif name == "wlv_gzp_bhb":
        level, zone_type, level_raw = "hint", "hint", "Brauner Hinweisbereich"
        kind = label.split(" - ", 1)[-1].strip().lower()
        hazard, process = WLV_PROCESS.get(kind, ("unspecified", None))
    elif name == "wlv_gzp_vhb":
        level, zone_type, level_raw = "hint", "hint", "Violetter Hinweisbereich"
    if level is None:
        return None

    approved = _date(p.get("datum ministerielle genehmigung"))
    return _zone(
        feature.get("geometry"),
        hazard=hazard,
        process=process,
        level=level,
        zone_type=zone_type,
        label=level_raw,
        kind=label,
        plan=p.get("name des gefahrenzonenplans"),
        municipality=p.get("hauptgemeindename"),
        approved=approved,
        status=p.get("status"),
        source="wlv-gzp",
        id=f"wlv-gzp:{feature.get('id') or p.get('wlk_id') or ''}",
        source_date=_date(p.get("export")),
    )


# --- Autonomous Province of Bolzano ----------------------------------------------------------

BZ_HAZARD = {"Lawine": "avalanche", "Wassergefahr": "water", "Massenbewegung": "mass_movement"}
BZ_LEVEL = {"H4": "high", "H3": "high", "H2": "medium", "H1": "low"}
BZ_PROCESS = {
    "Fließlawine": "flowing_avalanche",
    "Staublawine": "powder_avalanche",
    "Gleitschnee": "snow_glide",
    "Übersarung": "flood",
    "Überflutung": "flood",
    "Vermurung": "debris_flow",
    "Sturz": "rockfall",
    "Hangmure": "slope_failure",
    "Rutschung": "landslide",
}


def map_bz(feature: Feature) -> Optional[Feature]:
    """One feature of `UrbanPlan-HazardZonePlan-*` -> hazard_zone, or None (not dangerous)."""
    p = feature.get("properties") or {}
    label = str(p.get("GEFAHR") or "").strip()
    prefix = label.split(" - ", 1)[0]
    match = re.search(r"\((H[1-4])\)", label)
    if not match or prefix not in BZ_HAZARD:
        return None  # "Untersucht und nicht gefährlich" or an unknown class
    status = p.get("DESC_D") or None
    return _zone(
        feature.get("geometry"),
        hazard=BZ_HAZARD[prefix],
        process=BZ_PROCESS.get(str(p.get("PROZESS") or "")),
        level=BZ_LEVEL[match.group(1)],
        zone_type="plan",
        label=match.group(1),
        kind=label,
        municipality=p.get("NAME_D"),
        status=status,
        depth=p.get("BEARBEITUNGSTIEFE"),
        source="provinz-bozen",
        id=f"provinz-bozen:{p.get('OBJECTID') or feature.get('id') or ''}",
    )


# --- LfU Bavaria -------------------------------------------------------------------------------

LFU_PROCESS = [
    ("steinschlag", ("mass_movement", "rockfall")),
    ("tiefreichende rutschung", ("mass_movement", "deep_landslide")),
    ("rutschung", ("mass_movement", "landslide")),
    ("hanganbruch", ("mass_movement", "slope_failure")),
    ("subrosion", ("mass_movement", "sinkhole")),
    ("senkung", ("mass_movement", "subsidence")),
]


def map_lfu(record: Feature, layer: str, index: int = 0) -> Optional[Feature]:
    """One shape record of the hazard indication map (`ghk_*`) -> hazard_zone, or None.

    `ghk_uebersicht` is the coverage, not a zone; build_official writes it to hazard_coverage.
    """
    if layer.startswith("ghk_uebersicht"):
        return None
    p = record.get("properties") or {}
    label = str(p.get("PROZESS") or "").strip()
    lowered = label.lower()
    hazard, process = next((value for key, value in LFU_PROCESS if key in lowered), ("mass_movement", None))
    return _zone(
        record.get("geometry"),
        hazard=hazard,
        process=process,
        level="hint",
        zone_type="hint",
        label="Gefahrenhinweis",
        kind=label,
        source="lfu-bayern",
        id=f"lfu-bayern:{layer.removesuffix('_epsg4258')}:{index}",
        source_date=_date(p.get("STAND")),
    )
