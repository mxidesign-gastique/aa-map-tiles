package net.allactive.routes;

import com.onthegomap.planetiler.FeatureCollector;
import com.onthegomap.planetiler.FeatureMerge;
import com.onthegomap.planetiler.Planetiler;
import com.onthegomap.planetiler.Profile;
import com.onthegomap.planetiler.VectorTile;
import com.onthegomap.planetiler.config.Arguments;
import com.onthegomap.planetiler.geo.GeometryException;
import com.onthegomap.planetiler.reader.SourceFeature;
import com.onthegomap.planetiler.reader.osm.OsmElement;
import com.onthegomap.planetiler.reader.osm.OsmRelationInfo;
import java.nio.file.Path;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Overlay tile set "routes": OSM route relations laid onto their member ways (layer {@code route})
 * plus single ways with a difficulty grading (layer {@code way}).
 * <p>
 * Schema: see the "routes" section of the repository README. Layer {@code route} carries per way
 * and route type only the relation with the best (smallest) rank, the others count in
 * {@code route_count}. Zoom range of the set: 6 to 14.
 * <p>
 * Run: {@code java -Xmx8g -jar target/aa-routes-1.0-with-deps.jar --osm_path=in.osm.pbf
 * --output=routes.pmtiles}
 */
public class RoutesOverlay implements Profile {

  static final String LAYER_ROUTE = "route";
  static final String LAYER_WAY = "way";

  private static final Set<String> PISTE_TYPES = Set.of("downhill", "nordic", "skitour", "hike", "sled");
  private static final Set<String> PISTE_DIFFICULTIES =
    Set.of("novice", "easy", "intermediate", "advanced", "expert", "freeride", "extreme");
  private static final Pattern NUMBER = Pattern.compile("^\\s*(\\d+(?:[.,]\\d+)?)\\s*(km|mi|m)?\\s*$",
    Pattern.CASE_INSENSITIVE);

  /**
   * What the way pass needs from a route relation. Held in RAM for every matching relation, so keep
   * it small; all strings except name/ref/osmcSymbol/colour are constants.
   */
  record RouteRelation(
    @Override long id,
    String type,
    String network,
    int rank,
    String name,
    String ref,
    String osmcSymbol,
    String colour,
    Double distance,
    String pisteType
  ) implements OsmRelationInfo {

    @Override
    public long estimateMemoryUsageBytes() {
      // object header + fields + rough size of the strings (UTF-16 worst case)
      long size = 96;
      for (String s : new String[] {name, ref, osmcSymbol, colour}) {
        if (s != null) {
          size += 40 + 2L * s.length();
        }
      }
      return size;
    }
  }

  // ---- Step 1: remember the route relations --------------------------------------------------

  @Override
  public List<OsmRelationInfo> preprocessOsmRelation(OsmElement.Relation relation) {
    if (!relation.hasTag("type", "route")) {
      return null;
    }
    String route = relation.getString("route");
    if (route == null) {
      return null;
    }
    String type;
    String pisteType = null;
    switch (route) {
      case "hiking", "foot" -> type = "hiking";
      case "bicycle" -> type = "cycling";
      case "mtb" -> type = "mtb";
      case "horse" -> type = "horse";
      case "ski", "piste" -> {
        type = "winter";
        pisteType = pisteType(relation.getString("piste:type"));
        if (pisteType == null && "ski".equals(route)) {
          // route=ski without piste:type is a cross-country route in practice.
          pisteType = "nordic";
        }
        if (pisteType == null) {
          return null; // route=piste needs a known piste:type
        }
      }
      default -> {
        return null;
      }
    }
    String network = network(relation.getString("network"));
    return List.of(new RouteRelation(
      relation.id(),
      type,
      network,
      rank(network),
      firstNonBlank(relation.getString("name:de"), relation.getString("name")),
      blankToNull(relation.getString("ref")),
      blankToNull(relation.getString("osmc:symbol")),
      blankToNull(relation.getString("colour")),
      distanceKm(relation.getString("distance")),
      pisteType
    ));
  }

  // ---- Step 2: emit the ways --------------------------------------------------------------------

  @Override
  public void processFeature(SourceFeature sourceFeature, FeatureCollector features) {
    if (!sourceFeature.canBeLine()) {
      return;
    }
    emitRoutes(sourceFeature, features);
    emitWay(sourceFeature, features);
  }

