# -*- coding: utf-8 -*-
"""Core helper functions for OpenGeoEnrich."""

import csv
import math
import os
import re
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsSpatialIndex,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant


def safe_field_name(name, existing=None, max_len=24):
    """Create a safe and unique field name."""
    existing = set(existing or [])
    cleaned = re.sub(r'[^A-Za-z0-9_]+', '_', str(name)).strip('_')
    if not cleaned:
        cleaned = 'field'
    if cleaned[0].isdigit():
        cleaned = 'f_' + cleaned
    cleaned = cleaned[:max_len]
    base = cleaned
    i = 1
    while cleaned in existing:
        suffix = '_' + str(i)
        cleaned = base[: max_len - len(suffix)] + suffix
        i += 1
    existing.add(cleaned)
    return cleaned


def parse_lc_groups(group_text):
    """Parse user-supplied land-cover groups.

    Example:
    Urban=50;Cropland=10,20,30,40;Forest=60,70,80,90;Water=80
    """
    groups = []
    if not group_text or not str(group_text).strip():
        return groups
    parts = [p.strip() for p in str(group_text).split(';') if p.strip()]
    for part in parts:
        if '=' not in part:
            continue
        label, codes = part.split('=', 1)
        label = label.strip()
        code_values = []
        for c in codes.split(','):
            c = c.strip()
            if c == '':
                continue
            try:
                # Store codes as int-like strings because histogram fields are text names.
                code_values.append(str(int(float(c))))
            except Exception:
                code_values.append(c)
        if label and code_values:
            groups.append((label, set(code_values)))
    return groups


def create_working_polygon_layer(target_layer, buffer_distance, context, feedback):
    """Create polygon layer for enrichment zones and return mapping by OGE id.

    - polygon targets are copied directly;
    - point/line targets are buffered using the target layer CRS units.
    """
    crs_authid = target_layer.crs().authid()
    work = QgsVectorLayer('Polygon?crs={}'.format(crs_authid), 'opengeoenrich_zones', 'memory')
    provider = work.dataProvider()

    fields = QgsFields()
    fields.append(QgsField('__oge_id', QVariant.Int))
    provider.addAttributes(fields)
    work.updateFields()

    geom_type = QgsWkbTypes.geometryType(target_layer.wkbType())
    if geom_type in (QgsWkbTypes.PointGeometry, QgsWkbTypes.LineGeometry):
        if buffer_distance <= 0:
            raise ValueError('Buffer distance must be greater than zero for point or line target layers.')
        if target_layer.crs().isGeographic():
            feedback.pushWarning(
                'Target layer CRS is geographic. Buffer distance and vector distance outputs will be in CRS units. '
                'For meter-based buffers/distances, reproject the target layer to a projected CRS before running.'
            )

    features = []
    id_to_original = {}
    total = max(target_layer.featureCount(), 1)
    for idx, feat in enumerate(target_layer.getFeatures()):
        if feedback.isCanceled():
            break
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        if geom_type == QgsWkbTypes.PolygonGeometry:
            zone_geom = QgsGeometry(geom)
        else:
            zone_geom = geom.buffer(float(buffer_distance), 24)
        if zone_geom is None or zone_geom.isEmpty():
            continue
        out_feat = QgsFeature(work.fields())
        out_feat.setGeometry(zone_geom)
        out_feat['__oge_id'] = int(idx)
        features.append(out_feat)
        id_to_original[int(idx)] = feat
        if idx % 50 == 0:
            feedback.setProgress(int(10 * idx / total))

    provider.addFeatures(features)
    work.updateExtents()
    return work, id_to_original


def _extent_looks_lonlat(rect):
    """Return True when a layer extent numerically looks like lon/lat degrees."""
    try:
        return (
            -180.0 <= rect.xMinimum() <= 180.0 and
            -180.0 <= rect.xMaximum() <= 180.0 and
            -90.0 <= rect.yMinimum() <= 90.0 and
            -90.0 <= rect.yMaximum() <= 90.0
        )
    except Exception:
        return False


