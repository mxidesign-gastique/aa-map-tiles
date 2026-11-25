"""Protected-area mapping for the `protected_area` tile layer (Python 3, stdlib only).

Ported from the TypeScript zone adapters of the backend (bafu-zone-sources.ts,
lfu-bayern-source.ts, vorarlberg-zone-source.ts, bozen-zone-source.ts, zone-source.ts,
zone-geometry.ts). Input is a GeoJSON Feature in EPSG:4326 as ogr2ogr writes it from the source
service; output is a GeoJSON Feature with the geometry unchanged and the shared properties of the
zone schema, or None when the record is not a zone we take.

Texts come from the source fields only: `rule_text` is never invented, and a source that states
nothing gives ''. bafu-wildlife is deliberately NOT here (no licence proof); tirol is not connected.
"""

import calendar
import re

# Zone classes of the shared schema.
NO_ENTRY = "no_entry"
SEASONAL = "seasonal"
RULES = "rules"
PROTECTED = "protected"

# `source` is the id of the data-source register (credit), not of the adapter.
CREDIT_BAFU = "bafu-schutzgebiete"
CREDIT_LFU = "lfu-bayern"
CREDIT_VORARLBERG = "land-vorarlberg"
CREDIT_BOZEN = "provinz-bozen"

COORDINATE_STEP = 1e5
LABEL_SCANLINES = 16
LABEL_REFINEMENTS = 8


# --- text helpers -------------------------------------------------------------------------------

def trimmed(value):
    """A source value as stripped text; anything that is not a string is ''."""
    return value.strip() if isinstance(value, str) else ""


def lines(*parts):
    """Non-empty trimmed parts joined by line breaks."""
    return "\n".join(text for text in (trimmed(part) for part in parts) if text)


def capitalised(value):
    """First letter in capitals: 'örtliches Schutzgebiet' -> 'Örtliches Schutzgebiet'."""
    text = trimmed(value)
    return text[:1].upper() + text[1:]


def without_number(name):
    """'Piz Ela (Nr. 20.00)' is the zone 'Piz Ela': the number is the source's index."""
    return re.sub(r"\s*\(Nr\.[^)]*\)\s*$", "", trimmed(name))


_DAY_MONTH = r"(\d{1,2})\.\s*(\d{1,2})\.?"
_PERIOD = re.compile(rf"^{_DAY_MONTH}\s*(?:-|–|—|bis)\s*{_DAY_MONTH}\.*$", re.IGNORECASE | re.ASCII)


def _month_day(month, day):
    if not 1 <= month <= 12:
        return None
    if not 1 <= day <= calendar.monthrange(2024, month)[1]:
        return None
    return f"{month:02d}-{day:02d}"


def parse_protection_time(text):
    """Swiss protection time -> ('period', (from, to)) | ('all_year', None) | ('unreadable', None) | ('none', None).

    '20.12. - 30.04.' is a period (it may run over the turn of the year), '01.01. - 31.12.' is all
    year, anything else ('En cas de neige.', '16.12. bis Ende Skisaison.') is text we do not read
    into dates; an empty one is none.
    """
    value = re.sub(r"\s+", " ", trimmed(text))
    if not value:
        return ("none", None)
    match = _PERIOD.match(value)
    if not match:
        return ("unreadable", None)
    from_day, from_month, to_day, to_month = (int(group) for group in match.groups())
    start = _month_day(from_month, from_day)
    end = _month_day(to_month, to_day)
    if not start or not end:
        return ("unreadable", None)
    if start == "01-01" and end == "12-31":
        return ("all_year", None)
    return ("period", (start, end))


# --- geometry helpers ---------------------------------------------------------------------------

def _polygons(geometry):
    """The polygons (list of rings) of a Polygon or MultiPolygon geometry; anything else gives none."""
    if not isinstance(geometry, dict):
        return []
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list):
        return []
    if geometry.get("type") == "Polygon":
        return [coordinates] if coordinates else []
    if geometry.get("type") == "MultiPolygon":
        return [rings for rings in coordinates if rings]
    return []


