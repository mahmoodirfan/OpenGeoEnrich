# -*- coding: utf-8 -*-
"""OSM/Overpass data preparation helpers for OpenGeoEnrich.

This module intentionally uses only Python standard-library networking so the
plugin does not depend on requests or other third-party packages.
"""

import hashlib
import json
import os
import shutil
import time
import urllib.parse
import urllib.request

try:
    import processing
except Exception:  # pragma: no cover
    processing = None

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)

OVERPASS_URLS = [
    'https://overpass-api.de/api/interpreter',
    'https://overpass.kumi.systems/api/interpreter',
]

DEFAULT_FACILITY_CATEGORIES = 'schools,health,markets,water'


def ensure_folder(path):
    if path and not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


def _valid_lonlat(x, y):
    try:
        x = float(x)
        y = float(y)
    except Exception:
        return False
    return -180.0 <= x <= 180.0 and -90.0 <= y <= 90.0


def _bbox_from_points(points):
    xs = [p[0] for p in points if _valid_lonlat(p[0], p[1])]
    ys = [p[1] for p in points if _valid_lonlat(p[0], p[1])]
    if not xs or not ys:
        return None
    west = max(-180.0, min(xs))
    east = min(180.0, max(xs))
    south = max(-90.0, min(ys))
    north = min(90.0, max(ys))
    if west < east and south < north:
        return south, west, north, east
    return None


def _osr_transform_points(points, src_authid):
    """Fallback coordinate transformation using GDAL/OSR.

    Some QGIS/PROJ builds are stricter when transforming a whole QgsRectangle
    and can raise an exception even for normal UTM AOIs. Transforming sampled
    points via OSR gives us a defensive fallback for Overpass/COG bounding boxes.
    """
    try:
        from osgeo import osr
        src_srs = osr.SpatialReference()
        dst_srs = osr.SpatialReference()
        if str(src_authid).upper().startswith('EPSG:'):
            src_srs.ImportFromEPSG(int(str(src_authid).split(':', 1)[1]))
        else:
            return []
        dst_srs.ImportFromEPSG(4326)
        try:
            src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        except Exception:
            pass
        ct = osr.CoordinateTransformation(src_srs, dst_srs)
        out = []
        for x, y in points:
            try:
                lon, lat, _z = ct.TransformPoint(float(x), float(y))
                if _valid_lonlat(lon, lat):
                    out.append((lon, lat))
            except Exception:
                continue
        return out
    except Exception:
        return []


def bbox_from_layer_wgs84(layer):
    """Return layer extent transformed to WGS84 as (south, west, north, east).

    Uses a robust multi-step strategy because QGIS can occasionally fail on
    transformBoundingBox() for projected AOIs even when point transforms are OK.
    """
    if layer is None or not layer.isValid():
        raise ValueError('Invalid study area layer.')

    src = layer.crs()
    dst = QgsCoordinateReferenceSystem('EPSG:4326')
    extent = QgsRectangle(layer.extent())

    # Fast path for geographic AOIs already in WGS84.
    if src == dst:
        bbox = _bbox_from_points([
            (extent.xMinimum(), extent.yMinimum()),
            (extent.xMinimum(), extent.yMaximum()),
            (extent.xMaximum(), extent.yMinimum()),
            (extent.xMaximum(), extent.yMaximum()),
        ])
        if bbox:
            return bbox

    # Normal QGIS rectangle transform.
    try:
        tr = QgsCoordinateTransform(src, dst, QgsProject.instance())
        tex = tr.transformBoundingBox(extent)
        bbox = _bbox_from_points([
            (tex.xMinimum(), tex.yMinimum()),
            (tex.xMinimum(), tex.yMaximum()),
            (tex.xMaximum(), tex.yMinimum()),
            (tex.xMaximum(), tex.yMaximum()),
        ])
        if bbox:
            return bbox
    except Exception:
        pass

    # Defensive QGIS point-by-point transform of sampled extent and feature bounds.
    sample_src_points = [
        (extent.xMinimum(), extent.yMinimum()),
        (extent.xMinimum(), extent.yMaximum()),
        (extent.xMaximum(), extent.yMinimum()),
        (extent.xMaximum(), extent.yMaximum()),
        ((extent.xMinimum() + extent.xMaximum()) / 2.0, (extent.yMinimum() + extent.yMaximum()) / 2.0),
    ]
    try:
        tr = QgsCoordinateTransform(src, dst, QgsProject.instance())
        transformed = []
        for x, y in sample_src_points:
            try:
                p = tr.transform(QgsPointXY(float(x), float(y)))
                transformed.append((p.x(), p.y()))
            except Exception:
                continue
        bbox = _bbox_from_points(transformed)
        if bbox:
            return bbox
    except Exception:
        pass

    # GDAL/OSR fallback using the same sampled points.
    bbox = _bbox_from_points(_osr_transform_points(sample_src_points, src.authid()))
    if bbox:
        return bbox

    # Final heuristic: if coordinates already look like lon/lat despite CRS metadata, use them.
    bbox = _bbox_from_points(sample_src_points)
    if bbox:
        return bbox

    raise ValueError(
        'Study area extent could not be transformed to WGS84. '
        'Check that the AOI layer CRS is correctly assigned and that the geometry is valid.'
    )


def bbox_hash(bbox, extra=''):
    raw = '{:.6f},{:.6f},{:.6f},{:.6f}|{}'.format(bbox[0], bbox[1], bbox[2], bbox[3], extra)
    return hashlib.md5(raw.encode('utf-8'), usedforsecurity=False).hexdigest()[:12]


def parse_categories(text):
    if not text:
        return []
    cats = []
    for part in str(text).replace(';', ',').split(','):
        c = part.strip().lower()
        if c:
            cats.append(c)
    return cats


def build_roads_query(bbox, timeout):
    s, w, n, e = bbox
    b = '({},{},{},{})'.format(s, w, n, e)
    return """
[out:json][timeout:{timeout}];
(
  way["highway"]{bbox};
);
out tags geom;
""".format(timeout=int(timeout), bbox=b)


