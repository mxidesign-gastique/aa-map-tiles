"""Tests for export_relations.py with tiny synthetic .osm XML files (pyosmium reads XML).

Run: python3 -m pytest routes/test_export_relations.py
"""

import gzip
import json
import math
import re
from xml.sax.saxutils import quoteattr

import pytest

import export_relations as ex

LAT = 47.5
LON = 10.0
STEP_LON = 0.001  # about 75 m at 47.5 degrees north
GAP_LAT = 0.00108  # about 120 m


def osm_xml(nodes, ways, relations):
    """nodes: {id: (lon, lat)}; ways: {id: [node ids]}; relations: {id: (tags, [(type, ref, role)])}."""
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6" generator="test">']
    for node_id, (lon, lat) in sorted(nodes.items()):
        out.append(f'<node id="{node_id}" version="1" lat="{lat}" lon="{lon}"/>')
    for way_id, refs in sorted(ways.items()):
        out.append(f'<way id="{way_id}" version="1">' + "".join(f'<nd ref="{r}"/>' for r in refs) + "</way>")
    for rel_id, (tags, members) in sorted(relations.items()):
        body = "".join(f'<member type="{t}" ref="{r}" role={quoteattr(role)}/>' for t, r, role in members)
        body += "".join(f"<tag k={quoteattr(k)} v={quoteattr(v)}/>" for k, v in tags.items())
        out.append(f'<relation id="{rel_id}" version="3">{body}</relation>')
    out.append("</osm>")
    return "\n".join(out)


def run(tmp_path, nodes, ways, relations, source_date="2026-11-25", name="in.osm"):
    src = tmp_path / name
    src.write_text(osm_xml(nodes, ways, relations), encoding="utf-8")
    dst = tmp_path / "out.ndjson.gz"
    counts = ex.export(str(src), str(dst), source_date)
    with gzip.open(dst, "rt", encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    return rows, counts


def line_nodes(count, start_id=1, lon0=LON, lat=LAT):
    """count nodes on an east-going line, 0.001 degrees apart."""
    return {start_id + i: (round(lon0 + i * STEP_LON, 7), lat) for i in range(count)}


def hiking(**extra):
    return {"type": "route", "route": "hiking", **extra}


def validate_row(row):
    """The field rules of backend/src/lib/route-import/record.ts."""
    assert set(row) == {
        "source", "ref", "name", "ref_code", "route", "network", "state", "disused", "superroute",
        "geometry", "parts", "max_gap_m", "length_m", "osm_version", "source_date", "tags", "content_hash",
    }
    assert row["source"] in ("osm", "bvv")
    assert isinstance(row["ref"], str) and len(row["ref"]) >= 1
    for key in ("name", "ref_code", "state", "source_date"):
        assert row[key] is None or isinstance(row[key], str)
    assert isinstance(row["route"], str) and row["route"]
    assert row["network"] in ("iwn", "nwn", "rwn", "lwn", None)
    assert isinstance(row["disused"], bool) and isinstance(row["superroute"], bool)
    geometry = row["geometry"]
    assert set(geometry) == {"type", "coordinates"} and geometry["type"] == "LineString"
    assert len(geometry["coordinates"]) >= 2
    for point in geometry["coordinates"]:
        assert 2 <= len(point) <= 3 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in point)
        assert -180 <= point[0] <= 180 and -90 <= point[1] <= 90
    assert isinstance(row["parts"], int) and not isinstance(row["parts"], bool) and row["parts"] >= 0
    for key in ("max_gap_m", "length_m"):
        assert isinstance(row[key], (int, float)) and not isinstance(row[key], bool) and row[key] >= 0
    assert row["osm_version"] is None or (isinstance(row["osm_version"], int) and row["osm_version"] >= 0)
    assert isinstance(row["tags"], dict)
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in row["tags"].items())
    assert re.fullmatch(r"[0-9a-f]{64}", row["content_hash"])


