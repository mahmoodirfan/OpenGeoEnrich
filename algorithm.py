# -*- coding: utf-8 -*-
"""OpenGeoEnrich Processing algorithm."""

import os

from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterDistance,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFile,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterString,
    QgsProcessingParameterVectorLayer,
    QgsVectorLayer,
    QgsWkbTypes,
    QgsGraduatedSymbolRenderer,
    QgsRendererRange,
    QgsSymbol,
    QgsProject,
)
from qgis.analysis import QgsZonalStatistics

try:
    import processing
except Exception:  # pragma: no cover
    processing = None


ESA_WORLDCOVER_LABELS = {
    10: 'Tree cover',
    20: 'Shrubland',
    30: 'Grassland',
    40: 'Cropland',
    50: 'Built-up',
    60: 'Bare / sparse vegetation',
    70: 'Snow and ice',
    80: 'Permanent water',
    90: 'Herbaceous wetland',
    95: 'Mangroves',
    100: 'Moss and lichen',
}


def esa_worldcover_label(value):
    try:
        if value in (None, ''):
            return None
        code = int(float(value))
        return ESA_WORLDCOVER_LABELS.get(code, str(code))
    except Exception:
        return None

from .enrich_engine import (
    compute_vector_enrichment,
    create_working_polygon_layer,
    extract_zone_stats,
    parse_lc_groups,
    safe_field_name,
    write_csv,
)