def _facility_blocks(categories, custom_tags, bbox_text):
    blocks = []
    cats = set(parse_categories(categories))
    if 'schools' in cats or 'education' in cats:
        blocks.append('nwr["amenity"~"school|college|university|kindergarten"]{};'.format(bbox_text))
    if 'health' in cats or 'healthcare' in cats:
        blocks.append('nwr["amenity"~"hospital|clinic|doctors|pharmacy|dentist"]{};'.format(bbox_text))
        blocks.append('nwr["healthcare"]{};'.format(bbox_text))
    if 'markets' in cats or 'market' in cats or 'shops' in cats:
        blocks.append('nwr["amenity"="marketplace"]{};'.format(bbox_text))
        blocks.append('nwr["shop"~"supermarket|convenience|mall|general|marketplace"]{};'.format(bbox_text))
    if 'water' in cats or 'waterpoints' in cats or 'water_points' in cats:
        blocks.append('nwr["amenity"~"drinking_water|water_point"]{};'.format(bbox_text))
        blocks.append('nwr["man_made"="water_well"]{};'.format(bbox_text))
        blocks.append('nwr["waterway"]{};'.format(bbox_text))

    # Custom tag syntax, one per line or semicolon:
    # amenity=bank
    # shop~supermarket|convenience
    # healthcare
    if custom_tags:
        raw_parts = []
        for line in str(custom_tags).replace(';', '\n').splitlines():
            line = line.strip()
            if line:
                raw_parts.append(line)
        for item in raw_parts:
            if '~' in item:
                key, val = item.split('~', 1)
                blocks.append('nwr["{}"~"{}"]{};'.format(key.strip(), val.strip(), bbox_text))
            elif '=' in item:
                key, val = item.split('=', 1)
                blocks.append('nwr["{}"="{}"]{};'.format(key.strip(), val.strip(), bbox_text))
            else:
                blocks.append('nwr["{}"]{};'.format(item.strip(), bbox_text))
    return blocks


def build_facilities_query(bbox, timeout, categories=DEFAULT_FACILITY_CATEGORIES, custom_tags=''):
    s, w, n, e = bbox
    b = '({},{},{},{})'.format(s, w, n, e)
    blocks = _facility_blocks(categories, custom_tags, b)
    if not blocks:
        blocks = _facility_blocks(DEFAULT_FACILITY_CATEGORIES, '', b)
    return """
[out:json][timeout:{timeout}];
(
  {blocks}
);
out tags center geom;
""".format(timeout=int(timeout), blocks='\n  '.join(blocks))


def fetch_overpass(query, timeout, feedback=None):
    """Fetch Overpass JSON with endpoint fallback."""
    last_exc = None
    data = urllib.parse.urlencode({'data': query}).encode('utf-8')
    for url in OVERPASS_URLS:
        try:
            if feedback:
                feedback.pushInfo('Contacting Overpass endpoint: {}'.format(url))
            req = urllib.request.Request(
                url,
                data=data,
                headers={'User-Agent': 'OpenGeoEnrich-QGIS/1.0 (+https://github.com/mahmoodirfan/OpenGeoEnrich)'},
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=int(timeout) + 10) as resp:  # nosec B310 - plugin only opens fixed HTTPS public open-data endpoints
                raw = resp.read().decode('utf-8')
            return json.loads(raw)
        except Exception as exc:
            last_exc = exc
            if feedback:
                feedback.pushWarning('Overpass endpoint failed: {} ({})'.format(url, exc))
    raise RuntimeError('All Overpass endpoints failed. Last error: {}'.format(last_exc))


def _tag_value(tags, key):
    try:
        return tags.get(key)
    except Exception:
        return None


def _guess_facility_category(tags):
    if not tags:
        return 'poi'
    amenity = tags.get('amenity', '')
    shop = tags.get('shop', '')
    healthcare = tags.get('healthcare', '')
    man_made = tags.get('man_made', '')
    if amenity in ('school', 'college', 'university', 'kindergarten'):
        return 'school'
    if amenity in ('hospital', 'clinic', 'doctors', 'pharmacy', 'dentist') or healthcare:
        return 'health'
    if amenity == 'marketplace' or shop:
        return 'market'
    if amenity in ('drinking_water', 'water_point') or man_made == 'water_well' or tags.get('waterway'):
        return 'water'
    return amenity or shop or healthcare or 'poi'


def _safe_json_tags(tags):
    try:
        txt = json.dumps(tags or {}, ensure_ascii=False, sort_keys=True)
        return txt[:240]
    except Exception:
        return ''


def _write_layer_to_gpkg(layer, gpkg_path, layer_name):
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = 'GPKG'
    options.layerName = layer_name
    if os.path.exists(gpkg_path):
        options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer
    else:
        options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer,
        gpkg_path,
        QgsProject.instance().transformContext(),
        options,
    )
    # QGIS versions differ: some return (err, msg), others return
    # (err, msg, newFilename, newLayer). Handle both safely.
    err = result[0] if isinstance(result, tuple) else result
    msg = result[1] if isinstance(result, tuple) and len(result) > 1 else ''
    if err != QgsVectorFileWriter.NoError:
        raise RuntimeError('Could not write {} to {}: {}'.format(layer_name, gpkg_path, msg))


def overpass_roads_to_gpkg(data, gpkg_path, layer_name='roads'):
    layer = QgsVectorLayer('LineString?crs=EPSG:4326', layer_name, 'memory')
    pr = layer.dataProvider()
    pr.addAttributes([
        QgsField('osm_id', QVariant.String),
        QgsField('osm_type', QVariant.String),
        QgsField('highway', QVariant.String),
        QgsField('name', QVariant.String),
        QgsField('surface', QVariant.String),
        QgsField('tags_json', QVariant.String),
    ])
    layer.updateFields()
    feats = []
    for el in data.get('elements', []):
        if el.get('type') != 'way' or not el.get('geometry'):
            continue
        pts = [QgsPointXY(float(p['lon']), float(p['lat'])) for p in el.get('geometry', []) if 'lon' in p and 'lat' in p]
        if len(pts) < 2:
            continue
        tags = el.get('tags', {}) or {}
        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromPolylineXY(pts))
        f.setAttributes([
            str(el.get('id', '')),
            str(el.get('type', '')),
            _tag_value(tags, 'highway'),
            _tag_value(tags, 'name'),
            _tag_value(tags, 'surface'),
            _safe_json_tags(tags),
        ])
        feats.append(f)
    pr.addFeatures(feats)
    layer.updateExtents()
    _write_layer_to_gpkg(layer, gpkg_path, layer_name)
    return len(feats)