def test_continuous_chain_in_member_order(tmp_path):
    nodes = line_nodes(5)
    rows, counts = run(tmp_path, nodes, {10: [1, 2, 3], 11: [3, 4, 5]}, {100: (hiking(name="Weg"), [("w", 10, ""), ("w", 11, "")])})
    (row,) = rows
    assert row["ref"] == "relation/100"
    assert [p[0] for p in row["geometry"]["coordinates"]] == [10.0, 10.001, 10.002, 10.003, 10.004]
    assert (row["parts"], row["max_gap_m"]) == (1, 0)
    assert row["length_m"] == pytest.approx(4 * 75.2, abs=3)
    assert row["osm_version"] == 3 and row["source_date"] == "2026-11-25"
    assert counts["written"] == 1
    validate_row(row)


def test_reversed_way_is_turned_around(tmp_path):
    nodes = line_nodes(5)
    # way 11 is stored against the walking direction: 5 -> 3
    rows, _ = run(tmp_path, nodes, {10: [1, 2, 3], 11: [5, 4, 3]}, {100: (hiking(), [("w", 10, ""), ("w", 11, "")])})
    (row,) = rows
    assert [p[0] for p in row["geometry"]["coordinates"]] == [10.0, 10.001, 10.002, 10.003, 10.004]
    assert (row["parts"], row["max_gap_m"]) == (1, 0)
    validate_row(row)


def test_member_order_does_not_have_to_follow_the_walk(tmp_path):
    nodes = line_nodes(5)
    rows, _ = run(tmp_path, nodes, {10: [1, 2, 3], 11: [3, 4, 5]}, {100: (hiking(), [("w", 11, ""), ("w", 10, "")])})
    (row,) = rows
    lons = [p[0] for p in row["geometry"]["coordinates"]]
    assert lons in ([10.0, 10.001, 10.002, 10.003, 10.004], [10.004, 10.003, 10.002, 10.001, 10.0])
    assert row["parts"] == 1


def test_gap_of_120_m_is_measured_and_bridged(tmp_path):
    nodes = line_nodes(3)
    nodes.update({4: (10.002, round(LAT + GAP_LAT, 7)), 5: (10.003, round(LAT + GAP_LAT, 7))})
    rows, _ = run(tmp_path, nodes, {10: [1, 2, 3], 11: [4, 5]}, {100: (hiking(), [("w", 10, ""), ("w", 11, "")])})
    (row,) = rows
    assert row["parts"] == 2
    assert row["max_gap_m"] == pytest.approx(120, abs=1.5)
    assert len(row["geometry"]["coordinates"]) == 5
    # the bridge is part of the length: 2 * 75 m + 120 m + 75 m
    assert row["length_m"] == pytest.approx(150.4 + 120 + 75.2, abs=3)
    validate_row(row)


def test_first_chain_is_oriented_towards_the_next_one(tmp_path):
    nodes = line_nodes(3)
    nodes.update({4: (10.002, round(LAT + GAP_LAT, 7)), 5: (10.003, round(LAT + GAP_LAT, 7))})
    # way 10 is stored 3 -> 1, away from the other piece; the bridge must still be the short one
    rows, _ = run(tmp_path, nodes, {10: [3, 2, 1], 11: [4, 5]}, {100: (hiking(), [("w", 10, ""), ("w", 11, "")])})
    assert rows[0]["max_gap_m"] == pytest.approx(120, abs=1.5)


def test_round_trip_is_one_part(tmp_path):
    nodes = {1: (10.0, 47.5), 2: (10.001, 47.5), 3: (10.001, 47.501), 4: (10.0, 47.501)}
    ways = {10: [1, 2, 3], 11: [3, 4, 1]}
    rows, _ = run(tmp_path, nodes, ways, {100: (hiking(), [("w", 10, ""), ("w", 11, "")])})
    (row,) = rows
    coords = row["geometry"]["coordinates"]
    assert (row["parts"], row["max_gap_m"]) == (1, 0)
    assert coords[0] == coords[-1] and len(coords) == 5
    validate_row(row)


