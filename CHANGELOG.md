
## 1.2.4

- Suppressed Bandit B310 false positives on fixed HTTPS open-data download calls using explicit `# nosec B310` annotations.
- Confirmed open-data download URLs are fixed HTTPS endpoints for WorldPop, ESA WorldCover, Copernicus DEM, and Overpass API.
- Updated metadata version for QGIS repository resubmission.

# Changelog

## 1.2.3
- Improved default output styling for the enriched output layer.
- More robustly finds the loaded Processing output layer before applying style.
- Clears accidental feature selection after enrichment so QGIS yellow selection highlighting does not hide graduated colors.
- Default style still prioritizes `pop_dens_km2`, then `built_crop_pct`, `natural_pct`, and `road_dens_km_km2`.

## 1.2.2
- Fixed raster output typing during preparation.
- DEM merge/clip now preserves Float32 elevation values before slope/roughness derivation.
- ESA WorldCover clipping now preserves categorical Byte class codes and avoids GeoTIFF color-table warnings caused by Int32 output.


## 1.2.1

DEM data-type fix.

- Fixed Copernicus DEM merge/clip data type so elevations are preserved as Float32 instead of being forced to Byte on some QGIS/GDAL builds.
- This prevents corrupted DEM-derived elevation, slope, and roughness outputs when multiple Copernicus DEM tiles are merged.


## 1.2.0

Product-hardening build.

- Added Preparation mode: Custom, Quick, and Full.
- Added soft-fail option so optional dataset failures can be logged without cancelling the whole run.
- Added clearer download warnings for large WorldPop, ESA WorldCover, and DEM inputs.
- Added default graduated styling for the enriched output layer when QGIS exposes the result layer after Processing.
- Improved prepared-folder and input-status logging.
- Kept the full open-data stack: OSM roads/facilities, WorldPop, ESA WorldCover, Copernicus DEM, slope, TRI, CSV, QA zones, and HTML report.

- Renamed the visible preparation algorithm to `Prepare OpenGeoEnrich data` for a cleaner QGIS Processing Toolbox label.
- Expanded README with Quick mode testing, soft-fail testing, and repository-readiness checklist.

## Development build - cleanup after WorldCover integration
- Fixed HTML status detection for string/text fields such as `lc_majority_label`.
- Added simplified land-cover summary fields: `natural_pct` and `built_crop_pct`.
- Added ESA WorldCover tile-count metadata to `manifest.json`.
- Updated HTML interpretation notes for the simplified land-cover summaries.

## Development build - ESA WorldCover auto-data
- Added optional ESA WorldCover 10 m auto-download for 2020 v100 and 2021 v200.
- Added local WorldCover tile cache under `_cache/esa_worldcover`.
- Added merging/clipping to create `landcover.tif` in the prepared data folder.
- Main enrichment algorithm now auto-loads `landcover.tif` from a prepared folder when no manual land-cover raster is selected.
- Updated default land-cover groups to official ESA WorldCover classes.
- Added `lc_majority_label` output for known ESA WorldCover class codes.
- Updated HTML report to show WorldCover class/group fields.


## Development build - DEM auto-data
- Added optional Copernicus DEM GLO-30 auto-download in the data-preparation algorithm.
- Added local DEM tile cache under `_cache/copernicus_dem_30m`.
- Added DEM merging/clipping to create `dem.tif` in the prepared data folder.
- Added derived `slope.tif` and `tri.tif` outputs when GDAL terrain algorithms are available.
- Main enrichment algorithm now auto-loads `dem.tif`, `slope.tif`, and `tri.tif` from a prepared folder.
- Added output fields: `slope_mean`, `slope_min`, `slope_max`, `tri_mean`, `tri_min`, `tri_max`.
- Updated HTML report to summarize elevation, slope, and terrain ruggedness indicators.

## Development build - report and diagnostics hardening
- Added prepared-folder checklist logging in the main enrichment algorithm for `population.tif`, `roads.gpkg`, `facilities.gpkg`, and `manifest.json`.
- Added prepared-data checklist logging at the end of the data-preparation algorithm with file sizes where available.
- Added clear enrichment input status logging for population, land cover, DEM, roads, and facilities.
- Added warnings when per-population indicators cannot be calculated because either population or OSM layers are missing.
- Reworked the HTML report into separated indicator groups: area, population, terrain, land cover, roads, facilities, and population-normalized access.
- Added per-field populated/empty status in the HTML report.
- Added interpretation notes for modelled WorldPop values, polygon nearest-distance behaviour, and per-10k-population indicators.

## Development build - CRS robustness fix
- Added automatic CRS sanity check for prepared OSM layers.
- If roads/facilities geometries look like longitude/latitude but CRS metadata is missing or inconsistent, the enrichment engine now interprets them as EPSG:4326 and transforms them to the target CRS before length/distance calculations.
- This prevents false huge nearest-distance values caused by lon/lat OSM coordinates being compared directly against projected target coordinates.

## Auto-data development build

- Added `Prepare open enrichment data (OSM auto-fetch)` Processing algorithm.
- Added OSM roads download using Overpass API.
- Added OSM facilities/POI download using Overpass API.
- Added default POI categories: schools, health, markets, water.
- Added custom OSM tag support using `key=value`, `key~regex`, or `key` syntax.
- Added local output/cache folder containing `roads.gpkg`, `facilities.gpkg`, and `manifest.json`.
- Added Overpass endpoint fallback and timeout/error handling.
- Added optional prepared-data folder input to the main enrichment algorithm.
- The main enrichment algorithm can now auto-load roads/facilities from a prepared folder when manual layers are not provided.
- Kept local manual layer override support.
- Kept score-free direct indicator outputs.
- Added `road_dens_km_km2` and `fac_dens_km2` fields for polygon-friendly interpretation.
- Added clearer HTML interpretation note for polygon targets where nearest-distance values are often zero.
- Strengthened cache behavior: cached OSM files are reused only when the previous `manifest.json` cache key matches the current AOI/options.
- Manifest/cache log now reports cached feature counts when available.


## 1.0.0

- First stable local-data enrichment engine.
- Supports point, line, and polygon targets.
- Supports population, DEM, land-cover, roads, and facilities as user-provided inputs.
- Adds optional QA enrichment zones, CSV summary, and HTML report.
- Removed built-in relative score to avoid misuse as a universal index.



### Population-normalized indicators
- Added `fac_per_10k_pop` = facilities per 10,000 people when population and facilities are available.
- Added `road_km_per_10k_pop` = road kilometres per 10,000 people when population and roads are available.
- Added these fields to CSV/HTML summaries and preview tables.

### Reliability refinements
- Removed public-facing private/test wording from algorithm help and metadata.
- Extended WorldPop year handling to support Global 2 candidate paths for 2015-2030 and legacy fallback candidates for 2000-2020.
- Added WorldPop file-size probing and download-size warnings before large raster downloads when server headers are available.
- Added chunked WorldPop download progress updates.
- Added clearer Overpass start/wait messages to reduce user confusion during blocking network requests.
- Added automatic prepared-folder population raster loading in the enrichment algorithm.