def overpass_facilities_to_gpkg(data, gpkg_path, layer_name='facilities'):
    layer = QgsVectorLayer('Point?crs=EPSG:4326', layer_name, 'memory')
    pr = layer.dataProvider()
    pr.addAttributes([
        QgsField('osm_id', QVariant.String),
        QgsField('osm_type', QVariant.String),
        QgsField('category', QVariant.String),
        QgsField('amenity', QVariant.String),
        QgsField('shop', QVariant.String),
        QgsField('healthcare', QVariant.String),
        QgsField('name', QVariant.String),
        QgsField('tags_json', QVariant.String),
    ])
    layer.updateFields()
    feats = []
    for el in data.get('elements', []):
        lon = lat = None
        if el.get('type') == 'node' and 'lon' in el and 'lat' in el:
            lon, lat = el.get('lon'), el.get('lat')
        elif 'center' in el:
            lon, lat = el['center'].get('lon'), el['center'].get('lat')
        elif el.get('geometry'):
            # Fallback centroid approximation for geometries without center.
            coords = [(p.get('lon'), p.get('lat')) for p in el.get('geometry', []) if 'lon' in p and 'lat' in p]
            if coords:
                lon = sum(c[0] for c in coords) / len(coords)
                lat = sum(c[1] for c in coords) / len(coords)
        if lon is None or lat is None:
            continue
        tags = el.get('tags', {}) or {}
        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(float(lon), float(lat))))
        f.setAttributes([
            str(el.get('id', '')),
            str(el.get('type', '')),
            _guess_facility_category(tags),
            _tag_value(tags, 'amenity'),
            _tag_value(tags, 'shop'),
            _tag_value(tags, 'healthcare'),
            _tag_value(tags, 'name'),
            _safe_json_tags(tags),
        ])
        feats.append(f)
    pr.addFeatures(feats)
    layer.updateExtents()
    _write_layer_to_gpkg(layer, gpkg_path, layer_name)
    return len(feats)


