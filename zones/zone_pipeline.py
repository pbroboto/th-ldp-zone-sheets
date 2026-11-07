"""
Build one TH-LDP zone's one-page sheet end to end: clip DEM/geoid, compute the
combined-factor distortion raster, drape it over a hillshade, pull OSM roads/
places/AOI, assemble the QGIS Print Layout, export PDF+PNG.

Usage (run with QGIS's own python, e.g. via python-qgis.bat):
    python zone_pipeline.py "Phrae"
    python zone_pipeline.py "Phrae" --skip-osm     # reuse cached OSM files
    python zone_pipeline.py "Phrae" --osm-only     # just refresh OSM

One zone's working files live under zones/<slug>/, the final PDF/PNG land in
output/<Name>_TH-LDP.{pdf,png}.
"""
import os, re, sys, csv, json, time, math, argparse, urllib.request, urllib.parse
QGIS_HOME = os.environ.get("QGIS_HOME", r"C:\Program Files\QGIS 4.2.1")
os.environ.setdefault("GDAL_DATA", QGIS_HOME + r"\share\gdal")

# Every location below can be moved with an environment variable (see README).
ROOT = os.environ.get("TH_LDP_ROOT", r"D:\dev\One Page TH-LDP")
OVERRIDES = ROOT + r"\zones\zone_overrides.tsv"
TSV = os.environ.get("TH_LDP_TSV", OVERRIDES)  # base definitions; the overrides file holds all 90 zones
GADM = ROOT + r"\Thailand Admins\gadm41_THA.gpkg"
DEM = ROOT + r"\DEM\TH_FABDEM.tif"
GTX = ROOT + r"\TGM2017\tgm2017.gtx"
THAILAND_OUTLINE = ROOT + r"\work\thailand_outline.gpkg"  # built once, shared
ZONES_DIR = ROOT + r"\zones"
OUTPUT_DIR = ROOT + r"\output"
QGIS_PREFIX = QGIS_HOME + r"\apps\qgis"

PAGE_W, PAGE_H = 210.0, 297.0  # A4 portrait, mm (the formal size in Thailand)
MAP_X, MAP_Y = 10, 31
MAP_W = PAGE_W - 20
MAP_H_MAX = 150  # raster is built to this aspect
MAP_H_VIEW = 150  # tallest map box on the page; the raster is centre-cropped to fit
TARGET_ASPECT = MAP_W / MAP_H_MAX
RES = 60.0  # dem/raster resolution, metres