def _ring_area(ring):
    total = 0.0
    for index in range(len(ring) - 1):
        total += ring[index][0] * ring[index + 1][1] - ring[index + 1][0] * ring[index][1]
    return abs(total) / 2


def _crossings(rings, y):
    """x values where the horizontal line y crosses the rings, sorted (half-open rule)."""
    xs = []
    for ring in rings:
        for index in range(len(ring) - 1):
            x1, y1 = ring[index][0], ring[index][1]
            x2, y2 = ring[index + 1][0], ring[index + 1][1]
            if (y1 > y) != (y2 > y):
                xs.append(x1 + ((y - y1) / (y2 - y1)) * (x2 - x1))
    return sorted(xs)


def _round(value):
    return int(value * COORDINATE_STEP + 0.5) / COORDINATE_STEP if value >= 0 else -int(-value * COORDINATE_STEP + 0.5) / COORDINATE_STEP


def _label_point_of(rings):
    """A point inside the polygon, away from its edges: the middle of the widest stretch of a few
    horizontal lines (even-odd, so holes count), refined around the best line."""
    outer = rings[0]
    south = min(point[1] for point in outer)
    north = max(point[1] for point in outer)

    def widest_on(ys, best=None):
        for y in ys:
            xs = _crossings(rings, y)
            for index in range(0, len(xs) - 1, 2):
                width = xs[index + 1] - xs[index]
                if best is None or width > best[0]:
                    best = (width, (_round((xs[index] + xs[index + 1]) / 2), _round(y)))
        return best

    step = (north - south) / (LABEL_SCANLINES + 1)
    coarse = widest_on([south + (line + 1) * step for line in range(LABEL_SCANLINES)])
    if coarse is None:
        return [outer[0][0], outer[0][1]]
    fine_ys = [coarse[1][1] + ((line + 0.5 - LABEL_REFINEMENTS / 2) * step) / LABEL_REFINEMENTS * 2 for line in range(LABEL_REFINEMENTS)]
    fine = widest_on(fine_ys)
    best = fine if fine is not None and fine[0] > coarse[0] else coarse
    return [best[1][0], best[1][1]]


def label_point(feature):
    """[lon, lat] of one label point inside the largest part of the feature, or None without a polygon.

    The map names a zone once: the point goes to the `zone_label` layer, not to the zone properties.
    """
    polygons = _polygons((feature or {}).get("geometry"))
    if not polygons:
        return None
    largest = polygons[0]
    for rings in polygons[1:]:
        if _ring_area(rings[0]) > _ring_area(largest[0]):
            largest = rings
    return _label_point_of(largest)


# --- shared output ------------------------------------------------------------------------------

def _properties(feature):
    properties = (feature or {}).get("properties")
    return properties if isinstance(properties, dict) else {}


def _has_polygon(feature):
    return bool(_polygons((feature or {}).get("geometry")))