def _resolved_source_crs(source_layer, target_crs, feedback, label):
    """Resolve CRS and guard against common OSM GeoPackage CRS metadata problems.

    Prepared OSM layers should be EPSG:4326. In some QGIS/GDAL combinations,
    or after users manually save/load layers, the CRS metadata may be missing or
    may be inherited incorrectly. If the coordinate values are clearly lon/lat
    but the target is projected, force EPSG:4326 before transformation. This
    prevents huge false nearest-distance values such as ~3,800,000 m.
    """
    src_crs = source_layer.crs()
    extent = source_layer.extent()
    looks_lonlat = _extent_looks_lonlat(extent)

    if looks_lonlat and not target_crs.isGeographic():
        if (not src_crs.isValid()) or (src_crs == target_crs) or (not src_crs.isGeographic()):
            feedback.pushWarning(
                '{} layer coordinates look like longitude/latitude but CRS metadata is missing or inconsistent. '
                'Interpreting it as EPSG:4326 and transforming to the target CRS.'.format(label.capitalize())
            )
            return QgsCoordinateReferenceSystem('EPSG:4326')

    if not src_crs.isValid():
        feedback.pushWarning(
            '{} layer CRS is invalid/unknown. Distances may be wrong unless this layer is assigned the correct CRS.'.format(
                label.capitalize()
            )
        )
    return src_crs


def add_transformed_index(source_layer, target_crs, feedback, label='features'):
    """Build a spatial index and transformed geometry dictionary."""
    if source_layer is None:
        return None, {}

    src_crs = _resolved_source_crs(source_layer, target_crs, feedback, label)
    transform = None
    if src_crs.isValid() and src_crs != target_crs:
        transform = QgsCoordinateTransform(src_crs, target_crs, QgsProject.instance())
        feedback.pushInfo('Transforming {} from {} to {} for metric calculations.'.format(
            label, src_crs.authid() or 'unknown CRS', target_crs.authid() or 'target CRS'
        ))

    idx = QgsSpatialIndex()
    geoms = {}
    total = max(source_layer.featureCount(), 1)
    for i, feat in enumerate(source_layer.getFeatures()):
        if feedback.isCanceled():
            break
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        g = QgsGeometry(geom)
        if transform is not None:
            try:
                g.transform(transform)
            except Exception:
                continue
        f = QgsFeature()
        f.setId(int(feat.id()))
        f.setGeometry(g)
        idx.addFeature(f)
        geoms[int(feat.id())] = g
        if i > 0 and i % 1000 == 0:
            feedback.pushInfo('Indexed {} {}...'.format(i, label))
    feedback.pushInfo('Indexed {} {}.'.format(len(geoms), label))
    return idx, geoms