def test_role_alternative_excursion_and_approach_stay_out_of_the_main_line(tmp_path):
    nodes = line_nodes(3)
    nodes.update({7: (10.0, 47.6), 8: (10.001, 47.6), 9: (10.002, 47.6)})
    ways = {10: [1, 2, 3], 20: [7, 8], 21: [8, 9]}
    members = [("w", 10, "main"), ("w", 20, "alternative"), ("w", 21, "excursion"), ("w", 20, "approach"), ("w", 21, "connection")]
    rows, _ = run(tmp_path, nodes, ways, {100: (hiking(), members)})
    (row,) = rows
    assert [p[1] for p in row["geometry"]["coordinates"]] == [LAT, LAT, LAT]
    assert row["parts"] == 1


def test_relation_with_only_alternative_ways_is_skipped(tmp_path):
    nodes = line_nodes(3)
    rows, counts = run(tmp_path, nodes, {10: [1, 2, 3]}, {100: (hiking(), [("w", 10, "alternative")])})
    assert rows == [] and counts["skipped_no_way_geometry"] == 1


def test_forward_and_backward_roles_are_main_line(tmp_path):
    nodes = line_nodes(5)
    rows, _ = run(tmp_path, nodes, {10: [1, 2, 3], 11: [3, 4, 5]}, {100: (hiking(), [("w", 10, "forward"), ("w", 11, "backward")])})
    assert rows[0]["parts"] == 1 and len(rows[0]["geometry"]["coordinates"]) == 5


def test_superroute_with_own_ways_is_flagged_and_without_ways_skipped(tmp_path):
    nodes = line_nodes(3)
    relations = {
        100: (hiking(), [("w", 10, ""), ("r", 101, "")]),
        101: (hiking(), [("w", 10, "")]),
        102: ({"type": "superroute", "route": "hiking"}, [("r", 101, "")]),
        103: ({"type": "superroute", "route": "hiking"}, [("w", 10, "")]),
    }
    rows, counts = run(tmp_path, nodes, {10: [1, 2, 3]}, relations)
    by_ref = {r["ref"]: r for r in rows}
    assert set(by_ref) == {"relation/100", "relation/101", "relation/103"}
    assert by_ref["relation/100"]["superroute"] is True
    assert by_ref["relation/101"]["superroute"] is False
    assert by_ref["relation/103"]["superroute"] is True
    assert counts["skipped_superroute_without_ways"] == 1 and counts["written_superroute"] == 2
    for row in rows:
        validate_row(row)


def test_only_hiking_and_foot_are_exported(tmp_path):
    nodes = line_nodes(3)
    ways = {10: [1, 2, 3]}
    relations = {
        100: ({"type": "route", "route": "bicycle"}, [("w", 10, "")]),
        101: ({"type": "route", "route": "foot"}, [("w", 10, "")]),
        102: ({"type": "route", "route": "mtb"}, [("w", 10, "")]),
        103: (hiking(), [("w", 10, "")]),
        104: ({"type": "route"}, [("w", 10, "")]),
    }
    rows, counts = run(tmp_path, nodes, ways, relations)
    assert [r["ref"] for r in rows] == ["relation/101", "relation/103"]
    assert [r["route"] for r in rows] == ["foot", "hiking"]
    assert counts["relations_selected"] == 2


def test_name_de_comes_before_name_and_blank_names_are_null(tmp_path):
    nodes = line_nodes(3)
    ways = {10: [1, 2, 3]}
    relations = {
        100: (hiking(**{"name": "Lech Trail", "name:de": "Lechweg"}), [("w", 10, "")]),
        101: (hiking(name="Nur Name"), [("w", 10, "")]),
        102: (hiking(**{"name:de": "  ", "name": "Zweiter"}), [("w", 10, "")]),
        103: (hiking(), [("w", 10, "")]),
    }
    rows, _ = run(tmp_path, nodes, ways, relations)
    assert [r["name"] for r in rows] == ["Lechweg", "Nur Name", "Zweiter", None]