def _id_text(value):
    """An id of the source as text; None when it has none (bool and None are not ids)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    return text or None


def _fid(feature, *names):
    """The record id: the first of the given property names that is set, else the Feature id."""
    properties = _properties(feature)
    for name in names:
        found = _id_text(properties.get(name))
        if found:
            return found
    return _id_text((feature or {}).get("id")) or _id_text((feature or {}).get("featureId"))


def _zone(feature, *, id, source, name, kind, zone_class, source_date="", binding="", link="",
          rule_text="", period=None, period_text=""):
    """The output Feature: geometry unchanged, properties exactly as the shared schema."""
    return {
        "type": "Feature",
        "geometry": feature["geometry"],
        "properties": {
            "id": id,
            "name": name,
            "kind": kind,
            "source": source,
            "source_date": source_date or "",
            "binding": binding,
            "link": link,
            "rule_text": rule_text,
            "zone_class": zone_class,
            "period_from": period[0] if period else "",
            "period_to": period[1] if period else "",
            "period_text": period_text,
        },
    }


# --- BAFU (Switzerland) -------------------------------------------------------------------------

PARK_KINDS = {"SNP": "Nationalpark", "RN": "Regionaler Naturpark", "NE": "Naturerlebnispark"}


def _binding_of(value):
    text = trimmed(value)
    return "binding" if text == "rechtsverbindlich" else "recommended" if text == "empfohlen" else ""


def _rule_text(properties):
    """The provision of a record, then what the source adds where that is not the same text."""
    provision = trimmed(properties.get("best_de"))
    extra = trimmed(properties.get("zusatzinformation"))
    return lines(provision, "" if extra.lower() == provision.lower() else extra)


def _map_park(feature, national, source_date):
    if not _has_polygon(feature):
        return None
    properties = _properties(feature)
    fid = _fid(feature, "featureId", "id")
    if fid is None:
        return None
    category = trimmed(properties.get("kategorie"))
    if (category == "SNP") != national:
        return None
    prefix = "bafu-np" if national else "bafu-park"
    return _zone(
        feature, id=f"{prefix}:{fid}", source=CREDIT_BAFU, source_date=source_date,
        name=trimmed(properties.get("name")), kind=PARK_KINDS.get(category) or capitalised(category),
        zone_class=RULES if national else PROTECTED,
    )


def map_bafu_np(feature, source_date=""):
    """Swiss National Park: the SNP record of the parks dataset, class rules, no rule text."""
    return _map_park(feature, True, source_date)


def map_bafu_park(feature, source_date=""):
    """Regional nature parks and nature experience parks (everything but SNP): outline only."""
    return _map_park(feature, False, source_date)


def map_bafu_jagdbann(feature, source_date=""):
    """Federal wildlife reserve (Jagdbanngebiet). The allowed routes (lines) of the layer are left out."""
    if not _has_polygon(feature):
        return None
    properties = _properties(feature)
    fid = _fid(feature, "featureId", "id")
    if fid is None:
        return None
    time = trimmed(properties.get("schutzzeit"))
    state, period = parse_protection_time(time)
    return _zone(
        feature, id=f"bafu-jagdbann:{fid}", source=CREDIT_BAFU, source_date=source_date,
        name=without_number(properties.get("jb_name")), kind="Jagdbanngebiet", zone_class=RULES,
        rule_text=_rule_text(properties), binding=_binding_of(properties.get("schutzs_de")),
        period=period if state == "period" else None, period_text="" if state == "all_year" else time,
    )


# --- LfU Bayern ---------------------------------------------------------------------------------

# type -> (kind, class); the concept table: what a visitor must follow, or the outline only.
BAVARIA_TYPES = {
    "nationalpark": ("Nationalpark", RULES),
    "naturschutzgebiet": ("Naturschutzgebiet", RULES),
    "nationales_naturmonument": ("Nationales Naturmonument", RULES),
    "naturdenkmal_flaechig": ("Naturdenkmal", RULES),
    "landschaftsschutzgebiet": ("Landschaftsschutzgebiet", PROTECTED),
    "naturpark": ("Naturpark", PROTECTED),
    "biosphaerenreservat": ("Biosphärenreservat", PROTECTED),
    "fauna_flora_habitat_gebiet": ("FFH-Gebiet", PROTECTED),
    "vogelschutzgebiet": ("Vogelschutzgebiet", PROTECTED),
    "landschaftsbestandteil_flaechig": ("Geschützter Landschaftsbestandteil", PROTECTED),
}


def map_lfu_bayern(feature, type_name=None, source_date=""):
    """A Bavarian protected area. The WFS type comes from `type_name` or from the GmlID prefix
    ('naturschutzgebiet.516'); GmlID may also stand in the Feature id (ogr2ogr)."""
    if not _has_polygon(feature):
        return None
    properties = _properties(feature)
    gml_id = _id_text(properties.get("GmlID")) or _id_text(properties.get("gml_id")) or _id_text(feature.get("id"))
    if gml_id is None:
        return None
    type_name = type_name or gml_id.rsplit(".", 1)[0]
    found = BAVARIA_TYPES.get(type_name)
    if found is None:
        return None
    kind, zone_class = found
    zone = trimmed(properties.get("zone_nam"))
    return _zone(
        feature, id=f"lfu-bayern:{gml_id}", source=CREDIT_LFU, source_date=source_date,
        name=trimmed(properties.get("name")) or trimmed(properties.get("NAME")),
        kind=f"{kind} ({zone})" if zone else kind, zone_class=zone_class, link=trimmed(properties.get("LINK")),
    )


# --- Vorarlberg ---------------------------------------------------------------------------------

# Categories with provisions a visitor must follow; every other category is an outline.
VORARLBERG_RULE_CATEGORIES = frozenset({"Naturschutzgebiet", "Pflanzenschutzgebiet", "Ruhezone"})


def map_vorarlberg(feature, source_date=""):
    if not _has_polygon(feature):
        return None
    properties = _properties(feature)
    uuid = _id_text(properties.get("uuid"))
    if uuid is None:
        return None
    category = trimmed(properties.get("kategorie"))
    return _zone(
        feature, id=f"vorarlberg:{uuid}", source=CREDIT_VORARLBERG, source_date=source_date,
        name=trimmed(properties.get("name")), kind=capitalised(category),
        zone_class=RULES if category in VORARLBERG_RULE_CATEGORIES else PROTECTED, link=trimmed(properties.get("ris_link")),
    )


# --- Bozen (South Tyrol) ------------------------------------------------------------------------

LANDSCAPE_PROTECTION = "Landschaftsschutzgebiet"
BOZEN_TYPES = {  # WFS type -> key used in the id
    "LandscapePlan-NationalAndNaturalParks": "parks",
    "LandscapePlan-Biotopes": "biotopes",
    "LandscapePlan-Natura2000": "natura2000",
    "LandscapePlan-ProtectedAreas": "landscape",
}


def _bozen_key(feature, type_key):
    """The dataset key: given, or from the ogr2ogr Feature id ('LandscapePlan-Biotopes.123'), or from BEZ_D."""
    if type_key in BOZEN_TYPES.values():
        return type_key
    if type_key in BOZEN_TYPES:
        return BOZEN_TYPES[type_key]
    gml_id = _id_text(_properties(feature).get("gml_id")) or _id_text((feature or {}).get("id")) or ""
    prefix = gml_id.rsplit(".", 1)[0].split(":")[-1]
    if prefix in BOZEN_TYPES:
        return BOZEN_TYPES[prefix]
    category = trimmed(_properties(feature).get("BEZ_D"))
    if "N2000_CODE" in _properties(feature):
        return "natura2000"
    if category in ("Nationalpark", "Naturpark"):
        return "parks"
    if category == "Geschütztes Biotop":
        return "biotopes"
    if category == LANDSCAPE_PROTECTION:
        return "landscape"
    return None


def map_bozen(feature, type_key=None, source_date=""):
    """A South Tyrol protected area of the landscape plan.

    Kept: national park, nature parks, protected biotopes, Natura 2000 sites and named
    Landschaftsschutzgebiete. Dropped: Gebiet mit Ensembleschutz, Landschaftliche Bannzone,
    Landschaftsschutzermächtigung durch die Landesverwaltung (planning categories, not rules for
    visitors) and unnamed Landschaftsschutzgebiete (cannot be told apart on the map).
    """
    if not _has_polygon(feature):
        return None
    properties = _properties(feature)
    key = _bozen_key(feature, type_key)
    object_id = _id_text(properties.get("OBJECTID"))
    if key is None or object_id is None:
        return None
    category = trimmed(properties.get("BEZ_D"))
    link = ""
    if key == "parks":
        found = {"Nationalpark": ("Nationalpark", RULES), "Naturpark": ("Naturpark", PROTECTED)}.get(category)
        name, link = trimmed(properties.get("NSO_DESC_DE")), trimmed(properties.get("NSO_LINK_DE"))
    elif key == "biotopes":
        found = ("Geschütztes Biotop", RULES)
        name, link = trimmed(properties.get("NSO_DESC_DE")), trimmed(properties.get("NSO_LINK_DE"))
    elif key == "natura2000":
        found = ("Natura-2000-Gebiet", PROTECTED)
        name = trimmed(properties.get("BEZEICHNUNG_D")) or category
        link = trimmed(properties.get("N2000_CODE_LINK_DE"))
    else:
        found = (LANDSCAPE_PROTECTION, PROTECTED) if category == LANDSCAPE_PROTECTION else None
        name = trimmed(properties.get("NSO_DESC_DE"))
    if found is None or (key == "landscape" and not name):
        return None
    kind, zone_class = found
    return _zone(
        feature, id=f"bozen:{key}:{object_id}", source=CREDIT_BOZEN, source_date=source_date,
        name=name, kind=kind, zone_class=zone_class, link=link,
    )


# Source id (dashes are underscores in the function name) -> mapping function.
SOURCES = {
    "bafu-np": map_bafu_np,
    "bafu-park": map_bafu_park,
    "bafu-jagdbann": map_bafu_jagdbann,
    "lfu-bayern": map_lfu_bayern,
    "vorarlberg": map_vorarlberg,
    "bozen": map_bozen,
}


# --- where the data comes from ------------------------------------------------------------------
#
# Per source a list of reads. "input" + "layer" go to ogr2ogr (output EPSG:4326, the WFS feature id
# lands in the properties as gml_id); "args" are the extra arguments of the map function.
#
# BAFU is the exception ("format": "geoadmin_find"): the mapping reads the attribute names of the
# geo.admin API (jb_name, schutzzeit, best_de, kategorie ...). The download files on data.geo.admin.ch
# carry other column names (ObjNummer, Name, Kategorie ...), so they would need a second mapping.
# One find call per layer with an empty search text returns every feature (checked 25.11.2026:
# Jagdbann 43, parks 20; about 1 MB and 1.4 MB). The answer is {"results": [Feature, ...]} with
# featureId at the top level and the attributes in "properties"; read it with urllib, not ogr2ogr.
GEOADMIN_FIND = (
    "https://api3.geo.admin.ch/rest/services/api/MapServer/find?layer={layer}&searchField={field}"
    "&searchText=&contains=true&returnGeometry=true&geometryFormat=geojson&sr=4326"
)
_PARKS = GEOADMIN_FIND.format(layer="ch.bafu.schutzgebiete-paerke_nationaler_bedeutung_perimeter", field="kategorie")
_LFU = "WFS:https://www.lfu.bayern.de/gdi/wfs/natur/schutzgebiete"
_BOZEN = "WFS:https://geoservices1.civis.bz.it/geoserver/p_bz-TerritorialPlans/ows"

FETCH = {
    "bafu-np": [{"format": "geoadmin_find", "input": _PARKS, "layer": "", "args": {}}],
    "bafu-park": [{"format": "geoadmin_find", "input": _PARKS, "layer": "", "args": {}}],
    "bafu-jagdbann": [{
        "format": "geoadmin_find",
        "input": GEOADMIN_FIND.format(layer="ch.bafu.wrz-jagdbanngebiete_select", field="kanton"),
        "layer": "", "args": {},
    }],
    "lfu-bayern": [
        {"format": "ogr", "input": _LFU, "layer": f"natur_wfsschutzgebiete:{type_name}", "args": {"type_name": type_name}}
        for type_name in BAVARIA_TYPES
    ],
    "vorarlberg": [{
        "format": "ogr",
        "input": "WFS:https://vogis.cnv.at/geoserver/vogis/schutzgebiete_naturschutz/wfs",
        "layer": "vogis:schutzgebiete_naturschutz", "args": {},
    }],
    "bozen": [
        {"format": "ogr", "input": _BOZEN, "layer": f"p_bz-TerritorialPlans:{type_name}", "args": {"type_key": key}}
        for type_name, key in BOZEN_TYPES.items()
    ],
}