class OpenGeoEnrichAlgorithm(QgsProcessingAlgorithm):
    """Enrich a vector layer with open geospatial indicators."""

    TARGET = 'TARGET'
    BUFFER = 'BUFFER'
    PREPARED_FOLDER = 'PREPARED_FOLDER'
    POP_RASTER = 'POP_RASTER'
    LC_RASTER = 'LC_RASTER'
    LC_GROUPS = 'LC_GROUPS'
    DEM_RASTER = 'DEM_RASTER'
    ROADS = 'ROADS'
    FACILITIES = 'FACILITIES'
    OUTPUT = 'OUTPUT'
    OUTPUT_ZONES = 'OUTPUT_ZONES'
    CSV = 'CSV'
    HTML = 'HTML'

    def name(self):
        return 'enrich_layer'

    def displayName(self):
        return 'Enrich layer with open geospatial data'

    def group(self):
        return 'GeoEnrichment'

    def groupId(self):
        return 'geoenrichment'

    def shortHelpString(self):
        return (
            'OpenGeoEnrich adds contextual indicators to point, line, or polygon layers using user-provided open geospatial data or a prepared OpenGeoEnrich data folder.\n\n'
            'For point and line target layers, a buffer is created first and indicators are calculated inside the buffer. '
            'For polygon target layers, indicators are calculated inside each polygon.\n\n'
            'Supported optional inputs are: prepared OpenGeoEnrich data folder, population raster, land-cover raster, DEM raster, roads layer, '
            'slope raster, terrain ruggedness raster, land-cover raster, and facilities/POI layer. The output is an enriched vector layer, optional QA enrichment zones, optional CSV table, and optional HTML report.\n\n'
            'For metric buffers/distances, use a projected CRS. If the target layer is geographic, QGIS layer units are used.'
        )

    def createInstance(self):
        return OpenGeoEnrichAlgorithm()

    def _find_output_layer_for_styling(self, context):
        """Find the loaded enriched output layer as robustly as possible."""
        dest_id = getattr(self, '_last_output_dest', None)
        if dest_id:
            layer = context.getMapLayer(dest_id)
            if layer is not None and layer.isValid():
                return layer
            layer = QgsProject.instance().mapLayer(dest_id)
            if layer is not None and layer.isValid():
                return layer
        # Some QGIS builds load Processing outputs under a generated display name.
        # Fall back to the newest loaded vector layer containing OpenGeoEnrich core fields.
        candidates = []
        for lyr in QgsProject.instance().mapLayers().values():
            try:
                if not isinstance(lyr, QgsVectorLayer) or not lyr.isValid():
                    continue
                names = set(f.name() for f in lyr.fields())
                if {'oge_area_km2', 'pop_dens_km2'}.issubset(names):
                    candidates.append(lyr)
            except Exception:
                pass
        return candidates[-1] if candidates else None

    def postProcessAlgorithm(self, context, feedback):
        """Apply a lightweight default graduated style to the loaded output layer."""
        try:
            layer = self._find_output_layer_for_styling(context)
            if layer is None or not layer.isValid():
                feedback.pushInfo('Default style was not applied because the output layer was not available in the QGIS project yet.')
                return {}

            field_names = [f.name() for f in layer.fields()]
            style_field = None
            for candidate in ('pop_dens_km2', 'built_crop_pct', 'natural_pct', 'road_dens_km_km2'):
                if candidate in field_names:
                    style_field = candidate
                    break
            if not style_field:
                return {}

            values = []
            for feat in layer.getFeatures():
                try:
                    v = feat[style_field]
                    if v not in (None, ''):
                        values.append(float(v))
                except Exception:
                    pass
            if not values:
                return {}
            vmin, vmax = min(values), max(values)
            if vmin == vmax:
                return {}

            colors = ['#f7fbff', '#c6dbef', '#6baed6', '#2171b5', '#08306b']
            ranges = []
            classes = min(5, max(2, len(set(values))))
            step = (vmax - vmin) / float(classes)
            for i in range(classes):
                lower = vmin + i * step
                upper = vmax if i == classes - 1 else vmin + (i + 1) * step
                sym = QgsSymbol.defaultSymbol(layer.geometryType())
                if sym is None:
                    continue
                sym.setColor(QColor(colors[min(i, len(colors) - 1)]))
                if hasattr(sym, 'symbolLayer') and sym.symbolLayerCount() > 0:
                    sl = sym.symbolLayer(0)
                    if hasattr(sl, 'setStrokeColor'):
                        sl.setStrokeColor(QColor('#555555'))
                    if hasattr(sl, 'setStrokeWidth'):
                        sl.setStrokeWidth(0.2)
                label = '{:.2f} - {:.2f}'.format(lower, upper)
                ranges.append(QgsRendererRange(lower, upper, sym, label))

            if ranges:
                # Clear accidental feature selection so QGIS yellow selection highlight does not hide the style.
                try:
                    layer.removeSelection()
                except Exception:
                    pass
                renderer = QgsGraduatedSymbolRenderer(style_field, ranges)
                renderer.setMode(QgsGraduatedSymbolRenderer.EqualInterval)
                layer.setRenderer(renderer)
                layer.triggerRepaint()
                feedback.pushInfo('Applied default graduated style using field: {}'.format(style_field))
        except Exception as exc:
            feedback.pushWarning('Could not apply default output style: {}'.format(exc))
        return {}

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.TARGET,
                'Target layer to enrich (point, line, or polygon)',
                [QgsProcessing.TypeVectorPoint, QgsProcessing.TypeVectorLine, QgsProcessing.TypeVectorPolygon],
            )
        )
        self.addParameter(
            QgsProcessingParameterDistance(
                self.BUFFER,
                'Buffer distance for point/line targets (target layer CRS units)',
                defaultValue=1000.0,
                parentParameterName=self.TARGET,
                optional=False,
                minValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterFile(
                self.PREPARED_FOLDER,
                'Prepared OpenGeoEnrich data folder (optional; from Prepare OpenGeoEnrich data)',
                behavior=QgsProcessingParameterFile.Folder,
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.POP_RASTER,
                'Population raster (optional; population count/intensity)',
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.LC_RASTER,
                'Land-cover raster (optional; integer classes)',
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.LC_GROUPS,
                'Land-cover groups, e.g. Tree=10;Shrubland=20;Grassland=30;Cropland=40;Builtup=50;Water=80',
                defaultValue='Tree=10;Shrubland=20;Grassland=30;Cropland=40;Builtup=50;Bare=60;SnowIce=70;Water=80;Wetland=90;Mangroves=95;MossLichen=100',
                optional=True,
                multiLine=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.DEM_RASTER,
                'DEM raster (optional)',
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.ROADS,
                'Roads/linear infrastructure layer (optional)',
                [QgsProcessing.TypeVectorLine],
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.FACILITIES,
                'Facilities/POI layer (optional; schools, health facilities, water points, etc.)',
                [QgsProcessing.TypeVectorPoint, QgsProcessing.TypeVectorPolygon],
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                'Enriched output layer',
                QgsProcessing.TypeVectorAnyGeometry,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_ZONES,
                'Optional QA enrichment zones/buffers',
                QgsProcessing.TypeVectorPolygon,
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.CSV,
                'Optional CSV summary table',
                fileFilter='CSV files (*.csv)',
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.HTML,
                'Optional HTML summary report',
                fileFilter='HTML files (*.html)',
                optional=True,
            )
        )

    def _add_zonal_stats(self, work_layer, raster_layer, prefix, stats, feedback):
        """Run QgsZonalStatistics and return True/False."""
        if raster_layer is None:
            return False
        try:
            zs = QgsZonalStatistics(work_layer, raster_layer, prefix, 1, stats)
            result = zs.calculateStatistics(feedback)
            if result != 0:
                feedback.pushWarning('Zonal statistics returned code {} for prefix {}'.format(result, prefix))
            return True
        except Exception as exc:
            feedback.pushWarning('Could not calculate zonal statistics for {}: {}'.format(prefix, exc))
            return False

    def _run_landcover_histogram(self, work_layer, lc_raster, feedback):
        """Run QGIS zonal histogram on the working layer.

        QGIS changed this algorithm's parameter names across versions.
        Try the modern names first, then older aliases. If all fail, the
        plugin will still return majority/variety from QgsZonalStatistics.
        """
        if processing is None or lc_raster is None:
            return work_layer

        param_sets = [
            {
                'INPUT_RASTER': lc_raster,
                'RASTER_BAND': 1,
                'INPUT_VECTOR': work_layer,
                'COLUMN_PREFIX': 'lc_',
                'OUTPUT': 'TEMPORARY_OUTPUT',
            },
            {
                'INPUT': work_layer,
                'INPUT_RASTER': lc_raster,
                'RASTER_BAND': 1,
                'COLUMN_PREFIX': 'lc_',
                'OUTPUT': 'TEMPORARY_OUTPUT',
            },
            {
                'INPUT': work_layer,
                'RASTER': lc_raster,
                'RASTER_BAND': 1,
                'COLUMN_PREFIX': 'lc_',
                'OUTPUT': 'TEMPORARY_OUTPUT',
            },
        ]

        for alg_id in ('native:zonalhistogram', 'qgis:zonalhistogram'):
            for params in param_sets:
                try:
                    feedback.pushInfo('Calculating land-cover histogram using {}...'.format(alg_id))
                    result = processing.run(alg_id, params, feedback=feedback)
                    out = result.get('OUTPUT')
                    if out is not None:
                        feedback.pushInfo('Land-cover histogram completed.')
                        return out
                except Exception as exc:
                    feedback.pushWarning('Land-cover histogram with {} failed: {}'.format(alg_id, exc))
        feedback.pushWarning('Land-cover class-percentage fields could not be calculated. Majority and variety fields are still available.')
        return work_layer

    def _load_prepared_layer(self, folder, layer_name, feedback):
        """Load a layer from a prepared OpenGeoEnrich GeoPackage folder."""
        if not folder:
            return None
        gpkg = os.path.join(folder, layer_name + '.gpkg')
        if not os.path.exists(gpkg):
            return None
        layer = QgsVectorLayer(gpkg + '|layername=' + layer_name, 'prepared_' + layer_name, 'ogr')
        if layer.isValid():
            feedback.pushInfo('Using prepared {} layer: {}'.format(layer_name, gpkg))
            return layer
        feedback.pushWarning('Prepared {} file exists but could not be loaded: {}'.format(layer_name, gpkg))
        return None

    def _load_prepared_raster(self, folder, file_name, feedback):
        """Load a raster from a prepared OpenGeoEnrich folder."""
        if not folder:
            return None
        path = os.path.join(folder, file_name)
        if not os.path.exists(path):
            return None
        from qgis.core import QgsRasterLayer
        layer = QgsRasterLayer(path, 'prepared_' + os.path.splitext(file_name)[0])
        if layer.isValid():
            feedback.pushInfo('Using prepared raster: {}'.format(path))
            return layer
        feedback.pushWarning('Prepared raster exists but could not be loaded: {}'.format(path))
        return None

    def _prepared_folder_checklist(self, folder, feedback):
        """Log a simple prepared-data checklist so users can see what will be used."""
        if not folder:
            return
        feedback.pushInfo('Prepared folder checklist: {}'.format(folder))
        expected = [
            ('WorldPop population raster', 'population.tif'),
            ('OSM roads layer', 'roads.gpkg'),
            ('OSM facilities/POIs layer', 'facilities.gpkg'),
            ('ESA WorldCover land-cover raster', 'landcover.tif'),
            ('Copernicus DEM raster', 'dem.tif'),
            ('Slope raster', 'slope.tif'),
            ('Terrain ruggedness raster', 'tri.tif'),
            ('Manifest file', 'manifest.json'),
        ]
        for label, name in expected:
            path = os.path.join(folder, name)
            if os.path.exists(path):
                try:
                    size_mb = os.path.getsize(path) / (1024.0 * 1024.0)
                    feedback.pushInfo('  [FOUND] {}: {} ({:.2f} MB)'.format(label, name, size_mb))
                except Exception:
                    feedback.pushInfo('  [FOUND] {}: {}'.format(label, name))
            else:
                feedback.pushInfo('  [missing] {}: {}'.format(label, name))

    def processAlgorithm(self, parameters, context, feedback):
        target = self.parameterAsVectorLayer(parameters, self.TARGET, context)
        buffer_distance = self.parameterAsDouble(parameters, self.BUFFER, context)
        prepared_folder = self.parameterAsString(parameters, self.PREPARED_FOLDER, context)
        pop_raster = self.parameterAsRasterLayer(parameters, self.POP_RASTER, context)
        lc_raster = self.parameterAsRasterLayer(parameters, self.LC_RASTER, context)
        dem_raster = self.parameterAsRasterLayer(parameters, self.DEM_RASTER, context)
        slope_raster = None
        tri_raster = None
        roads = self.parameterAsVectorLayer(parameters, self.ROADS, context)
        facilities = self.parameterAsVectorLayer(parameters, self.FACILITIES, context)
        if prepared_folder:
            self._prepared_folder_checklist(prepared_folder, feedback)
            if pop_raster is None:
                pop_raster = self._load_prepared_raster(prepared_folder, 'population.tif', feedback)
            if lc_raster is None:
                lc_raster = self._load_prepared_raster(prepared_folder, 'landcover.tif', feedback)
            if dem_raster is None:
                dem_raster = self._load_prepared_raster(prepared_folder, 'dem.tif', feedback)
            slope_raster = self._load_prepared_raster(prepared_folder, 'slope.tif', feedback)
            tri_raster = self._load_prepared_raster(prepared_folder, 'tri.tif', feedback)
            if roads is None:
                roads = self._load_prepared_layer(prepared_folder, 'roads', feedback)
            if facilities is None:
                facilities = self._load_prepared_layer(prepared_folder, 'facilities', feedback)
        lc_groups_text = self.parameterAsString(parameters, self.LC_GROUPS, context)
        csv_path = self.parameterAsFileOutput(parameters, self.CSV, context)
        html_path = self.parameterAsFileOutput(parameters, self.HTML, context)

        if target is None or not target.isValid():
            raise QgsProcessingException('Invalid target layer.')
        if not any([pop_raster, lc_raster, dem_raster, slope_raster, tri_raster, roads, facilities]):
            raise QgsProcessingException('Please provide at least one enrichment layer/raster.')

        feedback.pushInfo('Enrichment input status:')
        feedback.pushInfo('  Population: {}'.format('enabled' if pop_raster else 'not used'))
        feedback.pushInfo('  Land cover: {}'.format('enabled' if lc_raster else 'not used'))
        feedback.pushInfo('  DEM: {}'.format('enabled' if dem_raster else 'not used'))
        feedback.pushInfo('  Slope: {}'.format('enabled' if slope_raster else 'not used'))
        feedback.pushInfo('  Terrain ruggedness: {}'.format('enabled' if tri_raster else 'not used'))
        feedback.pushInfo('  Roads: {}'.format('enabled' if roads else 'not used'))
        feedback.pushInfo('  Facilities/POIs: {}'.format('enabled' if facilities else 'not used'))
        if pop_raster and not roads and not facilities:
            feedback.pushWarning('Population is enabled, but roads/facilities are not. Per-population road/facility indicators will remain empty.')
        if (roads or facilities) and not pop_raster:
            feedback.pushWarning('Roads/facilities are enabled, but population is not. Per-10k-population indicators will remain empty.')

        if target.crs().isGeographic():
            feedback.pushWarning('Target layer is in a geographic CRS. For meter-based buffers, road lengths, and nearest distances, reproject it to a projected CRS first.')

        feedback.pushInfo('Creating enrichment zones...')
        try:
            work_layer, id_to_original = create_working_polygon_layer(target, buffer_distance, context, feedback)
        except Exception as exc:
            raise QgsProcessingException(str(exc))

        if work_layer.featureCount() == 0:
            raise QgsProcessingException('No valid target geometries found.')

        feedback.pushInfo('Calculating raster summaries. Large rasters may take time during QGIS zonal-statistics operations...')
        feedback.setProgress(15)
        if pop_raster:
            feedback.pushInfo('Population zonal statistics started...')
            self._add_zonal_stats(
                work_layer,
                pop_raster,
                'pop_',
                QgsZonalStatistics.Sum | QgsZonalStatistics.Mean,
                feedback,
            )
            feedback.pushInfo('Population zonal statistics finished.')
            feedback.setProgress(25)
        if dem_raster:
            feedback.pushInfo('DEM zonal statistics started...')
            self._add_zonal_stats(
                work_layer,
                dem_raster,
                'dem_',
                QgsZonalStatistics.Mean | QgsZonalStatistics.Min | QgsZonalStatistics.Max,
                feedback,
            )
            feedback.pushInfo('DEM zonal statistics finished.')
            feedback.setProgress(35)
        if slope_raster:
            feedback.pushInfo('Slope zonal statistics started...')
            self._add_zonal_stats(
                work_layer,
                slope_raster,
                'slope_',
                QgsZonalStatistics.Mean | QgsZonalStatistics.Min | QgsZonalStatistics.Max,
                feedback,
            )
            feedback.pushInfo('Slope zonal statistics finished.')
            feedback.setProgress(38)
        if tri_raster:
            feedback.pushInfo('Terrain ruggedness zonal statistics started...')
            self._add_zonal_stats(
                work_layer,
                tri_raster,
                'tri_',
                QgsZonalStatistics.Mean | QgsZonalStatistics.Min | QgsZonalStatistics.Max,
                feedback,
            )
            feedback.pushInfo('Terrain ruggedness zonal statistics finished.')
            feedback.setProgress(40)
        if lc_raster:
            feedback.pushInfo('Land-cover majority/variety statistics started...')
            self._add_zonal_stats(
                work_layer,
                lc_raster,
                'lcstat_',
                QgsZonalStatistics.Majority | QgsZonalStatistics.Variety,
                feedback,
            )
            feedback.pushInfo('Land-cover majority/variety statistics finished.')
            feedback.setProgress(45)
            work_layer = self._run_landcover_histogram(work_layer, lc_raster, feedback)

        raster_rows = extract_zone_stats(work_layer)
        feedback.setProgress(55)

        feedback.pushInfo('Calculating vector accessibility/context indicators...')
        vector_rows = compute_vector_enrichment(work_layer, roads, facilities, feedback)

        # Create output field list.
        output_fields = QgsFields()
        existing_names = set()
        for fld in target.fields():
            name = safe_field_name(fld.name(), existing_names, 30)
            output_fields.append(QgsField(name, fld.type(), fld.typeName(), fld.length(), fld.precision()))

        added_field_defs = [
            ('oge_area_ha', QVariant.Double),
            ('oge_area_km2', QVariant.Double),
            ('pop_sum', QVariant.Double),
            ('pop_mean', QVariant.Double),
            ('pop_dens_km2', QVariant.Double),
            ('dem_mean', QVariant.Double),
            ('dem_min', QVariant.Double),
            ('dem_max', QVariant.Double),
            ('slope_mean', QVariant.Double),
            ('slope_min', QVariant.Double),
            ('slope_max', QVariant.Double),
            ('tri_mean', QVariant.Double),
            ('tri_min', QVariant.Double),
            ('tri_max', QVariant.Double),
            ('lc_majority', QVariant.Double),
            ('lc_majority_label', QVariant.String),
            ('lc_variety', QVariant.Double),
            ('natural_pct', QVariant.Double),
            ('built_crop_pct', QVariant.Double),
            ('road_len_km', QVariant.Double),
            ('road_dens_km_km2', QVariant.Double),
            ('near_road_m', QVariant.Double),
            ('facility_ct', QVariant.Int),
            ('fac_dens_km2', QVariant.Double),
            ('near_fac_m', QVariant.Double),
            ('fac_per_10k_pop', QVariant.Double),
            ('road_km_per_10k_pop', QVariant.Double),
        ]
        for name, typ in added_field_defs:
            output_fields.append(QgsField(safe_field_name(name, existing_names, 30), typ))

        # Land-cover groups become percentage/count fields if histogram exists.
        groups = parse_lc_groups(lc_groups_text)
        group_field_map = []
        if lc_raster and groups:
            for label, _codes in groups:
                safe = safe_field_name('lc_' + label, set(), 16)
                pct_name = safe_field_name(safe + '_pct', existing_names, 30)
                pix_name = safe_field_name(safe + '_pix', existing_names, 30)
                output_fields.append(QgsField(pct_name, QVariant.Double))
                output_fields.append(QgsField(pix_name, QVariant.Double))
                group_field_map.append((label, pct_name, pix_name))

        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            output_fields,
            target.wkbType(),
            target.crs(),
        )
        if sink is None:
            raise QgsProcessingException('Could not create output sink.')

        # Optional QA zones/buffers output. This helps users inspect exactly which
        # area was used for point/line enrichment.
        zone_fields = QgsFields()
        zone_existing = set()
        zone_fields.append(QgsField('__oge_id', QVariant.Int))
        for fld in target.fields():
            zone_fields.append(QgsField(safe_field_name(fld.name(), zone_existing, 30), fld.type(), fld.typeName(), fld.length(), fld.precision()))
        zone_fields.append(QgsField('oge_area_ha', QVariant.Double))
        zone_sink, zone_dest_id = self.parameterAsSink(
            parameters,
            self.OUTPUT_ZONES,
            context,
            zone_fields,
            QgsWkbTypes.Polygon,
            target.crs(),
        )

        original_count = target.fields().count()
        out_features_for_csv = []

        # Work layer area by id.
        work_geoms = {int(f['__oge_id']): f.geometry() for f in work_layer.getFeatures()}
        total = max(len(id_to_original), 1)

        # Build row cache before writing output.
        row_cache = {}
        for oge_id, original_feat in id_to_original.items():
            zone_geom = work_geoms.get(oge_id)
            area = zone_geom.area() if zone_geom is not None else None
            area_ha = (area / 10000.0) if area is not None else None
            area_km2 = (area / 1000000.0) if area is not None else None

            rr = raster_rows.get(oge_id, {})
            vr = vector_rows.get(oge_id, {})

            pop_sum = rr.get('pop_sum')
            pop_mean = rr.get('pop_mean')
            if pop_sum in (None, ''):
                pop_sum = rr.get('pop_Sum')
            if pop_mean in (None, ''):
                pop_mean = rr.get('pop_Mean')
            pop_density = None
            try:
                if pop_sum is not None and area_km2 and area_km2 > 0:
                    pop_density = float(pop_sum) / float(area_km2)
            except Exception:
                pop_density = None

            dem_mean = rr.get('dem_mean', rr.get('dem_Mean'))
            dem_min = rr.get('dem_min', rr.get('dem_Min'))
            dem_max = rr.get('dem_max', rr.get('dem_Max'))
            slope_mean = rr.get('slope_mean', rr.get('slope_Mean'))
            slope_min = rr.get('slope_min', rr.get('slope_Min'))
            slope_max = rr.get('slope_max', rr.get('slope_Max'))
            tri_mean = rr.get('tri_mean', rr.get('tri_Mean'))
            tri_min = rr.get('tri_min', rr.get('tri_Min'))
            tri_max = rr.get('tri_max', rr.get('tri_Max'))
            lc_majority = rr.get('lcstat_majority', rr.get('lcstat_Majority'))
            lc_majority_label = esa_worldcover_label(lc_majority)
            lc_variety = rr.get('lcstat_variety', rr.get('lcstat_Variety'))

            road_density = None
            facility_density = None
            facilities_per_10k_pop = None
            road_km_per_10k_pop = None
            try:
                if vr.get('road_len_km') is not None and area_km2 and area_km2 > 0:
                    road_density = float(vr.get('road_len_km')) / float(area_km2)
            except Exception:
                road_density = None
            try:
                if vr.get('facility_ct') is not None and area_km2 and area_km2 > 0:
                    facility_density = float(vr.get('facility_ct')) / float(area_km2)
            except Exception:
                facility_density = None
            try:
                pop_total = float(pop_sum) if pop_sum not in (None, '') else None
                if pop_total and pop_total > 0 and vr.get('facility_ct') is not None:
                    facilities_per_10k_pop = (float(vr.get('facility_ct')) / pop_total) * 10000.0
            except Exception:
                facilities_per_10k_pop = None
            try:
                pop_total = float(pop_sum) if pop_sum not in (None, '') else None
                if pop_total and pop_total > 0 and vr.get('road_len_km') is not None:
                    road_km_per_10k_pop = (float(vr.get('road_len_km')) / pop_total) * 10000.0
            except Exception:
                road_km_per_10k_pop = None

            row = {
                'area_ha': area_ha,
                'area_km2': area_km2,
                'pop_sum': pop_sum,
                'pop_mean': pop_mean,
                'pop_dens_km2': pop_density,
                'dem_mean': dem_mean,
                'dem_min': dem_min,
                'dem_max': dem_max,
                'slope_mean': slope_mean,
                'slope_min': slope_min,
                'slope_max': slope_max,
                'tri_mean': tri_mean,
                'tri_min': tri_min,
                'tri_max': tri_max,
                'lc_majority': lc_majority,
                'lc_majority_label': lc_majority_label,
                'lc_variety': lc_variety,
                'natural_pct': None,
                'built_crop_pct': None,
                'road_len_km': vr.get('road_len_km'),
                'road_dens_km_km2': road_density,
                'near_road_m': vr.get('near_road_m'),
                'facility_ct': vr.get('facility_ct'),
                'fac_dens_km2': facility_density,
                'near_fac_m': vr.get('near_fac_m'),
                'fac_per_10k_pop': facilities_per_10k_pop,
                'road_km_per_10k_pop': road_km_per_10k_pop,
                'groups': [],
            }

            if lc_raster and groups:
                hist_fields = {}
                total_pix = 0.0
                for key, value in rr.items():
                    if not str(key).startswith('lc_'):
                        continue
                    suffix = str(key)[3:]
                    try:
                        code = str(int(float(suffix.strip('_'))))
                    except Exception:
                        code = suffix.strip('_')
                    try:
                        val = float(value) if value not in (None, '') else 0.0
                    except Exception:
                        val = 0.0
                    hist_fields[code] = val
                    total_pix += val
                # Simplified high-level land-cover summaries for quick interpretation.
                # These are calculated from ESA WorldCover-style class codes where present.
                # Natural = Tree + Shrubland + Grassland + Wetland + Mangroves + Moss/Lichen.
                # Built/cropland = Built-up + Cropland.
                if total_pix > 0:
                    natural_pix = sum(hist_fields.get(c, 0.0) for c in ('10', '20', '30', '90', '95', '100'))
                    built_crop_pix = sum(hist_fields.get(c, 0.0) for c in ('40', '50'))
                    row['natural_pct'] = 100.0 * natural_pix / total_pix
                    row['built_crop_pct'] = 100.0 * built_crop_pix / total_pix

                for label, codes in groups:
                    pix = sum(hist_fields.get(c, 0.0) for c in codes)
                    pct = (100.0 * pix / total_pix) if total_pix > 0 else None
                    row['groups'].extend([pct, pix])

            row_cache[oge_id] = row

        feedback.pushInfo('Writing enriched output layer...')
        for n, (oge_id, original_feat) in enumerate(id_to_original.items()):
            if feedback.isCanceled():
                break
            out_feat = QgsFeature(output_fields)
            out_feat.setGeometry(original_feat.geometry())
            attrs = []
            for idx in range(original_count):
                attrs.append(original_feat.attributes()[idx])
            row = row_cache.get(oge_id, {})
            attrs.extend([
                row.get('area_ha'),
                row.get('area_km2'),
                row.get('pop_sum'),
                row.get('pop_mean'),
                row.get('pop_dens_km2'),
                row.get('dem_mean'),
                row.get('dem_min'),
                row.get('dem_max'),
                row.get('slope_mean'),
                row.get('slope_min'),
                row.get('slope_max'),
                row.get('tri_mean'),
                row.get('tri_min'),
                row.get('tri_max'),
                row.get('lc_majority'),
                row.get('lc_majority_label'),
                row.get('lc_variety'),
                row.get('natural_pct'),
                row.get('built_crop_pct'),
                row.get('road_len_km'),
                row.get('road_dens_km_km2'),
                row.get('near_road_m'),
                row.get('facility_ct'),
                row.get('fac_dens_km2'),
                row.get('near_fac_m'),
                row.get('fac_per_10k_pop'),
                row.get('road_km_per_10k_pop'),
            ])
            if lc_raster and groups:
                attrs.extend(row.get('groups', []))
            out_feat.setAttributes(attrs)
            sink.addFeature(out_feat, QgsFeatureSink.FastInsert)
            out_features_for_csv.append(out_feat)

            if zone_sink is not None:
                zf = QgsFeature(zone_fields)
                zf.setGeometry(work_geoms.get(oge_id))
                zattrs = [oge_id]
                for idx in range(original_count):
                    zattrs.append(original_feat.attributes()[idx])
                zattrs.append(row.get('area_ha'))
                zf.setAttributes(zattrs)
                zone_sink.addFeature(zf, QgsFeatureSink.FastInsert)

            if n % 20 == 0:
                feedback.setProgress(80 + int(20 * n / total))

        if csv_path:
            try:
                write_csv(csv_path, out_features_for_csv, output_fields)
                feedback.pushInfo('CSV written to {}'.format(csv_path))
            except Exception as exc:
                feedback.pushWarning('Could not write CSV: {}'.format(exc))

        if html_path:
            try:
                from .enrich_engine import write_html_report
                write_html_report(
                    html_path,
                    target_name=target.name(),
                    feature_count=len(out_features_for_csv),
                    fields=output_fields,
                    features=out_features_for_csv,
                    inputs={
                        'Population raster': pop_raster.name() if pop_raster else 'Not used',
                        'Land-cover raster': lc_raster.name() if lc_raster else 'Not used',
                        'DEM raster': dem_raster.name() if dem_raster else 'Not used',
                        'Slope raster': slope_raster.name() if slope_raster else 'Not used',
                        'Terrain ruggedness raster': tri_raster.name() if tri_raster else 'Not used',
                        'Roads layer': roads.name() if roads else 'Not used',
                        'Facilities layer': facilities.name() if facilities else 'Not used',
                    },
                )
                feedback.pushInfo('HTML report written to {}'.format(html_path))
            except Exception as exc:
                feedback.pushWarning('Could not write HTML report: {}'.format(exc))

        feedback.pushInfo('OpenGeoEnrich completed successfully.')
        self._last_output_dest = dest_id
        result = {self.OUTPUT: dest_id, self.CSV: csv_path, self.HTML: html_path}
        if zone_sink is not None:
            result[self.OUTPUT_ZONES] = zone_dest_id
        return result