def compute_vector_enrichment(work_layer, roads_layer, facilities_layer, feedback):
    """Compute road length, nearest road, facility count, and nearest facility distance."""
    results = {}
    target_crs = work_layer.crs()
    if target_crs.isGeographic():
        feedback.pushWarning(
            'Working CRS is geographic. Road length and nearest distance are in CRS units, not meters. '
            'Use a projected CRS for metric outputs.'
        )

    road_index, road_geoms = add_transformed_index(roads_layer, target_crs, feedback, 'roads') if roads_layer else (None, {})
    fac_index, fac_geoms = add_transformed_index(facilities_layer, target_crs, feedback, 'facilities') if facilities_layer else (None, {})

    total = max(work_layer.featureCount(), 1)
    for i, feat in enumerate(work_layer.getFeatures()):
        if feedback.isCanceled():
            break
        oge_id = int(feat['__oge_id'])
        geom = feat.geometry()
        row = {}

        if road_index is not None:
            road_len = 0.0
            nearest_road = None
            candidates = road_index.intersects(geom.boundingBox())
            # length inside polygon/buffer
            for rid in candidates:
                rg = road_geoms.get(int(rid))
                if rg is None:
                    continue
                try:
                    inter = geom.intersection(rg)
                    if inter and not inter.isEmpty():
                        road_len += inter.length()
                    d = geom.distance(rg)
                    if nearest_road is None or d < nearest_road:
                        nearest_road = d
                except Exception:
                    continue
            # If no bbox candidate, use nearest-neighbour fallback from centroid bbox search.
            if nearest_road is None and road_geoms:
                centroid = geom.centroid().asPoint()
                near_ids = road_index.nearestNeighbor(centroid, 1)
                for rid in near_ids:
                    rg = road_geoms.get(int(rid))
                    if rg is not None:
                        nearest_road = geom.distance(rg)
                        break
            row['road_len_km'] = road_len / 1000.0
            row['near_road_m'] = nearest_road if nearest_road is not None else None

        if fac_index is not None:
            fac_count = 0
            nearest_fac = None
            candidates = fac_index.intersects(geom.boundingBox())
            for fid in candidates:
                fg = fac_geoms.get(int(fid))
                if fg is None:
                    continue
                try:
                    if geom.intersects(fg):
                        fac_count += 1
                    d = geom.distance(fg)
                    if nearest_fac is None or d < nearest_fac:
                        nearest_fac = d
                except Exception:
                    continue
            if nearest_fac is None and fac_geoms:
                centroid = geom.centroid().asPoint()
                near_ids = fac_index.nearestNeighbor(centroid, 1)
                for fid in near_ids:
                    fg = fac_geoms.get(int(fid))
                    if fg is not None:
                        nearest_fac = geom.distance(fg)
                        break
            row['facility_ct'] = int(fac_count)
            row['near_fac_m'] = nearest_fac if nearest_fac is not None else None

        results[oge_id] = row
        if i % 20 == 0:
            feedback.setProgress(60 + int(20 * i / total))
    return results


def extract_zone_stats(work_layer):
    """Read fields added to the working layer by zonal-statistics algorithms."""
    out = {}
    for feat in work_layer.getFeatures():
        oid = int(feat['__oge_id'])
        out[oid] = {field.name(): feat[field.name()] for field in work_layer.fields() if field.name() != '__oge_id'}
    return out


def write_csv(path, features, fields):
    """Write a CSV file from QgsFeature iterator."""
    if not path:
        return
    folder = os.path.dirname(path)
    if folder and not os.path.exists(folder):
        os.makedirs(folder, exist_ok=True)
    names = [f.name() for f in fields]
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(names)
        for feat in features:
            writer.writerow([feat[name] for name in names])