  private static void emitRoutes(SourceFeature sourceFeature, FeatureCollector features) {
    var memberships = sourceFeature.relationInfo(RouteRelation.class, true);
    if (memberships.isEmpty()) {
      return;
    }
    // best relation and number of distinct relations per route type
    Map<String, RouteRelation> best = new LinkedHashMap<>();
    Map<String, Set<Long>> ids = new LinkedHashMap<>();
    for (var membership : memberships) {
      RouteRelation relation = membership.relation();
      ids.computeIfAbsent(relation.type(), k -> new HashSet<>()).add(relation.id());
      RouteRelation current = best.get(relation.type());
      if (current == null || isBetter(relation, current)) {
        best.put(relation.type(), relation);
      }
    }
    for (var entry : best.entrySet()) {
      RouteRelation relation = entry.getValue();
      int count = ids.get(entry.getKey()).size();
      var feature = features.line(LAYER_ROUTE)
        .setAttr("type", relation.type())
        .setAttr("network", relation.network())
        .setAttr("rank", relation.rank())
        .setAttr("name", relation.name())
        .setAttr("ref", relation.ref())
        .setAttr("osmc_symbol", relation.osmcSymbol())
        .setAttr("colour", relation.colour())
        .setAttr("distance", relation.distance())
        .setAttr("piste_type", relation.pisteType())
        .setAttr("osm_id", relation.id())
        .setMinZoom(routeMinZoom(relation))
        // keep short segments, the merge step in postProcessLayerFeatures needs them
        .setMinPixelSize(0);
      if (count >= 2) {
        feature.setAttr("route_count", count);
      }
    }
  }

  private static void emitWay(SourceFeature sourceFeature, FeatureCollector features) {
    Integer sac = sac(sourceFeature.getString("sac_scale"));
    Integer mtb = mtbScale(sourceFeature.getString("mtb:scale"));
    String pisteType = pisteType(sourceFeature.getString("piste:type"));
    if (sac == null && mtb == null && pisteType == null) {
      return;
    }
    String difficulty = sourceFeature.getString("piste:difficulty");
    if (difficulty != null && !PISTE_DIFFICULTIES.contains(difficulty)) {
      difficulty = null;
    }
    features.line(LAYER_WAY)
      .setAttr("sac", sac)
      .setAttr("mtb", mtb)
      .setAttr("piste_type", pisteType)
      .setAttr("piste_difficulty", pisteType == null ? null : difficulty)
      .setAttr("piste_grooming", pisteType == null ? null : blankToNull(sourceFeature.getString("piste:grooming")))
      .setAttr("trail_visibility", blankToNull(sourceFeature.getString("trail_visibility")))
      .setAttr("name", firstNonBlank(sourceFeature.getString("name:de"), sourceFeature.getString("name")))
      .setAttr("osm_id", sourceFeature.id())
      .setMinZoom(pisteType != null ? 10 : 11)
      .setMinPixelSize(0);
  }

  // ---- Step 3: merge touching lines with equal attributes ------------------------------------

  @Override
  public List<VectorTile.Feature> postProcessLayerFeatures(String layer, int zoom,
    List<VectorTile.Feature> items) throws GeometryException {
    if (LAYER_ROUTE.equals(layer)) {
      // Same attributes include osm_id, so only segments of one relation are joined.
      return FeatureMerge.mergeLineStrings(items,
        0.5, // drop lines still shorter than 0.5 px after merging
        0.1, // simplification tolerance in px
        4 // drop detail more than 4 px outside the tile
      );
    }
    return items; // way: attributes include the way id, nothing to join
  }

  // ---- Metadata ---------------------------------------------------------------------------------

  @Override
  public String name() {
    return "All-Active routes";
  }

  @Override
  public String description() {
    return "Hiking, cycling, mtb, horse and winter route networks plus graded ways, from OpenStreetMap";
  }

  @Override
  public String attribution() {
    return "© OpenStreetMap contributors (ODbL)";
  }

  @Override
  public String version() {
    return "1";
  }

  @Override
  public boolean isOverlay() {
    return true;
  }

  // ---- Helpers (pure, package-private for tests) ---------------------------------------------

