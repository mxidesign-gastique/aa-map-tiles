"""Map the signed leisure trails of Bavaria (BVV, ATKIS Basis-DLM plus) onto layer `route`.

Source: object type `L_53901_Freizeitwegachse_BY` of `by_basisdlm_plus.gpkg`
(https://geodaten.bayern.de/odd/m/2/basisdlm/plus/by_basisdlm_plus.zip, weekly, CC BY 4.0,
"Bayerische Vermessungsverwaltung – www.geodaten.bayern.de"). Value lists after the data model
(https://geodaten.bayern.de/odd/m/3/pdf/basisdlm_plus_datenmodell.pdf, pp. 57-59) and, where the data
spells them otherwise, after the data itself (FW_TYP "örtl. Wanderweg", "Mountainbikeweg"; FW_BYNET "kein ...").

The fields follow the route schema of karte-wegenetze (README "routes"), so its style applies
without a special case: `type`, `network`, `rank` (1-4), `name`, `ref`, `route_id` (integer,
unique in the set), plus `source`, `source_id`, `source_date`, `surface`, `traffic`, `main_route`.
`source_date` is the data date of the download (YYYY-MM-DD), as the hazard zones carry it.
The surface is data for the sheet, never a colour.
"""

from __future__ import annotations

import re
import zlib
from typing import Any, Optional

Feature = dict[str, Any]

SOURCE = "bvv-freizeitwege"

# FW_TYP -> (type, network, rank). The BVV has no national/international class; long trails are
# "Wanderweg"/"Radweg" too, the Bayernnetz für Radler is the state network.
# The data itself writes "örtl. Wanderweg" and "Mountainbikeweg" (GeoPackage and the QGIS project in the same zip,
# read 26.11.2026: 654,612 and 62,554 axes); the data model's spellings are kept in case the export follows it.
TYPES = {
    "örtl. wanderweg": ("hiking", "local", 4),
    "örtlicher wanderweg": ("hiking", "local", 4),
    "wanderweg": ("hiking", "regional", 3),
    "radweg": ("cycling", "regional", 3),
    "mountainbikeweg": ("mtb", "local", 4),
    "mountainbike": ("mtb", "local", 4),
    "moutainbike": ("mtb", "local", 4),  # spelling of the data model
}
# FW_TYP values left out on purpose, not counted as unknown: an axis without a trail type (its own empty
# category in the BVV's QGIS project) cannot be drawn as hiking, cycling or MTB.
SKIPPED: frozenset[str] = frozenset({""})
SURFACE = {
    "bitumen, asphalt, beton": "paved",
    "kein bitumen, asphalt, beton": "unpaved",
    "noch nicht bekannt": "unknown",
}
TRAFFIC = {
    "auf strecke mit dichtem verkehrsaufkommen": "heavy",
    "auf strecke mit mittlerem verkehrsaufkommen": "medium",
    "abgesetzt, kein oder geringes verkehrsaufkommen": "low",
}
# Tile min zoom by rank, as agreed with karte-wegenetze: rank 1-2 z6, 3 z9, 4 z11.
MIN_ZOOM = {1: 6, 2: 6, 3: 9, 4: 11}


def _key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def route_id_of(fw_id: Any) -> int:
    """FW_ID as a stable integer: digits as they are, anything else a CRC32 (BVV ids are numeric)."""
    text = str(fw_id or "").strip()
    if re.fullmatch(r"\d{1,15}", text):
        return int(text)
    return zlib.crc32(text.encode("utf-8"))


def unknown_reason(feature: Feature) -> Optional[str]:
    """Why `map_bvv` gave None: the raw FW_TYP for a value the lists do not know, a marker for a missing
    geometry, None for a type left out on purpose (`SKIPPED`). The build counts these per reason."""
    p = feature.get("properties") or {}
    key = _key(p.get("FW_TYP"))
    if key in SKIPPED:
        return None
    if key in TYPES and not feature.get("geometry"):
        return "(no geometry)"
    return str(p.get("FW_TYP"))


def map_bvv(feature: Feature, source_date: str = "") -> Optional[Feature]:
    """One `L_53901_Freizeitwegachse_BY` feature -> route, or None for an unknown trail type."""
    p = feature.get("properties") or {}
    kind = TYPES.get(_key(p.get("FW_TYP")))
    if not kind or not feature.get("geometry"):
        return None
    route_type, network, rank = kind
    on_bayernnetz = _key(p.get("FW_BYNET")) == "bayernnetz für radler"
    if route_type == "cycling" and on_bayernnetz:
        network, rank = "national", 2  # the state-wide network, drawn from z6
    if _key(p.get("FRO_fzwRoute_BY")) == "zusatzroute":
        rank = min(rank + 1, 4)
    props = {
        "type": route_type,
        "network": network,
        "rank": rank,
        "name": str(p.get("FW_NAME") or "").strip(),
        "ref": "",
        "route_id": route_id_of(p.get("FW_ID")),
        "source": SOURCE,
        "source_id": str(p.get("FW_ID") or ""),
        "source_date": source_date,
        "surface": SURFACE.get(_key(p.get("FOM_fzwOberflmaterial_BY")), "unknown"),
    }
    traffic = TRAFFIC.get(_key(p.get("FVA_fzwVerkaufkommen_BY")))
    if traffic:
        props["traffic"] = traffic
    if on_bayernnetz:
        props["bayernnetz"] = True
    if _key(p.get("FLA_fzwLage_BY")) == "außerhalb bayerns":
        props["outside_bavaria"] = True
    return {"type": "Feature", "geometry": feature["geometry"], "properties": props, "tippecanoe": {"minzoom": MIN_ZOOM[rank]}}