os.makedirs(ZONES_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

PROVINCE_ALIASES = ("Ayutthaya", "Phang Nga", "Sisaket", "Bangkok")  # capital names that differ from NAME_1


def _norm_name(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def province_capital_names():
    """Normalised province names (zone names minus a North/South/... suffix) plus the aliases."""
    names = set(map(_norm_name, PROVINCE_ALIASES))
    with open(OVERRIDES, encoding="utf-8", newline="") as f:
        for line in f.read().split("\n"):
            if line.strip():
                base = re.sub(r"\s+(North|South|Central|East|West)$", "", line.split("\t")[0])
                names.add(_norm_name(base))
    return names


def slugify(name):
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")


HEADER_CSV = ROOT + r"\Province LDP Definition\ProvinceLDP.csv"  # committee file, not in the repo


def zone_header(code):
    """ISO_1, HASC_1, NAME_1 and POP coverage for the sheet header, from the committee's CSV."""
    with open(HEADER_CSV, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row["HASC_1"] == code:
                return row
    return None


def load_zone(zone_name):
    # th_ldp.tsv has a stray \r embedded mid-record (before the WKT field) on
    # every row -- open with newline="" to stop universal-newline translation
    # from splitting each record into two "lines", and strip() each field.
    # zone_overrides.tsv (same columns, WKT optional) wins over th_ldp.tsv, for
    # definitions the committee has issued that are not yet in the main table
    # Override rows may also carry the committee's design values in columns
    # 5-7 (hPP m, MSL m, CSF tolerance ppm); th_ldp.tsv column 5 is WKT, so
    # design values are only read from the override file.
    content = ""
    design = {}
    if os.path.exists(OVERRIDES):
        with open(OVERRIDES, encoding="utf-8", newline="") as f:
            ov = f.read()
        content += ov + "\n"
        for line in ov.split("\n"):
            r = [c.strip() for c in line.split("\t")]
            if len(r) >= 7:
                design[r[0]] = (float(r[4]), float(r[5]), float(r[6]))
    with open(TSV, encoding="utf-8", newline="") as f:
        content += f.read()
    for line in content.split("\n"):
        if not line.strip():
            continue
        row = [c.strip() for c in line.split("\t")]
        if row[0] == zone_name:
            name, code, kind, proj4 = row[0], row[1], row[2], row[3]
            params = dict(p.split("=") for p in proj4.split() if "=" in p)
            hpp, msl, csf = design.get(name, (None, None, None))
            return {
                "name": name, "code": code, "kind": kind, "proj4": proj4,
                "lon_0": float(params["+lon_0"]), "k0": float(params["+k_0"]),
                "x0": float(params["+x_0"]), "y0": float(params["+y_0"]),
                "lat_0": float(params.get("+lat_0", params.get("+lat_1", 0.0))),
                "slug": slugify(name),
                "hpp": hpp, "msl": msl, "csf": csf,
            }
    raise KeyError(f"zone {zone_name!r} not found in {OVERRIDES} or {TSV}")


def extract_boundary(zone, zdir):
    """Clip the zone's own province/sub-zone polygon out of the GADM layer.
    Returns (minx, miny, maxx, maxy) in WGS84, or None if no matching NAME_1."""
    from osgeo import ogr
    ogr.UseExceptions()
    src = ogr.Open(GADM)
    lyr = src.GetLayerByName("ADM_ADM_1")
    lyr.SetAttributeFilter(f"NAME_1 = '{zone['name']}'")
    feats = list(lyr)
    if not feats:
        return None
    drv = ogr.GetDriverByName("GPKG")
    out_path = zdir + r"\boundary_wgs84.gpkg"
    if os.path.exists(out_path):
        drv.DeleteDataSource(out_path)
    ds = drv.CreateDataSource(out_path)
    out_lyr = ds.CreateLayer("boundary", lyr.GetSpatialRef(), ogr.wkbMultiPolygon)
    out_lyr.CreateFields(lyr.schema)
    minx = miny = 1e18
    maxx = maxy = -1e18
    for f in feats:
        out_lyr.CreateFeature(f.Clone())
        g = f.GetGeometryRef()
        e = g.GetEnvelope()  # minx, maxx, miny, maxy
        minx, maxx = min(minx, e[0]), max(maxx, e[1])
        miny, maxy = min(miny, e[2]), max(maxy, e[3])
    ds = None
    return (minx, miny, maxx, maxy)


# FABDEM's own WGS84 coverage -- never request data outside this or the
# warp fills the gap with blank nodata (seen as a white rectangle on the page)
DEM_BOUNDS = (96.999861, 5.000139, 105.999861, 21.000139)


def buffered_bbox(bbox, margin=0.15):
    minx, miny, maxx, maxy = bbox
    mx, my = (maxx - minx) * margin, (maxy - miny) * margin
    minx, maxx = minx - mx, maxx + mx
    miny, maxy = miny - my, maxy + my
    # widen east-west to match the page's own map-box aspect ratio so the
    # rendered raster fills the frame with no blank letterbox strips
    center_lat = (miny + maxy) / 2.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(center_lat))
    m_per_deg_lat = 111320.0
    height_m = (maxy - miny) * m_per_deg_lat
    width_m_needed = TARGET_ASPECT * height_m
    width_deg_needed = width_m_needed / m_per_deg_lon
    if width_deg_needed > (maxx - minx):
        center_lon = (minx + maxx) / 2.0
        minx, maxx = center_lon - width_deg_needed / 2.0, center_lon + width_deg_needed / 2.0
    dminx, dminy, dmaxx, dmaxy = DEM_BOUNDS
    minx, maxx = max(minx, dminx), min(maxx, dmaxx)
    miny, maxy = max(miny, dminy), min(maxy, dmaxy)
    return (minx, miny, maxx, maxy)


def build_raster(zone, zdir, bbox):
    """Clip FABDEM+TGM2017, compute the combined scale-factor distortion
    raster (exact point scale from PROJ's conformal factors x elevation
    factor using TGM2017 geoid height), hillshade, and the classified+shaded
    RGBA render. Returns the QgsRectangle-like tuple extent in the LDP CRS."""
    import numpy as np
    from osgeo import gdal
    import pyproj
    gdal.UseExceptions()

    proj4 = zone["proj4"]
    minx, miny, maxx, maxy = bbox

    dem_ldp = zdir + r"\dem_ldp.tif"
    gdal.Warp(dem_ldp, DEM, dstSRS=proj4, outputBoundsSRS="EPSG:4326",
              outputBounds=(minx, miny, maxx, maxy), xRes=RES, yRes=RES,
              resampleAlg="bilinear", dstNodata=-9999)

    ds = gdal.Open(dem_ldp)
    gt = ds.GetGeoTransform()
    nx, ny = ds.RasterXSize, ds.RasterYSize
    dem = ds.GetRasterBand(1).ReadAsArray().astype(np.float64)

    ulx, uly = gt[0], gt[3]
    lrx, lry = gt[0] + nx * gt[1], gt[3] + ny * gt[5]

    geoid_ldp = zdir + r"\geoid_ldp.tif"
    gdal.Warp(geoid_ldp, GTX, dstSRS=proj4, outputBounds=(ulx, lry, lrx, uly),
              width=nx, height=ny, resampleAlg="bilinear")
    gds = gdal.Open(geoid_ldp)
    geoid = gds.GetRasterBand(1).ReadAsArray().astype(np.float64)

    xs = gt[0] + (np.arange(nx) + 0.5) * gt[1]
    ys = gt[3] + (np.arange(ny) + 0.5) * gt[5]
    xx, yy = np.meshgrid(xs, ys)
    to_geo = pyproj.Transformer.from_crs(pyproj.CRS.from_proj4(proj4), "EPSG:4326", always_xy=True)
    lon, lat = to_geo.transform(xx, yy)

    p = pyproj.Proj(proj4)
    factors = p.get_factors(lon, lat, radians=False)
    k_grid = np.array(factors.meridional_scale)

    R = 6371008.7714
    h_ellipsoid = dem + geoid
    k_elev = R / (R + h_ellipsoid)
    combined = k_grid * k_elev
    ppm = (combined - 1.0) * 1.0e6

    valid = dem > -999
    ppm_out = np.where(valid, ppm, -32768)

    ppm_path = zdir + r"\distortion_ppm.tif"
    drv = gdal.GetDriverByName("GTiff")
    out = drv.Create(ppm_path, nx, ny, 1, gdal.GDT_Float32, options=["COMPRESS=DEFLATE", "PREDICTOR=2"])
    out.SetGeoTransform(gt)
    out.SetProjection(ds.GetProjection())
    band = out.GetRasterBand(1)
    band.SetNoDataValue(-32768)
    band.WriteArray(ppm_out.astype(np.float32))
    band.FlushCache()
    out = None
    print(f"  [{zone['name']}] ppm range: {ppm[valid].min():.1f} .. {ppm[valid].max():.1f}")

    hillshade_path = zdir + r"\hillshade.tif"
    gdal.DEMProcessing(hillshade_path, dem_ldp, "hillshade", zFactor=1.5, azimuth=315, altitude=45,
                        computeEdges=True)

    # classify ppm into the 7-bin legend, draped (multiplied) over the hillshade
    BINS = [
        (-1e9, -50, (18, 62, 150)), (-50, -40, (217, 95, 14)), (-40, -30, (253, 141, 60)),
        (-30, -20, (254, 217, 118)), (-20, -10, (173, 221, 142)), (-10, 10, (65, 171, 93)),
        (10, 20, (173, 221, 142)), (20, 30, (254, 217, 118)), (30, 40, (253, 141, 60)),
        (40, 50, (217, 95, 14)), (50, 1e9, (203, 24, 29)),
    ]
    rgb = np.zeros((ny, nx, 3), dtype=np.float64)
    for lo, hi, col in BINS:
        m = (ppm >= lo) & (ppm < hi) & valid
        for c in range(3):
            rgb[..., c][m] = col[c]
    hs_ds = gdal.Open(hillshade_path)
    hs = hs_ds.GetRasterBand(1).ReadAsArray().astype(np.float64)
    hs_boost = 0.40 + 0.60 * (hs / 255.0)
    shaded = np.clip(rgb * hs_boost[..., None], 0, 255)
    alpha = np.where(valid, 255, 0).astype(np.uint8)

    render_path = zdir + r"\zone_render.tif"
    out = drv.Create(render_path, nx, ny, 4, gdal.GDT_Byte,
                      options=["COMPRESS=DEFLATE", "PHOTOMETRIC=RGB", "ALPHA=YES"])
    out.SetGeoTransform(gt)
    out.SetProjection(ds.GetProjection())
    for c in range(3):
        out.GetRasterBand(c + 1).WriteArray(shaded[..., c].astype(np.uint8))
    out.GetRasterBand(4).WriteArray(alpha)
    out.FlushCache()
    out = None

    # zero-distortion contour (literal zero-crossing of the combined factor),
    # simplified and short fragments dropped for legibility
    from osgeo import ogr, osr
    srs = osr.SpatialReference()
    srs.ImportFromWkt(ds.GetProjection())
    zero_clean = zdir + r"\zero_line_clean.gpkg"
    drv_gpkg = ogr.GetDriverByName("GPKG")
    if os.path.exists(zero_clean):
        drv_gpkg.DeleteDataSource(zero_clean)
    zout = drv_gpkg.CreateDataSource(zero_clean)
    zlyr = zout.CreateLayer("contour", srs, ogr.wkbLineString)
    zlyr.CreateField(ogr.FieldDefn("ppm", ogr.OFTReal))

    mem_drv = ogr.GetDriverByName("Memory")
    mem_ds = mem_drv.CreateDataSource("contours")
    mem_lyr = mem_ds.CreateLayer("contour", srs, ogr.wkbLineString)
    mem_lyr.CreateField(ogr.FieldDefn("ID", ogr.OFTInteger))
    mem_lyr.CreateField(ogr.FieldDefn("elev", ogr.OFTReal))
    ppm_ds = gdal.Open(ppm_path)  # band's own dataset was already closed above
    ppm_band = ppm_ds.GetRasterBand(1)
    gdal.ContourGenerate(ppm_band, 0, 0, [0.0], 0, -32768, mem_lyr, 0, 1)
    ppm_ds = None
    for f in mem_lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        g2 = g.SimplifyPreserveTopology(400)
        if g2 is None or g2.Length() <= 800:
            continue
        nf = ogr.Feature(zlyr.GetLayerDefn())
        nf.SetGeometry(g2)
        nf.SetField("ppm", 0.0)
        zlyr.CreateFeature(nf)
    zout = None

    return render_path


def reproject_gpkg(src_path, src_layer, dst_path, dst_layer, dst_srs_wkt_or_proj4, where=None):
    from osgeo import ogr, osr
    src = ogr.Open(src_path)
    lyr = src.GetLayerByName(src_layer)
    if where:
        lyr.SetAttributeFilter(where)
    src_srs = lyr.GetSpatialRef()
    dst_srs = osr.SpatialReference()
    if dst_srs_wkt_or_proj4.strip().startswith("+"):
        dst_srs.ImportFromProj4(dst_srs_wkt_or_proj4)
    else:
        dst_srs.ImportFromEPSG(int(dst_srs_wkt_or_proj4))
    xform = osr.CoordinateTransformation(src_srs, dst_srs)

    drv = ogr.GetDriverByName("GPKG")
    if os.path.exists(dst_path):
        drv.DeleteDataSource(dst_path)
    ds = drv.CreateDataSource(dst_path)
    out_lyr = ds.CreateLayer(dst_layer, dst_srs, lyr.GetGeomType())
    out_lyr.CreateFields(lyr.schema)
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        g2 = g.Clone()
        g2.Transform(xform)
        nf = ogr.Feature(out_lyr.GetLayerDefn())
        nf.SetFrom(f)
        nf.SetGeometry(g2)
        out_lyr.CreateFeature(nf)
    ds = None


def build_vectors(zone, zdir):
    proj4 = zone["proj4"]
    reproject_gpkg(zdir + r"\boundary_wgs84.gpkg", "boundary", zdir + r"\boundary_ldp.gpkg", "boundary", proj4)
    reproject_gpkg(GADM, "ADM_ADM_1", zdir + r"\provinces_ldp.gpkg", "provinces", proj4)
    reproject_gpkg(zdir + r"\boundary_wgs84.gpkg", "boundary", zdir + r"\boundary_utm.gpkg", "boundary", "32647")


def fetch_osm(zone, zdir, bbox, retries=6):
    minx, miny, maxx, maxy = bbox
    bbox_s = f"{miny},{minx},{maxy},{maxx}"
    hw_query = f"""[out:json][timeout:90];
(
  way["highway"~"^(motorway|trunk|primary)$"]({bbox_s});
  way["highway"~"^(motorway_link|trunk_link|primary_link)$"]({bbox_s});
);
out geom;"""
    poi_query = f"""[out:json][timeout:90];
(
  node["place"~"^(city|town)$"]({bbox_s});
  node["aeroway"="aerodrome"]({bbox_s});
  way["aeroway"="aerodrome"]({bbox_s});
  way["leisure"="nature_reserve"]({bbox_s});
  way["boundary"="protected_area"]({bbox_s});
  way["boundary"="national_park"]({bbox_s});
);
out geom;
relation["aeroway"="aerodrome"]({bbox_s});
out center tags;"""

    def run(q, out_path):
        data = urllib.parse.urlencode({"data": q}).encode()
        wait = 5
        for attempt in range(retries):
            req = urllib.request.Request("https://overpass-api.de/api/interpreter", data=data,
                headers={"User-Agent": "TH-LDP-atlas/1.0 (riabroy@gmail.com)"})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    js = json.loads(r.read())
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(js, f)
                return js
            except Exception as e:
                print(f"    overpass attempt {attempt+1} failed: {e}; retrying in {wait}s")
                time.sleep(wait)
                wait = min(wait * 2, 60)
        raise RuntimeError(f"overpass failed for {out_path}")

    hw_json = zdir + r"\osm_highways.json"
    poi_json = zdir + r"\osm_poi.json"
    run(hw_query, hw_json)
    time.sleep(3)
    run(poi_query, poi_json)
    return hw_json, poi_json


def osm_to_gpkg(zone, zdir, hw_json, poi_json):
    from osgeo import ogr, osr
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    HWRANK = {"motorway": 1, "trunk": 2, "primary": 3,
              "motorway_link": 1, "trunk_link": 2, "primary_link": 3}

    js = json.load(open(hw_json, encoding="utf-8"))
    hw_path = zdir + r"\osm_highways.gpkg"
    drv = ogr.GetDriverByName("GPKG")
    if os.path.exists(hw_path):
        drv.DeleteDataSource(hw_path)
    ds = drv.CreateDataSource(hw_path)
    lyr = ds.CreateLayer("highways", srs, ogr.wkbLineString)
    for fname in ("name", "ref", "highway"):
        lyr.CreateField(ogr.FieldDefn(fname, ogr.OFTString))
    lyr.CreateField(ogr.FieldDefn("rank", ogr.OFTInteger))
    for el in js["elements"]:
        if el.get("type") != "way" or "geometry" not in el:
            continue
        tags = el.get("tags", {})
        hw = tags.get("highway", "")
        geom = ogr.Geometry(ogr.wkbLineString)
        for pt in el["geometry"]:
            geom.AddPoint(pt["lon"], pt["lat"])
        feat = ogr.Feature(lyr.GetLayerDefn())
        feat.SetGeometry(geom)
        feat.SetField("name", tags.get("name", "") or tags.get("name:en", ""))
        feat.SetField("ref", tags.get("ref", ""))
        feat.SetField("highway", hw)
        feat.SetField("rank", HWRANK.get(hw, 9))
        lyr.CreateFeature(feat)
    ds = None

    js = json.load(open(poi_json, encoding="utf-8"))
    poi_path = zdir + r"\osm_poi.gpkg"
    if os.path.exists(poi_path):
        drv.DeleteDataSource(poi_path)
    ds = drv.CreateDataSource(poi_path)
    pt_lyr = ds.CreateLayer("poi_points", srs, ogr.wkbPoint)
    for fname in ("name", "kind"):
        pt_lyr.CreateField(ogr.FieldDefn(fname, ogr.OFTString))
    poly_lyr = ds.CreateLayer("aoi_polygons", srs, ogr.wkbPolygon)
    for fname in ("name", "kind"):
        poly_lyr.CreateField(ogr.FieldDefn(fname, ogr.OFTString))
    for el in js["elements"]:
        tags = el.get("tags", {})
        name = tags.get("name:en", "") or tags.get("name", "")
        if tags.get("place") in ("city", "town"):
            kind = "place_" + tags["place"]
        elif tags.get("aeroway") == "aerodrome":
            kind = "airport"
        elif tags.get("leisure") == "nature_reserve" or tags.get("boundary") in ("protected_area", "national_park"):
            kind = "protected_area"
        else:
            continue
        if el["type"] == "node":
            if not name and kind != "airport":
                continue
            geom = ogr.Geometry(ogr.wkbPoint)
            geom.AddPoint(el["lon"], el["lat"])
            feat = ogr.Feature(pt_lyr.GetLayerDefn())
            feat.SetGeometry(geom)
            feat.SetField("name", name)
            feat.SetField("kind", kind)
            pt_lyr.CreateFeature(feat)
        elif el["type"] == "way" and "geometry" in el:
            coords = el["geometry"]
            if len(coords) < 3:
                continue
            ring = ogr.Geometry(ogr.wkbLinearRing)
            for pt in coords:
                ring.AddPoint(pt["lon"], pt["lat"])
            ring.CloseRings()
            poly = ogr.Geometry(ogr.wkbPolygon)
            poly.AddGeometry(ring)
            if kind == "airport":
                feat = ogr.Feature(pt_lyr.GetLayerDefn())
                feat.SetGeometry(poly.Centroid())
                feat.SetField("name", name)
                feat.SetField("kind", kind)
                pt_lyr.CreateFeature(feat)
            else:
                feat = ogr.Feature(poly_lyr.GetLayerDefn())
                feat.SetGeometry(poly)
                feat.SetField("name", name)
                feat.SetField("kind", kind)
                poly_lyr.CreateFeature(feat)
        elif el["type"] == "relation" and el.get("tags", {}).get("aeroway") == "aerodrome" and "center" in el:
            geom = ogr.Geometry(ogr.wkbPoint)
            geom.AddPoint(el["center"]["lon"], el["center"]["lat"])
            feat = ogr.Feature(pt_lyr.GetLayerDefn())
            feat.SetGeometry(geom)
            feat.SetField("name", name)
            feat.SetField("kind", "airport")
            pt_lyr.CreateFeature(feat)
    ds = None

    proj4 = zone["proj4"]
    reproject_gpkg(hw_path, "highways", zdir + r"\highways_ldp.gpkg", "highways", proj4)
    reproject_gpkg(poi_path, "poi_points", zdir + r"\poi_ldp.gpkg", "poi_points", proj4)
    aoi_tmp = zdir + r"\poi_ldp_aoi_tmp.gpkg"
    reproject_gpkg(poi_path, "aoi_polygons", aoi_tmp, "aoi_polygons", proj4)
    # merge poi points + aoi polygons into one gpkg with two layers
    merged_path = zdir + r"\poi_ldp.gpkg"
    src = ogr.Open(aoi_tmp)
    src_lyr = src.GetLayer(0)
    dst = ogr.Open(merged_path, update=1)
    dst.CopyLayer(src_lyr, "aoi_polygons")
    dst = None
    src = None
    try:
        os.remove(aoi_tmp)
    except OSError:
        pass

    # merge highway segments sharing the same ref into fewer, longer lines
    # so route-shield labels don't repeat every few hundred metres
    merge_highways_by_ref(zdir + r"\highways_ldp.gpkg", zdir + r"\highways_merged.gpkg")


def merge_highways_by_ref(src_path, dst_path):
    from osgeo import ogr
    src = ogr.Open(src_path)
    lyr = src.GetLayer(0)
    groups = {}
    for f in lyr:
        key = (f.GetField("ref") or "", f.GetField("rank"))
        g = f.GetGeometryRef()
        if g is None:
            continue
        groups.setdefault(key, []).append(g.Clone())

    drv = ogr.GetDriverByName("GPKG")
    if os.path.exists(dst_path):
        drv.DeleteDataSource(dst_path)
    ds = drv.CreateDataSource(dst_path)
    out_lyr = ds.CreateLayer("highways", lyr.GetSpatialRef(), ogr.wkbMultiLineString)
    out_lyr.CreateField(ogr.FieldDefn("ref", ogr.OFTString))
    out_lyr.CreateField(ogr.FieldDefn("rank", ogr.OFTInteger))
    for (ref, rank), geoms in groups.items():
        union = None
        for g in geoms:
            union = g if union is None else union.Union(g)
        try:
            merged = ogr.ForceToMultiLineString(union)
        except Exception:
            merged = union
        feat = ogr.Feature(out_lyr.GetLayerDefn())
        feat.SetGeometry(merged)
        feat.SetField("ref", ref)
        feat.SetField("rank", rank)
        out_lyr.CreateFeature(feat)
    ds = None


def dms(decimal_deg, is_lat):
    hemi = ("N" if decimal_deg >= 0 else "S") if is_lat else ("E" if decimal_deg >= 0 else "W")
    d = abs(decimal_deg)
    deg = int(d)
    m_full = (d - deg) * 60
    minutes = int(round(m_full))
    if minutes == 60:
        minutes = 0
        deg += 1
    return f"{deg}° {minutes:02d}' 00\" {hemi}"


def build_layout(zone, zdir, render_path):
    import math as _m
    from qgis.core import (
        Qgis, QgsApplication, QgsProject, QgsRasterLayer, QgsVectorLayer,
        QgsCoordinateReferenceSystem, QgsPrintLayout, QgsLayoutItemMap,
        QgsLayoutItemLabel, QgsLayoutItemShape, QgsLayoutItemScaleBar, QgsLayoutItemPicture,
        QgsLayoutPoint, QgsLayoutSize, QgsUnitTypes, QgsLayoutExporter,
        QgsSingleSymbolRenderer, QgsLineSymbol, QgsFillSymbol, QgsSimpleFillSymbolLayer,
        QgsPalLayerSettings, QgsTextFormat, QgsVectorLayerSimpleLabeling,
        QgsLayoutItemMapGrid, QgsLayoutMeasurement, QgsFeature, QgsGeometry, QgsPointXY,
        QgsRendererCategory, QgsCategorizedSymbolRenderer, QgsMarkerSymbol, QgsTextBackgroundSettings,
        QgsLayoutItem, QgsRuleBasedLabeling,
    )
    from qgis.PyQt.QtGui import QColor, QFont
    from qgis.PyQt.QtCore import Qt, QSizeF

    proj4 = zone["proj4"]
    ldp_crs = QgsCoordinateReferenceSystem.fromProj4(proj4)
    assert ldp_crs.isValid(), f"invalid CRS for {zone['name']}"

    project = QgsProject.instance()
    project.setCrs(ldp_crs)

    def add_raster(path, name):
        lyr = QgsRasterLayer(path, name)
        assert lyr.isValid(), f"invalid raster {path}"
        project.addMapLayer(lyr)
        return lyr

    def add_vector(path, name, layername=None):
        uri = path if not layername else f"{path}|layername={layername}"
        lyr = QgsVectorLayer(uri, name, "ogr")
        assert lyr.isValid(), f"invalid vector {path}"
        project.addMapLayer(lyr)
        return lyr

    render_lyr = add_raster(render_path, "Distortion over relief")

    boundary_lyr = add_vector(zdir + r"\boundary_ldp.gpkg", f"{zone['name']} boundary")
    sym = QgsFillSymbol()
    sym.deleteSymbolLayer(0)
    halo = QgsSimpleFillSymbolLayer()
    halo.setBrushStyle(Qt.BrushStyle.NoBrush)
    halo.setStrokeColor(QColor(30, 30, 30, 190))
    halo.setStrokeWidth(1.1)
    sym.appendSymbolLayer(halo)
    white_line = QgsSimpleFillSymbolLayer()
    white_line.setBrushStyle(Qt.BrushStyle.NoBrush)
    white_line.setStrokeColor(QColor(255, 255, 255, 255))
    white_line.setStrokeWidth(0.5)
    sym.appendSymbolLayer(white_line)
    boundary_lyr.setRenderer(QgsSingleSymbolRenderer(sym))

    provinces_lyr = add_vector(zdir + r"\provinces_ldp.gpkg", "Neighbouring provinces")
    psym = QgsFillSymbol.createSimple({"color": "0,0,0,0", "outline_color": "90,90,90,160", "outline_width": "0.25"})
    provinces_lyr.setRenderer(QgsSingleSymbolRenderer(psym))
    provinces_lyr.setSubsetString(f"NAME_1 <> '{zone['name']}'")
    pal = QgsPalLayerSettings()
    pal.fieldName = "NAME_1"
    tf = QgsTextFormat()
    f = QFont("Tahoma", 8)
    f.setItalic(True)
    tf.setFont(f)
    tf.setSize(8)
    tf.setColor(QColor(70, 70, 70))
    pal.setFormat(tf)
    provinces_lyr.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    provinces_lyr.setLabelsEnabled(True)

    zero_lyr = add_vector(zdir + r"\zero_line_clean.gpkg", "Zero distortion")
    lsym = QgsLineSymbol.createSimple({"line_color": "40,40,40,200", "line_width": "0.25", "line_style": "dash"})
    zero_lyr.setRenderer(QgsSingleSymbolRenderer(lsym))

    cm_lyr = QgsVectorLayer(f"LineString?crs={proj4}", "Central meridian", "memory")
    cm_lyr.setCrs(ldp_crs)
    ext = render_lyr.extent()
    view_h = ext.width() * MAP_H_VIEW / MAP_W
    if view_h < ext.height():  # centre-crop the raster to the map box's aspect
        cy = (ext.yMinimum() + ext.yMaximum()) / 2
        ext = type(ext)(ext.xMinimum(), cy - view_h / 2, ext.xMaximum(), cy + view_h / 2)
    feat = QgsFeature()
    feat.setGeometry(QgsGeometry.fromPolylineXY([
        QgsPointXY(zone["x0"], ext.yMinimum() - 1000), QgsPointXY(zone["x0"], ext.yMaximum() + 1000)]))
    cm_lyr.dataProvider().addFeature(feat)
    cm_lyr.updateExtents()
    project.addMapLayer(cm_lyr)
    cmsym = QgsLineSymbol.createSimple({"line_color": "0,0,0,255", "line_width": "0.5", "line_style": "dash dot"})
    cm_lyr.setRenderer(QgsSingleSymbolRenderer(cmsym))

    sp_lyr = None
    sp_title = "STANDARD PARALLEL" if zone["kind"] == "LCC" else "ORIGIN LATITUDE"
    if ext.yMinimum() < zone["y0"] < ext.yMaximum():
        sp_lyr = QgsVectorLayer(f"LineString?crs={proj4}", sp_title.capitalize(), "memory")
        sp_lyr.setCrs(ldp_crs)
        sfeat = QgsFeature()
        sfeat.setGeometry(QgsGeometry.fromPolylineXY([
            QgsPointXY(ext.xMinimum() - 1000, zone["y0"]), QgsPointXY(ext.xMaximum() + 1000, zone["y0"])]))
        sp_lyr.dataProvider().addFeature(sfeat)
        sp_lyr.updateExtents()
        project.addMapLayer(sp_lyr)
        sp_lyr.setRenderer(QgsSingleSymbolRenderer(QgsLineSymbol.createSimple(
            {"line_color": "0,0,0,255", "line_width": "0.5", "line_style": "dash"})))

    hw_lyr = add_vector(zdir + r"\highways_merged.gpkg", "Main highways", "highways")
    hw_style = {
        1: ("Motorway", "20,50,25,255", 1.1 * 0.5),
        2: ("Trunk", "25,65,32,255", 0.9 * 0.5),
        3: ("Primary", "35,80,40,255", 0.7 * 0.5),
        9: ("Other", "35,80,40,255", 0.5 * 0.5),
    }
    hw_cats = [QgsRendererCategory(rank, QgsLineSymbol.createSimple(
        {"line_color": color, "line_width": str(width), "capstyle": "round", "joinstyle": "round"}), label)
        for rank, (label, color, width) in hw_style.items()]
    hw_lyr.setRenderer(QgsCategorizedSymbolRenderer("rank", hw_cats))

    hw_pal = QgsPalLayerSettings()
    hw_pal.fieldName = "ref"
    hw_tf = QgsTextFormat()
    hwf = QFont("Tahoma", round(7 * 0.75))
    hwf.setBold(True)
    hw_tf.setFont(hwf)
    hw_tf.setSize(7 * 0.75)
    hw_tf.setColor(QColor(10, 10, 10))
    shield = QgsTextBackgroundSettings()
    shield.setEnabled(True)
    shield.setType(QgsTextBackgroundSettings.ShapeType.ShapeRectangle)
    shield.setSizeType(QgsTextBackgroundSettings.SizeType.SizeBuffer)
    shield.setSize(QSizeF(1.3, 1.0))
    shield.setFillColor(QColor(255, 255, 255, 255))
    shield.setStrokeColor(QColor(0, 0, 0, 255))
    shield.setStrokeWidth(0.35)
    shield.setRadii(QSizeF(1.0, 1.0))
    hw_tf.setBackground(shield)
    hw_pal.setFormat(hw_tf)
    hw_pal.placement = QgsPalLayerSettings.Placement.Horizontal
    hw_lyr.setLabeling(QgsVectorLayerSimpleLabeling(hw_pal))
    hw_lyr.setLabelsEnabled(True)

    aoi_lyr = add_vector(zdir + r"\poi_ldp.gpkg", "AOI (protected areas)", "aoi_polygons")
    aoi_sym = QgsFillSymbol.createSimple({"color": "40,120,40,55", "outline_color": "30,100,30,180",
                                           "outline_width": "0.3", "outline_style": "dash"})
    aoi_lyr.setRenderer(QgsSingleSymbolRenderer(aoi_sym))
    aoi_pal = QgsPalLayerSettings()
    aoi_pal.fieldName = "name"
    aoi_tf = QgsTextFormat()
    aoif = QFont("Tahoma", 7)
    aoif.setItalic(True)
    aoi_tf.setFont(aoif)
    aoi_tf.setSize(7)
    aoi_tf.setColor(QColor(20, 90, 20))
    aoi_pal.setFormat(aoi_tf)
    aoi_lyr.setLabeling(QgsVectorLayerSimpleLabeling(aoi_pal))
    aoi_lyr.setLabelsEnabled(True)

    poi_lyr = add_vector(zdir + r"\poi_ldp.gpkg", "POI / places", "poi_points")
    # OSM tags some provincial capitals (e.g. Phrae) place=town; promote a town named after a province
    caps = province_capital_names()
    kind_idx = poi_lyr.fields().indexOf("kind")
    promote = {f.id(): {kind_idx: "place_city"} for f in poi_lyr.getFeatures()
               if f["kind"] == "place_town" and _norm_name(f["name"] or "") in caps}
    if promote:
        poi_lyr.dataProvider().changeAttributeValues(promote)
    poi_style = {
        "place_city": ("Provincial capital", "0,0,0,255", "circle", 2.6),
        "place_town": ("Town", "60,60,60,255", "circle", 1.15),  # 1.6 x (5 pt label / 7 pt label)
        "airport": ("Airport", "20,70,160,255", "triangle", 2.4),
    }
    poi_cats = [QgsRendererCategory(kind, QgsMarkerSymbol.createSimple(
        {"name": shape, "color": color, "outline_color": "255,255,255,255", "outline_width": "0.2", "size": str(size),
         "outline_style": "no" if kind == "place_town" else "solid"}), label)
        for kind, (label, color, shape, size) in poi_style.items()]
    poi_lyr.setRenderer(QgsCategorizedSymbolRenderer("kind", poi_cats))
    poi_pal = QgsPalLayerSettings()
    poi_pal.fieldName = "name"
    poi_tf = QgsTextFormat()
    poif = QFont("Tahoma", 7)
    poi_tf.setFont(poif)
    poi_tf.setSize(7)
    poi_tf.setColor(QColor(0, 0, 0))
    pbuf = poi_tf.buffer()
    pbuf.setEnabled(True)
    pbuf.setSize(0.6)
    pbuf.setColor(QColor(255, 255, 255))
    poi_tf.setBuffer(pbuf)
    poi_pal.setFormat(poi_tf)
    poi_pal.dist = 1.5
    # Amphur (place=town) names are the many small labels: smaller text, thinner halo.
    # Provincial capitals (city) and airports keep the full-size style.
    amp_pal = QgsPalLayerSettings(poi_pal)
    amp_tf = QgsTextFormat(poi_tf)
    amp_tf.setSize(5)
    amp_buf = amp_tf.buffer()
    amp_buf.setEnabled(False)  # Amphur labels: plain black text, no halo
    amp_tf.setBuffer(amp_buf)
    amp_pal.setFormat(amp_tf)
    poi_root = QgsRuleBasedLabeling.Rule(None)
    poi_root.appendChild(QgsRuleBasedLabeling.Rule(poi_pal, 0, 0, "\"kind\" <> 'place_town'", "Province / airport"))
    poi_root.appendChild(QgsRuleBasedLabeling.Rule(amp_pal, 0, 0, "\"kind\" = 'place_town'", "Amphur"))
    poi_lyr.setLabeling(QgsRuleBasedLabeling(poi_root))
    poi_lyr.setLabelsEnabled(True)

    utm_crs = QgsCoordinateReferenceSystem("EPSG:32647")
    th_lyr = QgsVectorLayer(THAILAND_OUTLINE, "Thailand", "ogr")
    project.addMapLayer(th_lyr)
    thsym = QgsFillSymbol.createSimple({"color": "225,222,206,255", "outline_color": "90,90,90,255", "outline_width": "0.2"})
    th_lyr.setRenderer(QgsSingleSymbolRenderer(thsym))

    loc_lyr = QgsVectorLayer(zdir + r"\boundary_utm.gpkg", f"{zone['name']} (locator)", "ogr")
    project.addMapLayer(loc_lyr)
    locsym = QgsFillSymbol.createSimple({"color": "210,30,30,255", "outline_color": "120,0,0,255", "outline_width": "0.3"})
    loc_lyr.setRenderer(QgsSingleSymbolRenderer(locsym))

    # =========================== LAYOUT ===========================
    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    layout.setName(f"{zone['name']} TH-LDP sheet")
    page = layout.pageCollection().pages()[0]
    page.setPageSize(QgsLayoutSize(PAGE_W, PAGE_H, QgsUnitTypes.LayoutMillimeters))

    def add_label(text, x, y, w, h, size=10, bold=False, italic=False,
                  align=Qt.AlignmentFlag.AlignLeft, color=QColor(0, 0, 0), font_family="Arial"):
        lbl = QgsLayoutItemLabel(layout)
        lbl.setText(text)
        fnt = QFont(font_family, int(round(size)))
        fnt.setBold(bold)
        fnt.setItalic(italic)
        if size != int(round(size)):
            fnt.setPointSizeF(size)
        lbl.setFont(fnt)
        lbl.setFontColor(color)
        lbl.setHAlign(align)
        lbl.attemptMove(QgsLayoutPoint(x, y, QgsUnitTypes.LayoutMillimeters))
        lbl.attemptResize(QgsLayoutSize(w, h, QgsUnitTypes.LayoutMillimeters))
        layout.addLayoutItem(lbl)
        return lbl

    hdr = zone_header(zone["code"])
    if hdr:
        sep = "   |   "
        add_label(f"ISO_1:{hdr['ISO_1']}{sep}TH-LDP : {hdr['HASC_1']}{sep}Zone: {hdr['NAME_1']}"
                  f"{sep}Cov%: {float(hdr['POP_Coverage(%)']):.2f}",
                  MAP_X, 19, MAP_W, 7, size=11, bold=True, align=Qt.AlignmentFlag.AlignHCenter)

    MAP_H = min(MAP_W / (ext.width() / ext.height()), MAP_H_VIEW)
    main_map = QgsLayoutItemMap(layout)
    main_map.attemptMove(QgsLayoutPoint(MAP_X, MAP_Y, QgsUnitTypes.LayoutMillimeters))
    main_map.attemptResize(QgsLayoutSize(MAP_W, MAP_H, QgsUnitTypes.LayoutMillimeters))
    main_map.setCrs(ldp_crs)
    map_layers = [poi_lyr, hw_lyr, aoi_lyr, cm_lyr] + ([sp_lyr] if sp_lyr else []) + [zero_lyr, provinces_lyr, boundary_lyr, render_lyr]
    main_map.setLayers(map_layers)
    main_map.zoomToExtent(ext)
    main_map.setFrameEnabled(True)
    layout.addLayoutItem(main_map)

    def nice_interval(span, target_divisions=7):
        raw = span / target_divisions
        magnitude = 10 ** _m.floor(_m.log10(raw))
        residual = raw / magnitude
        step = 1 if residual < 1.5 else 2 if residual < 3.5 else 5 if residual < 7.5 else 10
        return step * magnitude

    ldp_interval = nice_interval(min(ext.width(), ext.height()))

    grid = QgsLayoutItemMapGrid("Grid", main_map)
    grid.setEnabled(True)
    grid.setIntervalX(ldp_interval)
    grid.setIntervalY(ldp_interval)
    grid.setAnnotationEnabled(True)
    grid.setAnnotationFormat(QgsLayoutItemMapGrid.CustomFormat)
    grid.setAnnotationExpression("format_number(@grid_number, 0)")
    grid.setAnnotationFont(QFont("Arial", 6))
    grid.setFrameStyle(Qgis.MapGridFrameStyle.LineBorder)
    grid.setFrameWidth(0.8)
    grid.setAnnotationPrecision(0)
    grid.setGridLineColor(QColor(0, 0, 0, 60))
    grid.setAnnotationDirection(Qgis.MapGridAnnotationDirection.Vertical, Qgis.MapGridBorderSide.Left)
    grid.setGridLineWidth(0.1)
    grid.setAnnotationDisplay(Qgis.MapGridComponentVisibility.ShowAll, Qgis.MapGridBorderSide.Left)
    grid.setAnnotationDisplay(Qgis.MapGridComponentVisibility.ShowAll, Qgis.MapGridBorderSide.Bottom)
    grid.setAnnotationDisplay(Qgis.MapGridComponentVisibility.HideAll, Qgis.MapGridBorderSide.Top)
    grid.setAnnotationDisplay(Qgis.MapGridComponentVisibility.HideAll, Qgis.MapGridBorderSide.Right)
    main_map.grids().addGrid(grid)

    geo_grid = QgsLayoutItemMapGrid("Geographic", main_map)
    geo_grid.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
    geo_grid.setEnabled(True)
    geo_interval = nice_interval(ext.width() / 111320.0)
    geo_grid.setIntervalX(geo_interval)
    geo_grid.setIntervalY(geo_interval)
    geo_grid.setAnnotationEnabled(True)
    geo_grid.setAnnotationFormat(Qgis.MapGridAnnotationFormat.DegreeMinuteSecond)
    geo_grid.setAnnotationFont(QFont("Arial", 6))
    geo_grid.setAnnotationPrecision(0)
    geo_grid.setStyle(QgsLayoutItemMapGrid.GridStyle.FrameAndAnnotationsOnly)
    geo_grid.setFrameStyle(Qgis.MapGridFrameStyle.ExteriorTicks)
    geo_grid.setFrameWidth(0.8)
    geo_grid.setAnnotationDirection(Qgis.MapGridAnnotationDirection.Vertical, Qgis.MapGridBorderSide.Right)
    geo_grid.setAnnotationDisplay(Qgis.MapGridComponentVisibility.ShowAll, Qgis.MapGridBorderSide.Top)
    geo_grid.setAnnotationDisplay(Qgis.MapGridComponentVisibility.ShowAll, Qgis.MapGridBorderSide.Right)
    geo_grid.setAnnotationDisplay(Qgis.MapGridComponentVisibility.HideAll, Qgis.MapGridBorderSide.Left)
    geo_grid.setAnnotationDisplay(Qgis.MapGridComponentVisibility.HideAll, Qgis.MapGridBorderSide.Bottom)
    main_map.grids().addGrid(geo_grid)

    def add_rotated_label(text, center_x, center_y, w, h, size, bold=False, color=QColor(0, 0, 0), rotation=-90):
        lbl = QgsLayoutItemLabel(layout)
        lbl.setText(text)
        fnt = QFont("Tahoma", int(round(size)))
        fnt.setBold(bold)
        tf = QgsTextFormat()
        tf.setFont(fnt)
        tf.setSize(size)
        tf.setColor(color)
        buf = tf.buffer()  # white halo so the text reads over the dark map
        buf.setEnabled(True)
        buf.setSize(1.2)
        buf.setColor(QColor(255, 255, 255))
        tf.setBuffer(buf)
        lbl.setTextFormat(tf)
        lbl.setHAlign(Qt.AlignmentFlag.AlignHCenter)
        lbl.setVAlign(Qt.AlignmentFlag.AlignVCenter)
        lbl.attemptResize(QgsLayoutSize(w, h, QgsUnitTypes.LayoutMillimeters))
        lbl.setReferencePoint(QgsLayoutItem.ReferencePoint.Middle)
        lbl.attemptMove(QgsLayoutPoint(center_x, center_y, QgsUnitTypes.LayoutMillimeters), useReferencePoint=True)
        lbl.setItemRotation(rotation)
        layout.addLayoutItem(lbl)
        return lbl

    cm_value_str = dms(zone["lon_0"], is_lat=False)
    cm_x_mm = MAP_X + (zone["x0"] - ext.xMinimum()) / ext.width() * MAP_W
    cm_cy = MAP_Y + MAP_H * 0.33
    cm_gap = -0.3
    add_rotated_label("CENTRAL MERIDIAN", cm_x_mm - (8 / 2 + cm_gap), cm_cy, 55, 8, size=11, bold=True)
    add_rotated_label(cm_value_str, cm_x_mm + (9 / 2 + cm_gap), cm_cy, 36, 9, size=11)

    if sp_lyr is not None:
        sp_value_str = dms(zone["lat_0"], is_lat=True)
        sp_y_mm = MAP_Y + (ext.yMaximum() - zone["y0"]) / ext.height() * MAP_H
        # default: the side with more clearance from the central-meridian label
        sp_x_def = MAP_X + MAP_W * 0.78 if cm_x_mm < MAP_X + MAP_W * 0.5 else MAP_X + MAP_W * 0.22
        # town/airport labels in map-box mm, each approximated by a box that covers either side of its point
        poi_boxes = []
        for pf in poi_lyr.getFeatures():
            pg = pf.geometry().asPoint()
            nm = pf["name"] or ""
            half_w = len(nm) * 1.4 + 3
            px = MAP_X + (pg.x() - ext.xMinimum()) / ext.width() * MAP_W
            py = MAP_Y + (ext.yMaximum() - pg.y()) / ext.height() * MAP_H
            poi_boxes.append((px - half_w, py - 3.5, px + half_w, py + 3.5))
        sp_half = len(sp_title) * 2.4 / 2 + 1  # title is the wider of the two lines

        def sp_hits(cx):
            box = (cx - sp_half, sp_y_mm - 9, cx + sp_half, sp_y_mm + 8.5)
            n = sum(1 for b in poi_boxes if b[0] < box[2] and b[2] > box[0] and b[1] < box[3] and b[3] > box[1])
            return n + (5 if abs(cx - cm_x_mm) < sp_half + 10 else 0)  # keep clear of the meridian label

        cands = [MAP_X + sp_half + 3 + i * 2 for i in range(int((MAP_W - 2 * sp_half - 6) / 2) + 1)]
        sp_x_mm = min(cands, key=lambda cx: (sp_hits(cx), abs(cx - sp_x_def)))
        sp_gap = 0.5
        add_rotated_label(sp_title, sp_x_mm, sp_y_mm - (8 / 2 + sp_gap), 55, 8,
                           size=11, bold=True, rotation=0)
        add_rotated_label(sp_value_str, sp_x_mm, sp_y_mm + (7 / 2 + sp_gap), 45, 7, size=11, rotation=0)

    TXT_Y = MAP_Y + MAP_H + 6
    COL1_X, COL1_W = 10, 90
    COL2_X, COL2_W = 104, 52
    COL3_X, COL3_W = 162, PAGE_W - 10 - 162

    LOC_W, LOC_H = COL3_W, 42
    LOC_Y = TXT_Y + 5
    loc_map = QgsLayoutItemMap(layout)
    loc_map.attemptMove(QgsLayoutPoint(COL3_X, LOC_Y, QgsUnitTypes.LayoutMillimeters))
    loc_map.attemptResize(QgsLayoutSize(LOC_W, LOC_H, QgsUnitTypes.LayoutMillimeters))
    loc_map.setCrs(utm_crs)
    loc_map.setLayers([loc_lyr, th_lyr])
    loc_map.zoomToExtent(th_lyr.extent())
    loc_map.setFrameEnabled(True)
    loc_map.setBackgroundColor(QColor(255, 255, 255, 255))
    loc_map.setBackgroundEnabled(True)
    layout.addLayoutItem(loc_map)
    add_label("Thailand", COL3_X, TXT_Y, LOC_W, 5, size=7, italic=True, align=Qt.AlignmentFlag.AlignHCenter)

    add_label("Thailand Low Distortion Map Coordinate System (TH-LDP)", COL1_X, TXT_Y, COL1_W, 11, size=11, italic=True)
    add_label(f"{zone['name']} Zone", COL1_X, TXT_Y + 11, COL1_W, 9, size=16, bold=True)

    if zone["kind"] == "LCC":
        proj_name = "Lambert Conformal Conic projection"
        lat_label = "Origin latitude (standard parallel)"
    else:
        proj_name = "Transverse Mercator projection"
        lat_label = "Origin latitude"
    lat0_str = dms(zone["lat_0"], is_lat=True)
    fn = f"{zone['x0']:,.3f}".replace(",", " ")
    fnorth = f"{zone['y0']:,.3f}".replace(",", " ")
    params_text = (
        f"{proj_name}\n"
        "World Geodetic System 1984\n\n"
        f"{lat_label}:  {lat0_str}\n"
        f"Central meridian:  {cm_value_str}\n"
        f"False easting:  {fn} m\n"
        f"False northing:  {fnorth} m\n"
        f"Central meridian scale factor:  {zone['k0']:.6f} (exact)"
    )
    # committee design values (only for zones whose definition carries them):
    # two extra lines, so push the grid note / scale bar down to make room
    dy = 0
    if zone.get("hpp") is not None:
        params_text += (
            f"\nDesign project plane:  hPP {zone['hpp']:+.2f} m  (MSL {zone['msl']:+.2f} m)"
            f"\nCSF design range:  ±{zone['csf']:g} ppm"
        )
        dy = 9
    add_label(params_text, COL1_X, TXT_Y + 23, COL1_W, 36 + dy, size=8.5)
    add_label(
        f"Projected map grid is shown in unit meters ({ldp_interval/1000:g} km interval)",
        COL1_X, TXT_Y + 57 + dy, COL1_W, 6, size=8
    )
    add_label(
        "Prepared for the TH-LDP Committee\nriabroy@gmail.com\nLicense: CC BY-NC-SA 4.0\n\n"
        "Base data: FABDEM (elevation),\nTGM2017 (geoid), GADM 4.1\n(admin.), OpenStreetMap\n(roads, places, AOI)\n\n"
        "Map created 2026-10-20",
        COL3_X, LOC_Y + LOC_H + 4, COL3_W, 32, size=6.5
    )

    LEG_X = COL2_X
    LEG_Y = TXT_Y + 8
    add_label("Combined scale factor distortion", LEG_X, LEG_Y - 8, COL2_W + 20, 6, size=9, bold=True)
    bins = [
        ("< −50 ppm", (18, 62, 150)), ("±(40–50) ppm", (217, 95, 14)),
        ("±(30–40) ppm", (253, 141, 60)), ("±(20–30) ppm", (254, 217, 118)),
        ("±(10–20) ppm", (173, 221, 142)), ("±10 ppm", (65, 171, 93)),
        ("> +50 ppm", (203, 24, 29)),
    ]
    sw, sw_gap = 5, 0.9
    for i, (label, col) in enumerate(bins):
        y = LEG_Y + i * (sw + sw_gap)
        shp = QgsLayoutItemShape(layout)
        shp.setShapeType(QgsLayoutItemShape.Rectangle)
        shp.attemptMove(QgsLayoutPoint(LEG_X, y, QgsUnitTypes.LayoutMillimeters))
        shp.attemptResize(QgsLayoutSize(sw, sw, QgsUnitTypes.LayoutMillimeters))
        fsym = QgsFillSymbol.createSimple({"color": f"{col[0]},{col[1]},{col[2]},255", "outline_color": "40,40,40,255", "outline_width": "0.15"})
        shp.setSymbol(fsym)
        layout.addLayoutItem(shp)
        add_label(label, LEG_X + sw + 2, y - 0.5, 45, sw, size=7.5)

    zy = LEG_Y + len(bins) * (sw + sw_gap) + 1.5
    add_label("- - -  Zero distortion line", LEG_X, zy, 60, 5, size=7.5)
    # central meridian / origin latitude need no legend entry: they are annotated on the map
    zy2 = zy + 4.2
    add_label("—  Motorway / trunk / primary road", LEG_X, zy2, 70, 5, size=7.5)
    add_label("●  Town    ▲  Airport    ▒▒  Protected area", LEG_X, zy2 + 4.2, 75, 5, size=7.5)

    # Zebra scale bar, as in the OCRS handbook: two rows of alternating black/white cells, five
    # segments, the first one split into five small cells. Drawn from rectangles because the
    # built-in bar cannot subdivide its first segment.
    # Longest of 100/50/20/10 km that fits under the legend, left of the location map
    # (zones narrower than ~135 km cannot show 100 km).
    bar_km = next((k for k in (100, 50, 20, 10) if k * 1000 / ext.width() * MAP_W <= COL3_X - COL1_X - 12), 5)
    bar_mm = bar_km * 1000 / ext.width() * MAP_W
    seg_mm = bar_mm / 5
    row_h = 2.2
    sb_x, sb_y = 10, TXT_Y + 69 + dy
    cells = [(i * seg_mm / 5, seg_mm / 5, i) for i in range(5)]                # first segment, fine cells
    cells += [(seg_mm * s, seg_mm, 4 + s) for s in range(1, 5)]                # then four coarse segments
    for x_off, w, k in cells:
        for row in (0, 1):
            black = (k + row) % 2 == 0
            shp = QgsLayoutItemShape(layout)
            shp.setShapeType(QgsLayoutItemShape.Rectangle)
            shp.attemptMove(QgsLayoutPoint(sb_x + x_off, sb_y + row * row_h, QgsUnitTypes.LayoutMillimeters))
            shp.attemptResize(QgsLayoutSize(w, row_h, QgsUnitTypes.LayoutMillimeters))
            shp.setSymbol(QgsFillSymbol.createSimple({
                "color": "0,0,0,255" if black else "255,255,255,255",
                "outline_color": "0,0,0,255", "outline_width": "0.2"}))
            layout.addLayoutItem(shp)
    for i in range(6):
        add_label(f"{bar_km / 5 * i:g}", sb_x + i * seg_mm - 8, sb_y - 5.5, 16, 5, size=8,
                  align=Qt.AlignmentFlag.AlignHCenter)
    add_label("km", sb_x + bar_mm / 2 - 8, sb_y + 2 * row_h + 0.5, 16, 5, size=8,
              align=Qt.AlignmentFlag.AlignHCenter)

    # north arrow: QGIS's own SVG, beside the legend swatches; the map is not rotated, so up is grid north
    north_svg = next((os.path.join(p, "arrows", "NorthArrow_02.svg") for p in QgsApplication.svgPaths()
                      if os.path.exists(os.path.join(p, "arrows", "NorthArrow_02.svg"))), None)
    if north_svg:
        na = QgsLayoutItemPicture(layout)
        na.setPicturePath(north_svg)
        na.attemptMove(QgsLayoutPoint(COL2_X + 32.4, TXT_Y + 19.2, QgsUnitTypes.LayoutMillimeters))
        na.attemptResize(QgsLayoutSize(15.6, 15.6, QgsUnitTypes.LayoutMillimeters))
        layout.addLayoutItem(na)
        add_label("N", COL2_X + 27.5, TXT_Y + 16, 20, 6, size=11, bold=True, align=Qt.AlignmentFlag.AlignHCenter)
    else:
        add_label("Grid North ↑", COL2_X + 27.5, TXT_Y + 14, 20, 8, size=10, bold=True)

    add_label("A - draft", 10, PAGE_H - 10, 40, 6, size=9, color=QColor(150, 150, 150))
    add_label("Pilot sheet | TH-LDP Committee", PAGE_W - 70, PAGE_H - 10, 60, 6, size=9,
              align=Qt.AlignmentFlag.AlignRight, color=QColor(150, 150, 150))

    project.layoutManager().addLayout(layout)
    exporter = QgsLayoutExporter(layout)
    out_base = OUTPUT_DIR + rf"\{zone['name'].replace(' ', '_')}_TH-LDP"
    out_base += os.environ.get("TH_LDP_OUT_SUFFIX", "")  # e.g. "_review" when the normal PDF is locked
    res = exporter.exportToPdf(out_base + ".pdf", QgsLayoutExporter.PdfExportSettings())
    if res != QgsLayoutExporter.ExportResult.Success:
        print(f"WARNING: PDF export failed ({res}) - is {out_base}.pdf open in a viewer?", flush=True)
    img_settings = QgsLayoutExporter.ImageExportSettings()
    img_settings.dpi = 150
    exporter.exportToImage(out_base + ".png", img_settings)
    project.write(zdir + r"\project.qgz")
    return out_base + ".pdf"


def process_zone(zone_name, skip_osm=False, osm_only=False):
    zone = load_zone(zone_name)
    zdir = ZONES_DIR + "\\" + zone["slug"]
    os.makedirs(zdir, exist_ok=True)
    print(f"== {zone['name']} ({zone['code']}, {zone['kind']}) ==")

    bbox0 = extract_boundary(zone, zdir)
    if bbox0 is None:
        print(f"  SKIP: no GADM boundary found for NAME_1='{zone['name']}'")
        return None
    bbox = buffered_bbox(bbox0)

    if not osm_only:
        print("  building raster...")
        render_path = build_raster(zone, zdir, bbox)
        print("  building vectors...")
        build_vectors(zone, zdir)
    else:
        render_path = zdir + r"\zone_render.tif"

    if not skip_osm:
        print("  fetching OSM...")
        hw_json, poi_json = fetch_osm(zone, zdir, bbox)
        osm_to_gpkg(zone, zdir, hw_json, poi_json)

    if osm_only:
        return None

    print("  building layout...")
    pdf_path = build_layout(zone, zdir, render_path)
    print(f"  -> {pdf_path}")
    return pdf_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("zone", help="zone name as it appears in th_ldp.tsv, e.g. 'Phrae'")
    ap.add_argument("--skip-osm", action="store_true")
    ap.add_argument("--osm-only", action="store_true")
    args = ap.parse_args()

    from qgis.core import QgsApplication
    qgs = QgsApplication([], False)
    qgs.setPrefixPath(QGIS_PREFIX, True)
    qgs.initQgis()
    try:
        process_zone(args.zone, skip_osm=args.skip_osm, osm_only=args.osm_only)
    finally:
        qgs.exitQgis()


if __name__ == "__main__":
    main()