  /** international, national, regional or local; for "iwn;nwn" style lists the best one wins. */
  static String network(String raw) {
    if (raw == null) {
      return "local";
    }
    String best = "local";
    for (String part : raw.split(";")) {
      String candidate = switch (part.trim().toLowerCase()) {
        case "iwn", "icn" -> "international";
        case "nwn", "ncn" -> "national";
        case "rwn", "rcn" -> "regional";
        default -> "local";
      };
      if (rank(candidate) < rank(best)) {
        best = candidate;
      }
    }
    return best;
  }

  static int rank(String network) {
    return switch (network) {
      case "international" -> 1;
      case "national" -> 2;
      case "regional" -> 3;
      default -> 4;
    };
  }

  /** rank 1-2 from z6, rank 3 from z9, rank 4 from z11, winter sports from z10. */
  static int routeMinZoom(RouteRelation relation) {
    if ("winter".equals(relation.type())) {
      return 10;
    }
    return switch (relation.rank()) {
      case 1, 2 -> 6;
      case 3 -> 9;
      default -> 11;
    };
  }

  /** Smaller rank wins; then a named route, then the lowest relation id (deterministic). */
  static boolean isBetter(RouteRelation candidate, RouteRelation current) {
    if (candidate.rank() != current.rank()) {
      return candidate.rank() < current.rank();
    }
    boolean candidateNamed = candidate.name() != null || candidate.ref() != null;
    boolean currentNamed = current.name() != null || current.ref() != null;
    if (candidateNamed != currentNamed) {
      return candidateNamed;
    }
    return candidate.id() < current.id();
  }

  static Integer sac(String value) {
    if (value == null) {
      return null;
    }
    return switch (value.trim()) {
      case "hiking" -> 1;
      case "mountain_hiking" -> 2;
      case "demanding_mountain_hiking" -> 3;
      case "alpine_hiking" -> 4;
      case "demanding_alpine_hiking" -> 5;
      case "difficult_alpine_hiking" -> 6;
      default -> null;
    };
  }

  /** Leading digit of mtb:scale ("2", "1+", "0-"), 0 to 6. */
  static Integer mtbScale(String value) {
    if (value == null) {
      return null;
    }
    String trimmed = value.trim();
    if (!trimmed.isEmpty() && trimmed.charAt(0) >= '0' && trimmed.charAt(0) <= '6') {
      return trimmed.charAt(0) - '0';
    }
    return null;
  }

  static String pisteType(String value) {
    return value != null && PISTE_TYPES.contains(value.trim()) ? value.trim() : null;
  }

  /** Kilometres; plain numbers count as km, "m" and "mi" are converted. Null if unusable. */
  static Double distanceKm(String value) {
    if (value == null) {
      return null;
    }
    Matcher matcher = NUMBER.matcher(value);
    if (!matcher.matches()) {
      return null;
    }
    double number = Double.parseDouble(matcher.group(1).replace(',', '.'));
    String unit = matcher.group(2) == null ? "km" : matcher.group(2).toLowerCase();
    double km = switch (unit) {
      case "m" -> number / 1000;
      case "mi" -> number * 1.609344;
      default -> number;
    };
    if (!(km > 0) || km > 20000) {
      return null;
    }
    return Math.round(km * 10) / 10.0;
  }

  private static String blankToNull(String value) {
    return value == null || value.isBlank() ? null : value.trim();
  }

  private static String firstNonBlank(String first, String second) {
    String value = blankToNull(first);
    return value != null ? value : blankToNull(second);
  }

  // ---- Main -------------------------------------------------------------------------------------

  public static void main(String[] args) throws Exception {
    run(Arguments.fromArgsOrConfigFile(args)
      // defaults of this tile set; command line arguments win
      .orElse(Arguments.of("minzoom", "6", "maxzoom", "14")));
  }

  static void run(Arguments args) {
    Planetiler.create(args)
      .setProfile(new RoutesOverlay())
      // override with --osm_path=path/to/filtered.osm.pbf
      .addOsmSource("osm", Path.of("data", "sources", "routes.osm.pbf"))
      // override with --output=path/to/routes.pmtiles
      .overwriteOutput(Path.of("data", "routes.pmtiles"))
      .run();
  }
}