def write_html_report(path, target_name, feature_count, fields, features, inputs):
    """Write a compact HTML report for OpenGeoEnrich results."""
    if not path:
        return
    folder = os.path.dirname(path)
    if folder and not os.path.exists(folder):
        os.makedirs(folder, exist_ok=True)

    def esc(x):
        import html
        return html.escape('' if x is None else str(x))

    def as_float(v):
        try:
            if v in (None, ''):
                return None
            v = float(v)
            if math.isnan(v) or math.isinf(v):
                return None
            return v
        except Exception:
            return None

    def field_exists(name):
        return name in names

    def field_has_values(name):
        if name not in names:
            return False
        for feat in features:
            value = feat[name]
            if value is None:
                continue
            # Numeric fields: 0 is a real populated value, so as_float is enough.
            if as_float(value) is not None:
                return True
            # Text fields such as lc_majority_label should also be detected.
            try:
                txt = str(value).strip()
            except Exception:
                txt = ''
            if txt and txt.lower() not in ('null', 'none'):
                return True
        return False

    features = list(features)
    names = [f.name() for f in fields]

    sections = [
        ('Area / geometry', [
            ('oge_area_ha', 'Enrichment zone area in hectares'),
            ('oge_area_km2', 'Enrichment zone area in square kilometres'),
        ]),
        ('Population', [
            ('pop_sum', 'Estimated population total inside each zone'),
            ('pop_mean', 'Mean raster value inside each zone'),
            ('pop_dens_km2', 'Estimated population per square kilometre'),
        ]),
        ('Terrain / elevation', [
            ('dem_mean', 'Mean elevation'),
            ('dem_min', 'Minimum elevation'),
            ('dem_max', 'Maximum elevation'),
            ('slope_mean', 'Mean slope in degrees'),
            ('slope_min', 'Minimum slope in degrees'),
            ('slope_max', 'Maximum slope in degrees'),
            ('tri_mean', 'Mean terrain ruggedness index / roughness'),
            ('tri_min', 'Minimum terrain ruggedness index / roughness'),
            ('tri_max', 'Maximum terrain ruggedness index / roughness'),
        ]),
        ('Land cover', [
            ('lc_majority', 'Majority land-cover class code'),
            ('lc_majority_label', 'Majority land-cover class label, where known'),
            ('lc_variety', 'Number of land-cover classes represented'),
            ('natural_pct', 'Natural/semi-natural cover percentage: Tree + Shrubland + Grassland + Wetland + Mangroves + Moss/Lichen'),
            ('built_crop_pct', 'Built-up plus cropland percentage'),
        ]),
        ('Roads / linear access', [
            ('road_len_km', 'Road length inside each zone in kilometres'),
            ('road_dens_km_km2', 'Road length per square kilometre'),
            ('near_road_m', 'Distance to nearest road in metres'),
        ]),
        ('Facilities / POIs', [
            ('facility_ct', 'Number of facilities/POIs inside each zone'),
            ('fac_dens_km2', 'Facilities per square kilometre'),
            ('near_fac_m', 'Distance to nearest facility/POI in metres'),
        ]),
        ('Population-normalized access', [
            ('fac_per_10k_pop', 'Facilities per 10,000 people'),
            ('road_km_per_10k_pop', 'Road kilometres per 10,000 people'),
        ]),
    ]

    # Add any user-configured land-cover percentage/pixel fields to the Land cover section.
    dynamic_lc = []
    for n in names:
        if n.startswith('lc_') and (n.endswith('_pct') or n.endswith('_pix')) and n not in ('lc_majority', 'lc_majority_label', 'lc_variety'):
            desc = 'Land-cover group percentage' if n.endswith('_pct') else 'Land-cover group pixel count'
            dynamic_lc.append((n, desc))
    if dynamic_lc:
        for i, (title, items) in enumerate(sections):
            if title == 'Land cover':
                sections[i] = (title, items + dynamic_lc)
                break

    numeric_fields = [field for _section, items in sections for field, _label in items if field != 'lc_majority_label']
    summary_rows = []
    for name in numeric_fields:
        if name not in names:
            continue
        vals = [as_float(feat[name]) for feat in features]
        vals = [v for v in vals if v is not None]
        if vals:
            summary_rows.append((name, len(vals), min(vals), sum(vals) / len(vals), max(vals)))

    preview_fields = [n for n in names if n in (
        'site_id', 'site_type', 'TEHSIL', 'District', 'DISTRICT', 'oge_area_km2',
        'pop_sum', 'pop_dens_km2', 'dem_mean', 'slope_mean', 'tri_mean', 'lc_majority', 'lc_majority_label', 'natural_pct', 'built_crop_pct', 'road_len_km', 'road_dens_km_km2',
        'facility_ct', 'fac_dens_km2', 'fac_per_10k_pop', 'road_km_per_10k_pop'
    )]
    if not preview_fields:
        preview_fields = names[:10]

    with open(path, 'w', encoding='utf-8') as f:
        f.write('<!doctype html><html><head><meta charset="utf-8">')
        f.write('<title>OpenGeoEnrich Report</title>')
        f.write('<style>body{font-family:Arial,sans-serif;margin:28px;color:#222;line-height:1.35}h1{margin-bottom:4px}h2{margin-top:24px}h3{margin-top:18px}table{border-collapse:collapse;margin:14px 0;width:100%;font-size:13px}th,td{border:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}th{background:#f2f2f2}.small{color:#666;font-size:13px}.box{background:#f8f8f8;border:1px solid #ddd;padding:12px;margin:12px 0}.ok{color:#006400;font-weight:bold}.miss{color:#8a4b00;font-weight:bold}.note{background:#fff8e5;border:1px solid #e7d28a;padding:10px;margin:12px 0}.code{font-family:Consolas,monospace}</style>')
        f.write('</head><body>')
        f.write('<h1>OpenGeoEnrich Summary Report</h1>')
        f.write('<p class="small">Automatically generated by OpenGeoEnrich for QGIS.</p>')
        f.write('<div class="box"><b>Target layer:</b> {}<br><b>Features enriched:</b> {}</div>'.format(esc(target_name), esc(feature_count)))

        f.write('<h2>Inputs</h2><table><tr><th>Input</th><th>Layer / status</th></tr>')
        for k, v in inputs.items():
            status_class = 'miss' if str(v).lower() == 'not used' else 'ok'
            f.write('<tr><td>{}</td><td><span class="{}">{}</span></td></tr>'.format(esc(k), status_class, esc(v)))
        f.write('</table>')

        f.write('<h2>Indicator groups</h2>')
        f.write('<p class="small">This section separates the output fields by theme so users can quickly see which modules produced usable values.</p>')
        for title, items in sections:
            available = [(field, desc) for field, desc in items if field_exists(field)]
            if not available:
                continue
            f.write('<h3>{}</h3><table><tr><th>Field</th><th>Description</th><th>Status</th></tr>'.format(esc(title)))
            for field, desc in available:
                status = 'populated' if field_has_values(field) else 'empty / not calculated'
                cls = 'ok' if status == 'populated' else 'miss'
                f.write('<tr><td class="code">{}</td><td>{}</td><td><span class="{}">{}</span></td></tr>'.format(esc(field), esc(desc), cls, esc(status)))
            f.write('</table>')

        if summary_rows:
            f.write('<h2>Numeric summary</h2><table><tr><th>Field</th><th>Valid n</th><th>Min</th><th>Mean</th><th>Max</th></tr>')
            for row in summary_rows:
                f.write('<tr><td class="code">{}</td><td>{}</td><td>{:.3f}</td><td>{:.3f}</td><td>{:.3f}</td></tr>'.format(esc(row[0]), row[1], row[2], row[3], row[4]))
            f.write('</table>')

        f.write('<h2>Preview</h2><table><tr>')
        for n in preview_fields:
            f.write('<th>{}</th>'.format(esc(n)))
        f.write('</tr>')
        for feat in features[:25]:
            f.write('<tr>')
            for n in preview_fields:
                val = feat[n]
                if isinstance(val, float):
                    txt = '{:.3f}'.format(val)
                else:
                    txt = val
                f.write('<td>{}</td>'.format(esc(txt)))
            f.write('</tr>')
        f.write('</table>')

        f.write('<div class="note"><b>Interpretation notes</b><br>')
        f.write('WorldPop values are modelled gridded population estimates, not official census counts. ') 
        f.write('DEM values are elevation in metres where the DEM source uses metre units; slope is reported in degrees; TRI/roughness is a relative local terrain-variation indicator. ESA WorldCover class codes follow the official 10 m WorldCover legend where this prepared layer is used. natural_pct groups Tree, Shrubland, Grassland, Wetland, Mangroves and Moss/Lichen, while built_crop_pct groups Built-up and Cropland for quick screening. ') 
        f.write('For polygon targets, nearest-road and nearest-facility distances are often 0 when roads or facilities occur inside the polygon; in that case, road/facility density and per-10,000 population indicators are more informative. ') 
        f.write('Population-normalized fields are calculated only when both population and the relevant OSM layer are available.</div>')
        f.write('<p class="small">This report summarizes direct enrichment indicators only. It does not generate a universal suitability, vulnerability, exposure, or priority score.</p>')
        f.write('</body></html>')