def test_network_state_disused_and_ref_code(tmp_path):
    nodes = line_nodes(3)
    ways = {10: [1, 2, 3]}
    relations = {
        100: (hiking(network="rwn;nwn", ref="E4", state="proposed"), [("w", 10, "")]),
        101: (hiking(network="lwn", **{"disused": "yes"}), [("w", 10, "")]),
        102: (hiking(network="something", state="disused"), [("w", 10, "")]),
    }
    rows, _ = run(tmp_path, nodes, ways, relations)
    assert (rows[0]["network"], rows[0]["ref_code"], rows[0]["state"], rows[0]["disused"]) == ("nwn", "E4", "proposed", False)
    assert (rows[1]["network"], rows[1]["disused"]) == ("lwn", True)
    assert (rows[2]["network"], rows[2]["state"], rows[2]["disused"]) == (None, "disused", True)
    for row in rows:
        validate_row(row)


def test_all_tags_are_kept_and_unicode_survives(tmp_path):
    nodes = line_nodes(3)
    rows, _ = run(tmp_path, nodes, {10: [1, 2, 3]}, {100: (hiking(**{"name": "Höhenweg", "osmc:symbol": "red:white:red_bar"}), [("w", 10, "")])})
    assert rows[0]["tags"] == {"type": "route", "route": "hiking", "name": "Höhenweg", "osmc:symbol": "red:white:red_bar"}


def test_output_is_sorted_by_relation_id_and_byte_identical(tmp_path):
    nodes = line_nodes(3)
    relations = {300: (hiking(), [("w", 10, "")]), 100: (hiking(), [("w", 10, "")]), 200: (hiking(), [("w", 10, "")])}
    rows, _ = run(tmp_path, nodes, {10: [1, 2, 3]}, relations)
    assert [r["ref"] for r in rows] == ["relation/100", "relation/200", "relation/300"]
    first = (tmp_path / "out.ndjson.gz").read_bytes()
    run(tmp_path, nodes, {10: [1, 2, 3]}, relations)
    assert (tmp_path / "out.ndjson.gz").read_bytes() == first


def test_content_hash_is_stable_and_follows_geometry_and_tags(tmp_path):
    nodes = line_nodes(3)
    relations = {100: (hiking(name="A"), [("w", 10, "")])}
    a, _ = run(tmp_path, nodes, {10: [1, 2, 3]}, relations, source_date="2026-11-25", name="a.osm")
    b, _ = run(tmp_path, nodes, {10: [1, 2, 3]}, relations, source_date="2026-12-31", name="b.osm")
    assert a[0]["content_hash"] == b[0]["content_hash"]  # source_date is not part of the hash
    moved = dict(nodes)
    moved[2] = (10.001, round(LAT + 0.0001, 7))
    c, _ = run(tmp_path, moved, {10: [1, 2, 3]}, relations, name="c.osm")
    assert c[0]["content_hash"] != a[0]["content_hash"]
    d, _ = run(tmp_path, nodes, {10: [1, 2, 3]}, {100: (hiking(name="B"), [("w", 10, "")])}, name="d.osm")
    assert d[0]["content_hash"] != a[0]["content_hash"]
    # documented canonical form
    import hashlib
    canonical = json.dumps({"geometry": a[0]["geometry"], "tags": a[0]["tags"]}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert a[0]["content_hash"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_way_with_missing_node_is_left_out_and_counted(tmp_path):
    nodes = line_nodes(3)
    # way 11 references node 99 which is not in the file
    rows, counts = run(tmp_path, nodes, {10: [1, 2, 3], 11: [3, 99]}, {100: (hiking(), [("w", 10, ""), ("w", 11, "")])})
    assert len(rows) == 1 and rows[0]["parts"] == 1
    assert counts["ways_missing_nodes"] == 1


def test_haversine_matches_a_known_distance():
    # one degree of latitude is about 111.2 km
    assert ex.haversine_m((10.0, 47.0), (10.0, 48.0)) == pytest.approx(111195, rel=1e-3)
    assert ex.haversine_m((10.0, 47.0), (10.0, 47.0)) == 0
    assert math.isclose(ex.haversine_m((0, 0), (1, 0)), ex.haversine_m((1, 0), (0, 0)))
