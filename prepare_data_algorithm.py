# -*- coding: utf-8 -*-
"""OpenGeoEnrich data preparation/downloader algorithm."""

import os

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterEnum,
    QgsProcessingParameterString,
    QgsProcessingParameterVectorLayer,
    QgsProcessingOutputVectorLayer,
    QgsProcessingOutputFolder,
    QgsProcessingOutputRasterLayer,
)

from .osm_downloader import DEFAULT_FACILITY_CATEGORIES, prepare_osm_data, prepare_worldpop_population, prepare_copernicus_dem, prepare_esa_worldcover


class OpenGeoEnrichPrepareDataAlgorithm(QgsProcessingAlgorithm):
    """Prepare/download open geospatial context data for enrichment."""

    AOI = 'AOI'
    OUTPUT_FOLDER = 'OUTPUT_FOLDER'
    PRESET_MODE = 'PRESET_MODE'
    FAIL_SOFT = 'FAIL_SOFT'
    FETCH_ROADS = 'FETCH_ROADS'
    FETCH_FACILITIES = 'FETCH_FACILITIES'
    FACILITY_CATEGORIES = 'FACILITY_CATEGORIES'
    CUSTOM_TAGS = 'CUSTOM_TAGS'
    TIMEOUT = 'TIMEOUT'
    USE_CACHE = 'USE_CACHE'
    ROADS = 'ROADS'
    FACILITIES = 'FACILITIES'
    FETCH_WORLDPOP = 'FETCH_WORLDPOP'
    WORLDPOP_ISO3 = 'WORLDPOP_ISO3'
    WORLDPOP_YEAR = 'WORLDPOP_YEAR'
    WORLDPOP_MODEL = 'WORLDPOP_MODEL'
    POPULATION = 'POPULATION'
    FETCH_DEM = 'FETCH_DEM'
    FETCH_WORLDCOVER = 'FETCH_WORLDCOVER'
    WORLDCOVER_YEAR = 'WORLDCOVER_YEAR'
    LANDCOVER = 'LANDCOVER'
    DEM = 'DEM'
    SLOPE = 'SLOPE'
    TRI = 'TRI'

    def name(self):
        return 'prepare_open_enrichment_data'

    def displayName(self):
        return 'Prepare OpenGeoEnrich data'

    def group(self):
        return 'Data preparation'

    def groupId(self):
        return 'data_preparation'

    def shortHelpString(self):
        return (
            'Prepares and caches open enrichment datasets for a study area. '
            'Auto-data sources include OpenStreetMap roads/facilities through Overpass, WorldPop population rasters, ESA WorldCover 10 m land-cover rasters, and Copernicus DEM GLO-30 terrain rasters. Use Quick mode for population + OSM, Full mode for the complete stack, or Custom mode for manual selection.\n\n'
            'Outputs are written to a selected folder as roads.gpkg, facilities.gpkg, population.tif, landcover.tif, dem.tif, slope.tif, tri.tif, and manifest.json where selected. '
            'These outputs can be used directly in the main OpenGeoEnrich enrichment algorithm.\n\n'
            'WorldPop note: Global 2 population rasters cover 2015-2030; legacy Global 1 candidates are used for 2000-2020 where needed. '
            'Country rasters can be large, so the tool reports file size before download when the server provides it.\n\n'
            'Important: Overpass can timeout for large areas or unreliable internet connections. '
            'Use a small AOI first, enable cache reuse, and use local layers as fallback when needed.'
        )

    def createInstance(self):
        return OpenGeoEnrichPrepareDataAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.AOI,
                'Study area / target layer for download extent',
                [QgsProcessing.TypeVectorPoint, QgsProcessing.TypeVectorLine, QgsProcessing.TypeVectorPolygon],
            )
        )
        self.addParameter(
            QgsProcessingParameterFolderDestination(
                self.OUTPUT_FOLDER,
                'Output/cache folder for prepared data',
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.PRESET_MODE,
                'Preparation mode',
                options=[
                    'Custom - use the checkboxes below',
                    'Quick - OSM roads/facilities + WorldPop only',
                    'Full - OSM + WorldPop + ESA WorldCover + Copernicus DEM',
                ],
                defaultValue=0,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.FAIL_SOFT,
                'Continue if an optional dataset fails',
                defaultValue=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.FETCH_ROADS,
                'Fetch OSM roads',
                defaultValue=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.FETCH_FACILITIES,
                'Fetch OSM facilities/POIs',
                defaultValue=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.FACILITY_CATEGORIES,
                'Facility categories (comma-separated: schools, health, markets, water)',
                defaultValue=DEFAULT_FACILITY_CATEGORIES,
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.CUSTOM_TAGS,
                'Optional custom OSM tags, e.g. amenity=bank;shop~supermarket|convenience',
                defaultValue='',
                optional=True,
                multiLine=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.FETCH_WORLDPOP,
                'Fetch WorldPop population raster',
                defaultValue=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.WORLDPOP_ISO3,
                'WorldPop country ISO3 code, e.g. PAK',
                defaultValue='PAK',
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.WORLDPOP_YEAR,
                'WorldPop year (Global 2: 2015-2030; legacy fallback: 2000-2020)',
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=2024,
                minValue=2000,
                maxValue=2030,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.WORLDPOP_MODEL,
                'WorldPop model: constrained or unconstrained',
                defaultValue='constrained',
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.FETCH_WORLDCOVER,
                'Fetch ESA WorldCover land-cover raster',
                defaultValue=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.WORLDCOVER_YEAR,
                'ESA WorldCover year (2020 v100 or 2021 v200)',
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=2021,
                minValue=2020,
                maxValue=2021,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.FETCH_DEM,
                'Fetch Copernicus DEM GLO-30 and derive slope/TRI',
                defaultValue=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.TIMEOUT,
                'Overpass timeout in seconds',
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=180,
                minValue=30,
                maxValue=900,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.USE_CACHE,
                'Use cached files if available',
                defaultValue=True,
            )
        )
        self.addOutput(QgsProcessingOutputFolder(self.OUTPUT_FOLDER, 'Prepared data folder'))
        self.addOutput(QgsProcessingOutputVectorLayer(self.ROADS, 'Prepared OSM roads'))
        self.addOutput(QgsProcessingOutputVectorLayer(self.FACILITIES, 'Prepared OSM facilities/POIs'))
        self.addOutput(QgsProcessingOutputRasterLayer(self.POPULATION, 'Prepared WorldPop population raster'))
        self.addOutput(QgsProcessingOutputRasterLayer(self.LANDCOVER, 'Prepared ESA WorldCover land-cover raster'))
        self.addOutput(QgsProcessingOutputRasterLayer(self.DEM, 'Prepared Copernicus DEM raster'))
        self.addOutput(QgsProcessingOutputRasterLayer(self.SLOPE, 'Prepared slope raster'))
        self.addOutput(QgsProcessingOutputRasterLayer(self.TRI, 'Prepared terrain ruggedness raster'))

    def processAlgorithm(self, parameters, context, feedback):
        aoi = self.parameterAsVectorLayer(parameters, self.AOI, context)
        out_folder = self.parameterAsString(parameters, self.OUTPUT_FOLDER, context)
        preset_mode = self.parameterAsEnum(parameters, self.PRESET_MODE, context)
        fail_soft = self.parameterAsBool(parameters, self.FAIL_SOFT, context)
        fetch_roads = self.parameterAsBool(parameters, self.FETCH_ROADS, context)
        fetch_facilities = self.parameterAsBool(parameters, self.FETCH_FACILITIES, context)
        categories = self.parameterAsString(parameters, self.FACILITY_CATEGORIES, context)
        custom_tags = self.parameterAsString(parameters, self.CUSTOM_TAGS, context)
        timeout = self.parameterAsInt(parameters, self.TIMEOUT, context)
        use_cache = self.parameterAsBool(parameters, self.USE_CACHE, context)
        fetch_worldpop = self.parameterAsBool(parameters, self.FETCH_WORLDPOP, context)
        fetch_dem = self.parameterAsBool(parameters, self.FETCH_DEM, context)
        fetch_worldcover = self.parameterAsBool(parameters, self.FETCH_WORLDCOVER, context)
        worldcover_year = self.parameterAsInt(parameters, self.WORLDCOVER_YEAR, context)
        worldpop_iso3 = self.parameterAsString(parameters, self.WORLDPOP_ISO3, context)
        worldpop_year = self.parameterAsInt(parameters, self.WORLDPOP_YEAR, context)
        worldpop_model = self.parameterAsString(parameters, self.WORLDPOP_MODEL, context) or 'constrained'

        if preset_mode == 1:
            fetch_roads = True
            fetch_facilities = True
            fetch_worldpop = True
            fetch_worldcover = False
            fetch_dem = False
        elif preset_mode == 2:
            fetch_roads = True
            fetch_facilities = True
            fetch_worldpop = True
            fetch_worldcover = True
            fetch_dem = True

        if aoi is None or not aoi.isValid():
            raise QgsProcessingException('Invalid study area layer.')
        if not fetch_roads and not fetch_facilities and not fetch_worldpop and not fetch_worldcover and not fetch_dem:
            raise QgsProcessingException('Enable at least one data preparation option: roads, facilities, WorldPop population, ESA WorldCover land cover, or Copernicus DEM.')
        if not out_folder:
            raise QgsProcessingException('Please select an output/cache folder.')

        feedback.pushInfo('Preparing OpenGeoEnrich auto-data folder...')
        feedback.pushInfo('Output/cache folder: {}'.format(out_folder))
        mode_label = ['Custom', 'Quick', 'Full'][preset_mode] if preset_mode in (0, 1, 2) else 'Custom'
        feedback.pushInfo('Preparation mode: {}'.format(mode_label))
        feedback.pushInfo('Continue on optional dataset failure: {}'.format('yes' if fail_soft else 'no'))
        feedback.pushInfo('This tool is executed through the QGIS Processing framework. Network requests are blocking until the server responds or timeout is reached, but the algorithm itself is run by QGIS Processing rather than a menu click handler.')
        feedback.pushInfo('Download warning: WorldPop, WorldCover, and DEM files can be large. Use Quick mode first for slow internet or large AOIs.')
        feedback.setProgress(5)

        result = {'folder': out_folder, 'roads': '', 'facilities': '', 'population': '', 'landcover': '', 'dem': '', 'slope': '', 'tri': '', 'manifest': os.path.join(out_folder, 'manifest.json')}

        if fetch_roads or fetch_facilities:
            try:
                feedback.pushInfo('Starting OSM/Overpass preparation. The request may appear inactive until the server responds or times out.')
                osm_result = prepare_osm_data(
                    aoi_layer=aoi,
                    output_folder=out_folder,
                    fetch_roads=fetch_roads,
                    fetch_facilities=fetch_facilities,
                    facility_categories=categories,
                    custom_tags=custom_tags,
                    timeout=timeout,
                    use_cache=use_cache,
                    feedback=feedback,
                )
                result.update(osm_result)
            except Exception as exc:
                msg = ('OSM/Overpass preparation failed: {}\n\n'
                       'Likely causes: Overpass timeout, internet failure, too-large AOI, or blocked endpoint. '
                       'Try a smaller AOI, increase timeout, use cache, or provide local roads/facility layers in the enrichment algorithm.').format(exc)
                if fail_soft:
                    feedback.pushWarning(msg)
                else:
                    raise QgsProcessingException(msg)

        if fetch_worldpop:
            try:
                feedback.pushInfo('Starting WorldPop population preparation for {} {} ({}).'.format(worldpop_iso3, worldpop_year, worldpop_model))
                wp_result = prepare_worldpop_population(
                    aoi_layer=aoi,
                    output_folder=out_folder,
                    iso3=worldpop_iso3,
                    year=worldpop_year,
                    model=worldpop_model,
                    timeout=timeout,
                    use_cache=use_cache,
                    feedback=feedback,
                )
                result.update(wp_result)
            except Exception as exc:
                msg = ('WorldPop population preparation failed: {}\n\n'
                       'Try a supported ISO3 code/year, check the internet connection, or provide a local population raster in the enrichment algorithm.').format(exc)
                if fail_soft:
                    feedback.pushWarning(msg)
                else:
                    raise QgsProcessingException(msg)


        if fetch_worldcover:
            try:
                feedback.pushInfo('Starting ESA WorldCover land-cover preparation for {}.'.format(worldcover_year))
                wc_result = prepare_esa_worldcover(
                    aoi_layer=aoi,
                    output_folder=out_folder,
                    year=worldcover_year,
                    timeout=timeout,
                    use_cache=use_cache,
                    feedback=feedback,
                )
                result.update(wc_result)
            except Exception as exc:
                msg = ('ESA WorldCover preparation failed: {}\n\n'
                       'Try a smaller AOI, check the internet connection, enable cache, or provide a local land-cover raster in the enrichment algorithm.').format(exc)
                if fail_soft:
                    feedback.pushWarning(msg)
                else:
                    raise QgsProcessingException(msg)

        if fetch_dem:
            try:
                feedback.pushInfo('Starting Copernicus DEM GLO-30 preparation.')
                dem_result = prepare_copernicus_dem(
                    aoi_layer=aoi,
                    output_folder=out_folder,
                    timeout=timeout,
                    use_cache=use_cache,
                    feedback=feedback,
                )
                result.update(dem_result)
            except Exception as exc:
                msg = ('Copernicus DEM preparation failed: {}\n\n'
                       'Try a smaller AOI, check the internet connection, enable cache, or provide a local DEM raster in the enrichment algorithm.').format(exc)
                if fail_soft:
                    feedback.pushWarning(msg)
                else:
                    raise QgsProcessingException(msg)

        feedback.setProgress(95)
        feedback.pushInfo('Prepared folder: {}'.format(result.get('folder', out_folder)))
        feedback.pushInfo('Prepared data checklist:')
        checklist = [
            ('WorldPop population raster', result.get('population', ''), 'population.tif'),
            ('ESA WorldCover land-cover raster', result.get('landcover', ''), 'landcover.tif'),
            ('OSM roads layer', result.get('roads', ''), 'roads.gpkg'),
            ('OSM facilities/POIs layer', result.get('facilities', ''), 'facilities.gpkg'),
            ('Copernicus DEM raster', result.get('dem', ''), 'dem.tif'),
            ('Slope raster', result.get('slope', ''), 'slope.tif'),
            ('Terrain ruggedness raster', result.get('tri', ''), 'tri.tif'),
            ('Manifest JSON', result.get('manifest', os.path.join(out_folder, 'manifest.json')), 'manifest.json'),
        ]
        for label, path, expected_name in checklist:
            if path and os.path.exists(path):
                try:
                    size_mb = os.path.getsize(path) / (1024.0 * 1024.0)
                    feedback.pushInfo('  [FOUND] {}: {} ({:.2f} MB)'.format(label, path, size_mb))
                except Exception:
                    feedback.pushInfo('  [FOUND] {}: {}'.format(label, path))
            else:
                expected_path = os.path.join(out_folder, expected_name)
                if os.path.exists(expected_path):
                    try:
                        size_mb = os.path.getsize(expected_path) / (1024.0 * 1024.0)
                        feedback.pushInfo('  [FOUND] {}: {} ({:.2f} MB)'.format(label, expected_path, size_mb))
                    except Exception:
                        feedback.pushInfo('  [FOUND] {}: {}'.format(label, expected_path))
                else:
                    feedback.pushInfo('  [missing] {}: {}'.format(label, expected_path))

        feedback.setProgress(100)
        feedback.pushInfo('OpenGeoEnrich data preparation completed.')
        return {
            self.OUTPUT_FOLDER: result.get('folder', out_folder),
            self.ROADS: result.get('roads', ''),
            self.FACILITIES: result.get('facilities', ''),
            self.POPULATION: result.get('population', ''),
            self.LANDCOVER: result.get('landcover', ''),
            self.DEM: result.get('dem', ''),
            self.SLOPE: result.get('slope', ''),
            self.TRI: result.get('tri', ''),
        }