def write_manifest(path, payload):
    ensure_folder(os.path.dirname(path))
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def read_manifest(path):
    """Read a previous OpenGeoEnrich manifest if it exists."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _vector_feature_count(path, layer_name):
    """Return feature count for a GeoPackage layer, or None if unavailable."""
    if not path or not os.path.exists(path):
        return None
    lyr = QgsVectorLayer(path + '|layername=' + layer_name, layer_name, 'ogr')
    if lyr.isValid():
        return int(lyr.featureCount())
    return None


def _cache_matches(manifest, cache_key, dataset_name, path):
    """Only reuse cache when AOI/options match the current cache key."""
    if not manifest or manifest.get('cache_key') != cache_key:
        return False
    outputs = manifest.get('outputs', {}) if isinstance(manifest, dict) else {}
    if dataset_name not in outputs:
        return False
    return bool(path and os.path.exists(path))


def prepare_osm_data(aoi_layer, output_folder, fetch_roads=True, fetch_facilities=True,
                     facility_categories=DEFAULT_FACILITY_CATEGORIES, custom_tags='',
                     timeout=180, use_cache=True, feedback=None):
    """Download/cache OSM roads and facilities for an AOI."""
    ensure_folder(output_folder)
    bbox = bbox_from_layer_wgs84(aoi_layer)
    width = bbox[3] - bbox[1]
    height = bbox[2] - bbox[0]
    if feedback:
        feedback.pushInfo('AOI WGS84 bbox: south={:.6f}, west={:.6f}, north={:.6f}, east={:.6f}'.format(*bbox))
        if width * height > 4.0:
            feedback.pushWarning('Large AOI detected. Overpass may timeout. Start with a smaller area if the request fails.')

    cache_key = bbox_hash(bbox, '{}|{}|{}|{}'.format(fetch_roads, fetch_facilities, facility_categories, custom_tags))
    roads_path = os.path.join(output_folder, 'roads.gpkg')
    facilities_path = os.path.join(output_folder, 'facilities.gpkg')
    manifest_path = os.path.join(output_folder, 'manifest.json')
    previous_manifest = read_manifest(manifest_path)

    manifest = {
        'tool': 'OpenGeoEnrich',
        'mode': 'OSM auto-data preparation',
        'created_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'cache_key': cache_key,
        'bbox_wgs84': {'south': bbox[0], 'west': bbox[1], 'north': bbox[2], 'east': bbox[3]},
        'sources': {},
        'outputs': {},
        'warnings': [],
    }

    if fetch_roads:
        if use_cache and _cache_matches(previous_manifest, cache_key, 'roads', roads_path):
            n = _vector_feature_count(roads_path, 'roads')
            if feedback:
                feedback.pushInfo('Using cached OSM roads for matching AOI/options: {} ({} features)'.format(roads_path, n if n is not None else 'unknown'))
            manifest['sources']['roads'] = {
                'source': 'OpenStreetMap Overpass API',
                'status': 'cached',
                'features': n,
            }
        else:
            if use_cache and os.path.exists(roads_path) and previous_manifest.get('cache_key') != cache_key and feedback:
                feedback.pushInfo('Existing roads.gpkg found, but AOI/options changed. Re-downloading roads.')
            if feedback:
                feedback.pushInfo('Downloading OSM roads from Overpass...')
            q = build_roads_query(bbox, timeout)
            data = fetch_overpass(q, timeout, feedback)
            n = overpass_roads_to_gpkg(data, roads_path, 'roads')
            if feedback:
                feedback.pushInfo('OSM roads saved: {} features.'.format(n))
            manifest['sources']['roads'] = {'source': 'OpenStreetMap Overpass API', 'status': 'downloaded', 'features': n}
        manifest['outputs']['roads'] = roads_path

    if fetch_facilities:
        if use_cache and _cache_matches(previous_manifest, cache_key, 'facilities', facilities_path):
            n = _vector_feature_count(facilities_path, 'facilities')
            if feedback:
                feedback.pushInfo('Using cached OSM facilities for matching AOI/options: {} ({} features)'.format(facilities_path, n if n is not None else 'unknown'))
            manifest['sources']['facilities'] = {
                'source': 'OpenStreetMap Overpass API',
                'status': 'cached',
                'features': n,
                'categories': facility_categories,
                'custom_tags': custom_tags,
            }
        else:
            if use_cache and os.path.exists(facilities_path) and previous_manifest.get('cache_key') != cache_key and feedback:
                feedback.pushInfo('Existing facilities.gpkg found, but AOI/options changed. Re-downloading facilities.')
            if feedback:
                feedback.pushInfo('Downloading OSM facilities/POIs from Overpass...')
            q = build_facilities_query(bbox, timeout, facility_categories, custom_tags)
            data = fetch_overpass(q, timeout, feedback)
            n = overpass_facilities_to_gpkg(data, facilities_path, 'facilities')
            if feedback:
                feedback.pushInfo('OSM facilities saved: {} features.'.format(n))
            manifest['sources']['facilities'] = {
                'source': 'OpenStreetMap Overpass API',
                'status': 'downloaded',
                'features': n,
                'categories': facility_categories,
                'custom_tags': custom_tags,
            }
        manifest['outputs']['facilities'] = facilities_path

    write_manifest(manifest_path, manifest)
    if feedback:
        feedback.pushInfo('Manifest written: {}'.format(manifest_path))
    return {'roads': roads_path if fetch_roads else '', 'facilities': facilities_path if fetch_facilities else '', 'manifest': manifest_path, 'folder': output_folder}


# -----------------------------------------------------------------------------
# WorldPop helpers
# -----------------------------------------------------------------------------

WORLDPOP_BASE = 'https://data.worldpop.org/GIS/Population'


def _worldpop_model_parts(model):
    m = (model or 'constrained').strip().lower()
    if m.startswith('un') or m == 'uc':
        return 'unconstrained', 'UC'
    return 'constrained', 'CN'


def build_worldpop_candidate_urls(iso3, year, model='constrained'):
    """Return candidate WorldPop GeoTIFF URLs.

    Priority is Global 2 (2015-2030, R2024B/R2024A) where available,
    with Global 1 (2000-2020) as a legacy fallback for older years.
    """
    iso3_u = (iso3 or '').strip().upper()
    iso3_l = iso3_u.lower()
    y = int(year)
    subdir, suffix = _worldpop_model_parts(model)
    urls = []
    if 2015 <= y <= 2030:
        urls.append('{base}/Global_2015_2030/R2024B/{year}/{iso3}/v1/100m/{subdir}/{iso_l}_pop_{year}_{suffix}_100m_R2024B_v1.tif'.format(
            base=WORLDPOP_BASE, year=y, iso3=iso3_u, iso_l=iso3_l, subdir=subdir, suffix=suffix))
        urls.append('{base}/Global_2015_2030/R2024A/{year}/{iso3}/v1/100m/{subdir}/{iso_l}_pop_{year}_{suffix}_100m_R2024A_v1.tif'.format(
            base=WORLDPOP_BASE, year=y, iso3=iso3_u, iso_l=iso3_l, subdir=subdir, suffix=suffix))
        if suffix != 'UC':
            urls.append('{base}/Global_2015_2030/R2024A/{year}/{iso3}/v1/100m/unconstrained/{iso_l}_pop_{year}_UC_100m_R2024A_v1.tif'.format(
                base=WORLDPOP_BASE, year=y, iso3=iso3_u, iso_l=iso3_l))
    if 2000 <= y <= 2020:
        urls.extend([
            '{base}/Global_2000_2020/{year}/{iso3}/{iso_l}_ppp_{year}.tif'.format(base=WORLDPOP_BASE, year=y, iso3=iso3_u, iso_l=iso3_l),
            '{base}/Global_2000_2020/{year}/{iso3}/{iso_l}_ppp_{year}_UNadj.tif'.format(base=WORLDPOP_BASE, year=y, iso3=iso3_u, iso_l=iso3_l),
            '{base}/Global_2000_2020/{year}/{iso3}/{iso_l}_ppp_{year}_1km_Aggregated.tif'.format(base=WORLDPOP_BASE, year=y, iso3=iso3_u, iso_l=iso3_l),
        ])
    return urls


def _remote_file_size(url, timeout=20):
    """Return remote file size in bytes if available, otherwise None."""
    try:
        req = urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'OpenGeoEnrich-QGIS/1.0 (+https://github.com/mahmoodirfan/OpenGeoEnrich)'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - plugin only opens fixed HTTPS public open-data endpoints
            length = resp.headers.get('Content-Length')
            if length:
                return int(length)
    except Exception:
        try:
            req = urllib.request.Request(url, headers={'Range': 'bytes=0-0', 'User-Agent': 'OpenGeoEnrich-QGIS/1.0 (+https://github.com/mahmoodirfan/OpenGeoEnrich)'})
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - plugin only opens fixed HTTPS public open-data endpoints
                cr = resp.headers.get('Content-Range')
                if cr and '/' in cr:
                    return int(cr.rsplit('/', 1)[-1])
                length = resp.headers.get('Content-Length')
                if length:
                    return int(length)
        except Exception:
            return None
    return None


def _format_mb(nbytes):
    if nbytes is None:
        return 'unknown size'
    return '{:.1f} MB'.format(float(nbytes) / (1024.0 * 1024.0))


def _download_with_progress(url, dst, timeout=60, feedback=None, progress_start=20, progress_end=80):
    ensure_folder(os.path.dirname(dst))
    req = urllib.request.Request(url, headers={'User-Agent': 'OpenGeoEnrich-QGIS/1.0 (+https://github.com/mahmoodirfan/OpenGeoEnrich)'})
    with urllib.request.urlopen(req, timeout=max(60, int(timeout))) as resp:  # nosec B310 - plugin only opens fixed HTTPS public open-data endpoints
        total = resp.headers.get('Content-Length')
        total = int(total) if total else None
        if feedback:
            feedback.pushInfo('Downloading WorldPop raster: {}'.format(url))
            feedback.pushInfo('Download size: {}'.format(_format_mb(total)))
        tmp = dst + '.part'
        done = 0
        last_pct = -1
        with open(tmp, 'wb') as f:
            while True:
                if feedback and feedback.isCanceled():
                    raise RuntimeError('Download cancelled by user.')
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total and feedback:
                    pct = int(progress_start + (progress_end - progress_start) * min(1.0, float(done) / float(total)))
                    if pct != last_pct:
                        feedback.setProgress(pct)
                        last_pct = pct
        if os.path.exists(dst):
            os.remove(dst)
        os.replace(tmp, dst)
    return dst


def _probe_worldpop_url(urls, timeout=20, feedback=None):
    for url in urls:
        size = _remote_file_size(url, timeout=timeout)
        if size is not None:
            if feedback:
                feedback.pushInfo('WorldPop candidate available: {} ({})'.format(url, _format_mb(size)))
            return url, size
        elif feedback:
            feedback.pushInfo('WorldPop candidate unavailable or size unknown: {}'.format(url))
    return None, None


def _clip_raster_to_bbox(input_raster, output_raster, bbox, feedback=None):
    """Clip raster to AOI WGS84 bbox using GDAL if available."""
    if processing is None:
        shutil.copyfile(input_raster, output_raster)
        return False, 'processing module unavailable; copied full country raster'
    try:
        s, w, n, e = bbox
        extent = '{},{},{},{} [EPSG:4326]'.format(w, e, s, n)
        params = {
            'INPUT': input_raster,
            'PROJWIN': extent,
            'NODATA': None,
            'OPTIONS': '',
            'DATA_TYPE': 0,
            'EXTRA': '',
            'OUTPUT': output_raster,
        }
        processing.run('gdal:cliprasterbyextent', params, feedback=feedback)
        if os.path.exists(output_raster):
            return True, 'clipped to AOI bounding box'
    except Exception as exc:
        try:
            shutil.copyfile(input_raster, output_raster)
        except Exception:
            pass
        return False, 'clip failed; copied full country raster ({})'.format(exc)
    return False, 'clip did not create output; full raster retained'


def prepare_worldpop_population(aoi_layer, output_folder, iso3='PAK', year=2024,
                                model='constrained', use_cache=True, timeout=60,
                                feedback=None):
    """Download/cache a WorldPop country raster and clip it to the AOI bbox."""
    ensure_folder(output_folder)
    cache_dir = os.path.join(output_folder, '_cache', 'worldpop')
    ensure_folder(cache_dir)
    bbox = bbox_from_layer_wgs84(aoi_layer)
    iso3_u = (iso3 or '').strip().upper()
    if len(iso3_u) != 3:
        raise ValueError('WorldPop country code must be a 3-letter ISO3 code such as PAK.')
    year = int(year)
    if year < 2000 or year > 2030:
        raise ValueError('WorldPop year must be between 2000 and 2030. Global 2 coverage is 2015-2030; legacy Global 1 coverage is 2000-2020.')
    model_name, suffix = _worldpop_model_parts(model)
    cache_key = '{}_{}_{}'.format(iso3_u, year, suffix)
    cached_country = os.path.join(cache_dir, '{}.tif'.format(cache_key.lower()))
    pop_out = os.path.join(output_folder, 'population.tif')
    manifest_path = os.path.join(output_folder, 'manifest.json')
    manifest = read_manifest(manifest_path)
    if not manifest:
        manifest = {
            'tool': 'OpenGeoEnrich',
            'mode': 'Open enrichment data preparation',
            'created_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'bbox_wgs84': {'south': bbox[0], 'west': bbox[1], 'north': bbox[2], 'east': bbox[3]},
            'sources': {},
            'outputs': {},
            'warnings': [],
        }
    urls = build_worldpop_candidate_urls(iso3_u, year, model_name)
    if not urls:
        raise RuntimeError('No WorldPop URL candidates available for {} {}.'.format(iso3_u, year))
    source_url = None
    size = None
    if use_cache and os.path.exists(cached_country):
        if feedback:
            feedback.pushInfo('Using cached WorldPop country raster: {}'.format(cached_country))
        source_url = manifest.get('sources', {}).get('population', {}).get('url', 'cached')
        size = os.path.getsize(cached_country)
    else:
        if feedback:
            feedback.pushInfo('Checking WorldPop download candidates for {} {}, model={}...'.format(iso3_u, year, model_name))
        source_url, size = _probe_worldpop_url(urls, timeout=20, feedback=feedback)
        if not source_url:
            raise RuntimeError('Could not find an available WorldPop GeoTIFF for {} {}. Tried {} candidate URL(s).'.format(iso3_u, year, len(urls)))
        if feedback:
            feedback.pushWarning('WorldPop country rasters can be large. Selected file size: {}. The first download may take several minutes on slow connections.'.format(_format_mb(size)))
        _download_with_progress(source_url, cached_country, timeout=max(300, int(timeout)), feedback=feedback, progress_start=35, progress_end=75)
    if feedback:
        feedback.pushInfo('Preparing clipped population.tif for the study area...')
    clipped, clip_status = _clip_raster_to_bbox(cached_country, pop_out, bbox, feedback)
    if feedback:
        feedback.pushInfo('WorldPop output: {}'.format(pop_out))
        feedback.pushInfo('WorldPop clip status: {}'.format(clip_status))
    manifest.setdefault('sources', {})['population'] = {
        'source': 'WorldPop',
        'status': 'cached' if source_url == 'cached' else 'downloaded_or_prepared',
        'url': source_url,
        'iso3': iso3_u,
        'year': year,
        'model': model_name,
        'size_bytes': size,
        'clip_status': clip_status,
        'note': 'WorldPop values are modelled estimates; units are people per pixel for count rasters.',
    }
    manifest.setdefault('outputs', {})['population'] = pop_out
    manifest.setdefault('warnings', [])
    if not clipped:
        manifest['warnings'].append('WorldPop raster clipping was not fully successful: {}'.format(clip_status))
    write_manifest(manifest_path, manifest)
    return {'population': pop_out, 'manifest': manifest_path, 'folder': output_folder, 'url': source_url, 'size_bytes': size, 'clip_status': clip_status}

# -----------------------------------------------------------------------------
# Copernicus DEM helpers
# -----------------------------------------------------------------------------

COPERNICUS_DEM_30M_BASE = 'https://copernicus-dem-30m.s3.amazonaws.com'


def _deg_tile_name(lat_deg, lon_deg):
    """Return Copernicus DEM 1-degree tile id for integer SW corner."""
    lat_prefix = 'N' if lat_deg >= 0 else 'S'
    lon_prefix = 'E' if lon_deg >= 0 else 'W'
    lat_txt = '{}{:02d}_00'.format(lat_prefix, abs(int(lat_deg)))
    lon_txt = '{}{:03d}_00'.format(lon_prefix, abs(int(lon_deg)))
    return 'Copernicus_DSM_COG_10_{}_{}_DEM'.format(lat_txt, lon_txt)


def build_copernicus_dem_urls_for_bbox(bbox):
    """Build candidate 30m Copernicus DEM tile URLs intersecting a WGS84 bbox."""
    import math
    s, w, n, e = bbox
    # Tile uses integer degree SW corner. Include all 1-degree cells touched by bbox.
    lat0 = int(math.floor(s))
    lat1 = int(math.ceil(n)) - 1
    lon0 = int(math.floor(w))
    lon1 = int(math.ceil(e)) - 1
    tiles = []
    for lat in range(lat0, lat1 + 1):
        for lon in range(lon0, lon1 + 1):
            tile_id = _deg_tile_name(lat, lon)
            url = '{base}/{tile}/{tile}.tif'.format(base=COPERNICUS_DEM_30M_BASE, tile=tile_id)
            tiles.append((tile_id, url))
    return tiles


def _url_available(url, timeout=20):
    try:
        req = urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'OpenGeoEnrich-QGIS/1.0 (+https://github.com/mahmoodirfan/OpenGeoEnrich)'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - plugin only opens fixed HTTPS public open-data endpoints
            return 200 <= getattr(resp, 'status', 200) < 400
    except Exception:
        # Some S3-compatible endpoints are awkward with HEAD; try a one-byte range.
        try:
            req = urllib.request.Request(url, headers={'Range': 'bytes=0-0', 'User-Agent': 'OpenGeoEnrich-QGIS/1.0 (+https://github.com/mahmoodirfan/OpenGeoEnrich)'})
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - plugin only opens fixed HTTPS public open-data endpoints
                return 200 <= getattr(resp, 'status', 200) < 400
        except Exception:
            return False


def _download_generic_with_progress(url, dst, timeout=300, feedback=None, label='file', progress_start=10, progress_end=80):
    ensure_folder(os.path.dirname(dst))
    req = urllib.request.Request(url, headers={'User-Agent': 'OpenGeoEnrich-QGIS/1.0 (+https://github.com/mahmoodirfan/OpenGeoEnrich)'})
    with urllib.request.urlopen(req, timeout=max(60, int(timeout))) as resp:  # nosec B310 - plugin only opens fixed HTTPS public open-data endpoints
        total = resp.headers.get('Content-Length')
        total = int(total) if total else None
        if feedback:
            feedback.pushInfo('Downloading {}: {}'.format(label, url))
            feedback.pushInfo('{} download size: {}'.format(label, _format_mb(total)))
        tmp = dst + '.part'
        done = 0
        last_pct = -1
        with open(tmp, 'wb') as f:
            while True:
                if feedback and feedback.isCanceled():
                    raise RuntimeError('Download cancelled by user.')
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total and feedback:
                    pct = int(progress_start + (progress_end - progress_start) * min(1.0, float(done) / float(total)))
                    if pct != last_pct:
                        feedback.setProgress(pct)
                        last_pct = pct
        if os.path.exists(dst):
            os.remove(dst)
        os.replace(tmp, dst)
    return dst


def _clip_or_merge_rasters_to_bbox(input_rasters, output_raster, bbox, feedback=None, raster_kind='dem'):
    """Merge multiple rasters if needed and clip to AOI WGS84 bbox.

    raster_kind controls GDAL output typing. DEM must remain Float32; ESA
    WorldCover is categorical Byte. Keeping these separate avoids silent DEM
    corruption and avoids color-table export warnings for categorical land cover.
    """
    if not input_rasters:
        raise RuntimeError('No raster tiles were available for this AOI.')
    if processing is None:
        # Best-effort fallback: copy the first tile if processing is unavailable.
        shutil.copyfile(input_rasters[0], output_raster)
        return False, 'processing module unavailable; copied first raster tile only'
    ensure_folder(os.path.dirname(output_raster))
    tmp_dir = os.path.join(os.path.dirname(output_raster), '_tmp_dem')
    ensure_folder(tmp_dir)
    merged = os.path.join(tmp_dir, '{}_merged.tif'.format(raster_kind))

    if raster_kind == 'landcover':
        label = 'ESA WorldCover'
        # WorldCover class codes are categorical values 10-100; Byte preserves
        # the class codes and avoids GeoTIFF color-table errors caused by Int32.
        merge_data_type = 1  # Byte for gdal:merge
        clip_data_type = 1   # Byte for gdal:cliprasterbyextent
    else:
        label = 'DEM'
        # QGIS GDAL wrappers use different enum mappings for merge and clip.
        # In tested QGIS 3.44, merge DATA_TYPE=5 logs -ot Float32, while clip
        # DATA_TYPE=6 logs -ot Float32. This avoids Byte/Int32 DEM outputs.
        merge_data_type = 5
        clip_data_type = 6

    try:
        s, w, n, e = bbox
        extent = '{},{},{},{} [EPSG:4326]'.format(w, e, s, n)
        src_for_clip = input_rasters[0]
        if len(input_rasters) > 1:
            if feedback:
                feedback.pushInfo('Merging {} {} tile(s)...'.format(len(input_rasters), label))
            processing.run('gdal:merge', {
                'INPUT': input_rasters,
                'PCT': False,
                'SEPARATE': False,
                'NODATA_INPUT': None,
                'NODATA_OUTPUT': None,
                'OPTIONS': '',
                'EXTRA': '',
                'DATA_TYPE': merge_data_type,
                'OUTPUT': merged,
            }, feedback=feedback)
            src_for_clip = merged
        if feedback:
            feedback.pushInfo('Clipping {} to AOI bounding box...'.format(label))
        processing.run('gdal:cliprasterbyextent', {
            'INPUT': src_for_clip,
            'PROJWIN': extent,
            'NODATA': None,
            'OPTIONS': '',
            'DATA_TYPE': clip_data_type,
            'EXTRA': '',
            'OUTPUT': output_raster,
        }, feedback=feedback)
        if os.path.exists(output_raster):
            return True, 'merged/clipped to AOI bounding box' if len(input_rasters) > 1 else 'clipped to AOI bounding box'
    except Exception as exc:
        try:
            shutil.copyfile(input_rasters[0], output_raster)
        except Exception:
            pass
        return False, '{} clip/merge failed; copied first tile only ({})'.format(label, exc)
    return False, '{} clip/merge did not create output'.format(label)


def _derive_dem_products(dem_path, slope_path, tri_path, feedback=None):
    """Create slope and terrain-ruggedness rasters from DEM.

    Copernicus DEM COG tiles are distributed in geographic coordinates. GDAL's
    slope algorithm needs a metre-per-horizontal-unit scale when the DEM units
    are degrees; otherwise slopes become artificially close to 90 degrees.
    """
    outputs = {}
    if processing is None:
        return outputs, ['processing module unavailable; slope/TRI not derived']
    warnings = []

    # The auto-downloaded Copernicus DEM is in EPSG:4326 degrees. Use metres per
    # degree as the horizontal scale so slope is reported in real degrees rather
    # than near-vertical artefacts. This is intentionally conservative and avoids
    # forcing a reprojection step during data preparation.
    slope_scale = 111120.0
    try:
        if feedback:
            feedback.pushInfo('Deriving slope raster from DEM...')
            feedback.pushInfo('Using GDAL slope scale = {:.1f} because Copernicus DEM tiles are geographic degrees.'.format(slope_scale))
        processing.run('gdal:slope', {
            'INPUT': dem_path,
            'BAND': 1,
            'SCALE': slope_scale,
            'AS_PERCENT': False,
            'COMPUTE_EDGES': True,
            'ZEVENBERGEN': False,
            'OPTIONS': '',
            'EXTRA': '',
            'OUTPUT': slope_path,
        }, feedback=feedback)
        if os.path.exists(slope_path):
            outputs['slope'] = slope_path
    except Exception as exc:
        warnings.append('Slope derivation failed: {}'.format(exc))
    # Try GDAL TRI first, then roughness fallback if that algorithm is unavailable.
    try:
        if feedback:
            feedback.pushInfo('Deriving terrain ruggedness raster from DEM...')
        processing.run('gdal:tri', {
            'INPUT': dem_path,
            'BAND': 1,
            'COMPUTE_EDGES': True,
            'OPTIONS': '',
            'EXTRA': '',
            'OUTPUT': tri_path,
        }, feedback=feedback)
        if os.path.exists(tri_path):
            outputs['tri'] = tri_path
    except Exception as exc_tri:
        try:
            if feedback:
                feedback.pushWarning('gdal:tri failed; trying gdal:roughness fallback: {}'.format(exc_tri))
            processing.run('gdal:roughness', {
                'INPUT': dem_path,
                'BAND': 1,
                'COMPUTE_EDGES': True,
                'OPTIONS': '',
                'EXTRA': '',
                'OUTPUT': tri_path,
            }, feedback=feedback)
            if os.path.exists(tri_path):
                outputs['tri'] = tri_path
                warnings.append('Used GDAL roughness as TRI fallback.')
        except Exception as exc_rough:
            warnings.append('TRI/roughness derivation failed: {}; {}'.format(exc_tri, exc_rough))
    return outputs, warnings


def prepare_copernicus_dem(aoi_layer, output_folder, use_cache=True, timeout=300, feedback=None):
    """Download/cache Copernicus DEM GLO-30 COG tiles and prepare DEM, slope, TRI rasters."""
    ensure_folder(output_folder)
    cache_dir = os.path.join(output_folder, '_cache', 'copernicus_dem_30m')
    ensure_folder(cache_dir)
    bbox = bbox_from_layer_wgs84(aoi_layer)
    dem_out = os.path.join(output_folder, 'dem.tif')
    slope_out = os.path.join(output_folder, 'slope.tif')
    tri_out = os.path.join(output_folder, 'tri.tif')
    manifest_path = os.path.join(output_folder, 'manifest.json')
    manifest = read_manifest(manifest_path) or {
        'tool': 'OpenGeoEnrich',
        'mode': 'Open enrichment data preparation',
        'created_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'bbox_wgs84': {'south': bbox[0], 'west': bbox[1], 'north': bbox[2], 'east': bbox[3]},
        'sources': {},
        'outputs': {},
        'warnings': [],
    }
    tiles = build_copernicus_dem_urls_for_bbox(bbox)
    if feedback:
        feedback.pushInfo('Copernicus DEM tiles intersecting AOI bbox: {}'.format(len(tiles)))
        feedback.pushWarning('Copernicus DEM tiles are typically 20-80 MB each. First download may take time; cache reuse is recommended.')
    local_tiles = []
    downloaded_tiles = []
    for idx, (tile_id, url) in enumerate(tiles, start=1):
        if feedback and feedback.isCanceled():
            raise RuntimeError('DEM preparation cancelled by user.')
        local = os.path.join(cache_dir, tile_id + '.tif')
        if use_cache and os.path.exists(local):
            if feedback:
                feedback.pushInfo('Using cached Copernicus DEM tile {}/{}: {}'.format(idx, len(tiles), tile_id))
            local_tiles.append(local)
            continue
        if not _url_available(url, timeout=20):
            if feedback:
                feedback.pushWarning('Copernicus DEM tile unavailable or not found: {}'.format(tile_id))
            continue
        # Avoid wild progress jumps if many tiles: keep broad range 15-65.
        p0 = 15 + int(50.0 * (idx - 1) / max(len(tiles), 1))
        p1 = 15 + int(50.0 * idx / max(len(tiles), 1))
        _download_generic_with_progress(url, local, timeout=max(300, int(timeout)), feedback=feedback, label='Copernicus DEM tile {}'.format(tile_id), progress_start=p0, progress_end=p1)
        local_tiles.append(local)
        downloaded_tiles.append(tile_id)
    if not local_tiles:
        raise RuntimeError('No Copernicus DEM tiles could be downloaded/found for this AOI.')
    clipped, clip_status = _clip_or_merge_rasters_to_bbox(local_tiles, dem_out, bbox, feedback=feedback, raster_kind='dem')
    derived, deriv_warnings = _derive_dem_products(dem_out, slope_out, tri_out, feedback=feedback)
    if feedback:
        feedback.pushInfo('DEM output: {}'.format(dem_out))
        feedback.pushInfo('DEM preparation status: {}'.format(clip_status))
        if os.path.exists(slope_out):
            feedback.pushInfo('Slope output: {}'.format(slope_out))
        if os.path.exists(tri_out):
            feedback.pushInfo('Terrain ruggedness output: {}'.format(tri_out))
        for wmsg in deriv_warnings:
            feedback.pushWarning(wmsg)
    manifest.setdefault('sources', {})['dem'] = {
        'source': 'Copernicus DEM GLO-30 public COG tiles',
        'status': 'cached_or_downloaded',
        'tiles_requested': [t[0] for t in tiles],
        'tile_count_requested': len(tiles),
        'tiles_used': [os.path.splitext(os.path.basename(p))[0] for p in local_tiles],
        'tile_count_used': len(local_tiles),
        'tiles_downloaded_this_run': downloaded_tiles,
        'clip_status': clip_status,
        'note': 'DEM values are elevation in metres; slope is degrees; TRI/roughness is derived from DEM neighbourhood variation.',
    }
    manifest.setdefault('outputs', {})['dem'] = dem_out
    if os.path.exists(slope_out):
        manifest['outputs']['slope'] = slope_out
    if os.path.exists(tri_out):
        manifest['outputs']['tri'] = tri_out
    manifest.setdefault('warnings', [])
    if not clipped:
        manifest['warnings'].append('DEM raster clipping was not fully successful: {}'.format(clip_status))
    manifest['warnings'].extend(deriv_warnings)
    write_manifest(manifest_path, manifest)
    return {
        'dem': dem_out,
        'slope': slope_out if os.path.exists(slope_out) else '',
        'tri': tri_out if os.path.exists(tri_out) else '',
        'manifest': manifest_path,
        'folder': output_folder,
    }

# -----------------------------------------------------------------------------
# ESA WorldCover helpers
# -----------------------------------------------------------------------------

ESA_WORLDCOVER_BASE = 'https://esa-worldcover.s3.eu-central-1.amazonaws.com'


def _worldcover_version_for_year(year):
    y = int(year)
    if y == 2020:
        return 'v100'
    if y == 2021:
        return 'v200'
    raise ValueError('ESA WorldCover auto-download currently supports 2020 (v100) and 2021 (v200).')


def _worldcover_tile_origin(value):
    """Return the 3-degree WorldCover tile SW origin for a lat/lon value."""
    import math
    return int(math.floor(float(value) / 3.0) * 3)


def _worldcover_coord(lat_deg, lon_deg):
    lat_prefix = 'N' if lat_deg >= 0 else 'S'
    lon_prefix = 'E' if lon_deg >= 0 else 'W'
    return '{}{:02d}{}{:03d}'.format(lat_prefix, abs(int(lat_deg)), lon_prefix, abs(int(lon_deg)))


def build_worldcover_urls_for_bbox(bbox, year=2021):
    """Build ESA WorldCover 3x3-degree tile URLs intersecting a WGS84 bbox."""
    version = _worldcover_version_for_year(year)
    y = int(year)
    s, w, n, e = bbox
    # WorldCover tiles are 3 x 3 degree lat/lon COG tiles. Subtract a tiny
    # epsilon from max edge to avoid requesting the next tile when the AOI ends
    # exactly on a tile boundary.
    eps = 1e-9
    lat0 = _worldcover_tile_origin(s)
    lat1 = _worldcover_tile_origin(n - eps)
    lon0 = _worldcover_tile_origin(w)
    lon1 = _worldcover_tile_origin(e - eps)
    tiles = []
    for lat in range(lat0, lat1 + 1, 3):
        for lon in range(lon0, lon1 + 1, 3):
            coord = _worldcover_coord(lat, lon)
            fname = 'ESA_WorldCover_10m_{year}_{version}_{coord}_Map.tif'.format(year=y, version=version, coord=coord)
            url = '{base}/{version}/{year}/map/{fname}'.format(base=ESA_WORLDCOVER_BASE, version=version, year=y, fname=fname)
            tiles.append((fname.replace('.tif', ''), url))
    return tiles


def prepare_esa_worldcover(aoi_layer, output_folder, year=2021, use_cache=True, timeout=300, feedback=None):
    """Download/cache ESA WorldCover 10 m COG tiles and prepare landcover.tif."""
    ensure_folder(output_folder)
    cache_dir = os.path.join(output_folder, '_cache', 'esa_worldcover')
    ensure_folder(cache_dir)
    bbox = bbox_from_layer_wgs84(aoi_layer)
    version = _worldcover_version_for_year(year)
    lc_out = os.path.join(output_folder, 'landcover.tif')
    manifest_path = os.path.join(output_folder, 'manifest.json')
    manifest = read_manifest(manifest_path) or {
        'tool': 'OpenGeoEnrich',
        'mode': 'Open enrichment data preparation',
        'created_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'bbox_wgs84': {'south': bbox[0], 'west': bbox[1], 'north': bbox[2], 'east': bbox[3]},
        'sources': {},
        'outputs': {},
        'warnings': [],
    }
    cache_key = bbox_hash(bbox, 'esa_worldcover_{}_{}'.format(year, version))
    previous_lc = manifest.get('sources', {}).get('landcover', {}) if isinstance(manifest, dict) else {}
    if use_cache and os.path.exists(lc_out) and previous_lc.get('cache_key') == cache_key:
        if feedback:
            feedback.pushInfo('Using cached prepared ESA WorldCover landcover.tif for matching AOI/year: {}'.format(lc_out))
        return {'landcover': lc_out, 'manifest': manifest_path, 'folder': output_folder}

    tiles = build_worldcover_urls_for_bbox(bbox, year=year)
    if feedback:
        feedback.pushInfo('ESA WorldCover tiles intersecting AOI bbox: {}'.format(len(tiles)))
        feedback.pushWarning('ESA WorldCover tiles are 10 m COGs in 3x3-degree blocks. They can be large; cache reuse is recommended.')
    local_tiles = []
    downloaded_tiles = []
    missing_tiles = []
    for idx, (tile_id, url) in enumerate(tiles, start=1):
        if feedback and feedback.isCanceled():
            raise RuntimeError('ESA WorldCover preparation cancelled by user.')
        local = os.path.join(cache_dir, tile_id + '.tif')
        if use_cache and os.path.exists(local):
            if feedback:
                feedback.pushInfo('Using cached ESA WorldCover tile {}/{}: {}'.format(idx, len(tiles), tile_id))
            local_tiles.append(local)
            continue
        if not _url_available(url, timeout=20):
            missing_tiles.append(tile_id)
            if feedback:
                feedback.pushWarning('ESA WorldCover tile unavailable or not found: {}'.format(tile_id))
            continue
        p0 = 15 + int(55.0 * (idx - 1) / max(len(tiles), 1))
        p1 = 15 + int(55.0 * idx / max(len(tiles), 1))
        _download_generic_with_progress(
            url, local, timeout=max(300, int(timeout)), feedback=feedback,
            label='ESA WorldCover tile {}'.format(tile_id), progress_start=p0, progress_end=p1
        )
        local_tiles.append(local)
        downloaded_tiles.append(tile_id)
    if not local_tiles:
        raise RuntimeError('No ESA WorldCover tiles could be downloaded/found for this AOI.')
    clipped, clip_status = _clip_or_merge_rasters_to_bbox(local_tiles, lc_out, bbox, feedback=feedback, raster_kind='landcover')
    if feedback:
        feedback.pushInfo('ESA WorldCover output: {}'.format(lc_out))
        feedback.pushInfo('ESA WorldCover preparation status: {}'.format(clip_status))
        if missing_tiles:
            feedback.pushWarning('Some ESA WorldCover tiles were unavailable: {}'.format(', '.join(missing_tiles)))
    manifest.setdefault('sources', {})['landcover'] = {
        'source': 'ESA WorldCover 10 m',
        'status': 'cached_or_downloaded',
        'year': int(year),
        'version': version,
        'cache_key': cache_key,
        'tiles_requested': [t[0] for t in tiles],
        'tile_count_requested': len(tiles),
        'tiles_used': [os.path.splitext(os.path.basename(p))[0] for p in local_tiles],
        'tile_count_used': len(local_tiles),
        'tiles_downloaded_this_run': downloaded_tiles,
        'tiles_missing': missing_tiles,
        'clip_status': clip_status,
        'note': 'Class codes: 10 Tree cover, 20 Shrubland, 30 Grassland, 40 Cropland, 50 Built-up, 60 Bare/sparse, 70 Snow/ice, 80 Water, 90 Wetland, 95 Mangroves, 100 Moss/lichen.',
    }
    manifest.setdefault('outputs', {})['landcover'] = lc_out
    manifest.setdefault('warnings', [])
    if not clipped:
        manifest['warnings'].append('ESA WorldCover raster clipping was not fully successful: {}'.format(clip_status))
    if missing_tiles:
        manifest['warnings'].append('Some ESA WorldCover tiles were unavailable: {}'.format(', '.join(missing_tiles)))
    write_manifest(manifest_path, manifest)
    return {'landcover': lc_out, 'manifest': manifest_path, 'folder': output_folder}
