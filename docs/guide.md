# Detailed guide

[← Back to the overview](../README.md)

Read the current implementation notes in the overview before interpreting results.

# OpenGeoEnrich

**OpenGeoEnrich** is a QGIS Processing Toolbox plugin for open-source geoenrichment. It enriches any point, line, or polygon layer with contextual indicators from open geospatial datasets.

This build adds a hardened **auto-data preparation** workflow for OpenStreetMap roads/facilities using the Overpass API, optional WorldPop population preparation, optional ESA WorldCover 10 m land-cover preparation, and optional Copernicus DEM GLO-30 preparation with derived slope and terrain ruggedness rasters.

## Processing Toolbox paths

```text
OpenGeoEnrich > Data preparation > Prepare OpenGeoEnrich data
OpenGeoEnrich > GeoEnrichment > Enrich layer with open geospatial data
```


## Preparation modes

OpenGeoEnrich now supports three preparation modes:

- **Custom**: use the individual checkboxes for roads, facilities, WorldPop, WorldCover, and DEM.
- **Quick**: prepares OSM roads/facilities and WorldPop only. Use this first for slow internet, large AOIs, or basic exposure/access checks.
- **Full**: prepares OSM roads/facilities, WorldPop, ESA WorldCover, and Copernicus DEM with slope/TRI. Use this for the complete enrichment stack after the AOI has been tested.

The **Continue if an optional dataset fails** option lets the preparation tool keep successful datasets even if one optional source fails because of internet, server, or AOI-size problems. The Processing log will report what succeeded and what failed.

## Default styling

When QGIS exposes the Processing result layer after the run, OpenGeoEnrich applies a lightweight graduated style to the enriched output using the first available field from this priority list: `pop_dens_km2`, `built_crop_pct`, `natural_pct`, `road_dens_km_km2`. Styling is only a visual starting point; all raw fields remain unchanged.

## Recommended workflow

### 1. Prepare open data

Run:

```text
OpenGeoEnrich > Data preparation > Prepare OpenGeoEnrich data
```

Inputs:

- Study area / target layer
- Output/cache folder
- Preparation mode: Custom, Quick, or Full
- Continue if an optional dataset fails: yes/no
- Fetch OSM roads: yes/no
- Fetch OSM facilities/POIs: yes/no
- Facility categories: `schools,health,markets,water`
- Optional custom OSM tags
- Timeout seconds
- Use cached files if available
- Optional: Fetch WorldPop population raster
- Optional: Fetch ESA WorldCover land-cover raster
- Optional: Fetch Copernicus DEM GLO-30 and derive slope/TRI

Outputs written to the selected folder, depending on selected options:

```text
roads.gpkg
facilities.gpkg
population.tif
landcover.tif
dem.tif
slope.tif
tri.tif
manifest.json
```

The manifest records the AOI bbox, source, date/time, cache status, and output paths.

### 2. Enrich target layer

Run:

```text
OpenGeoEnrich > GeoEnrichment > Enrich layer with open geospatial data
```

You can either provide local layers manually or select the prepared OpenGeoEnrich data folder created in step 1. If manual inputs are empty, the algorithm automatically checks the prepared folder and tries to load:

```text
<prepared folder>/population.tif
<prepared folder>/landcover.tif
<prepared folder>/roads.gpkg
<prepared folder>/facilities.gpkg
<prepared folder>/dem.tif
<prepared folder>/slope.tif
<prepared folder>/tri.tif
```

The algorithm logs a prepared-folder checklist before processing so missing data are visible in the Processing log.

## What the plugin calculates

For each target feature, OpenGeoEnrich can calculate:

- zone area in hectares and square kilometres
- population sum, mean raster value, and population density per km²
- elevation mean, minimum, and maximum from DEM
- slope mean, minimum, and maximum in degrees
- terrain ruggedness / roughness mean, minimum, and maximum
- ESA WorldCover majority class, majority label, and class variety
- land-cover group percentages and pixel counts
- simplified land-cover summaries: `natural_pct` and `built_crop_pct`
- road length inside each zone
- road density in km per km²
- nearest road distance
- facility/POI count inside each zone
- facility density per km²
- nearest facility/POI distance
- facilities per 10,000 people, when population and facilities are available
- road kilometres per 10,000 people, when population and roads are available

The HTML report separates indicators into clear groups: area, population, terrain, land cover, roads, facilities, and population-normalized access. It also reports whether each field is populated or empty.

## Quick mode test

Use Quick mode when you want a fast, practical enrichment run with only population and OSM context. A successful Quick run prepares:

```text
population.tif
roads.gpkg
facilities.gpkg
manifest.json
_cache/
```

It intentionally does not prepare `landcover.tif`, `dem.tif`, `slope.tif`, or `tri.tif`. In the enrichment output, population and OSM fields should be populated while terrain and land-cover fields remain empty. This confirms Quick mode is independent from Full mode and works for users who do not want the larger downloads.

## Failure-handling test

To verify soft-fail behavior, run Quick mode with **Continue if an optional dataset fails** enabled and use an invalid WorldPop ISO3 code such as `XXX`. Expected behavior:

- OSM roads/facilities are still prepared if Overpass succeeds.
- `population.tif` is missing because WorldPop fails.
- the Processing algorithm finishes instead of crashing.
- the enrichment output still contains road/facility indicators, while population and per-10,000 population indicators remain empty.

This protects users from complete failure when one optional web source is unavailable, mistyped, blocked, or too slow.

## OSM auto-fetch notes

The OSM downloader uses Overpass API endpoints. Overpass is a public community service and may fail or timeout for large areas, high server load, or unreliable internet.

Best practice:

- test with a small AOI first;
- use cache reuse for repeated runs; cached files are reused only when the stored AOI/options cache key matches the current request;
- increase timeout for larger AOIs;
- provide local roads/facility layers as fallback if Overpass fails.

This build prioritizes OSM, WorldPop, and Copernicus DEM because roads, facilities, population, and terrain are core requirements for out-of-the-box open geoenrichment.

## Custom OSM tag syntax

The custom tag box accepts one tag per line or semicolon-separated values:

```text
amenity=bank
shop~supermarket|convenience
healthcare
```

Supported syntax:

- `key=value` for exact tag value;
- `key~regex` for Overpass regex matching;
- `key` for any feature containing that key.

## Land-cover group syntax

The land-cover grouping field accepts a semicolon-separated string:

```text
Tree=10;Shrubland=20;Grassland=30;Cropland=40;Builtup=50;Bare=60;SnowIce=70;Water=80;Wetland=90;Mangroves=95;MossLichen=100
```


### Simplified land-cover summary fields

When an ESA WorldCover land-cover raster is used, OpenGeoEnrich also writes two quick interpretation fields:

- `natural_pct` = Tree + Shrubland + Grassland + Wetland + Mangroves + Moss/Lichen
- `built_crop_pct` = Built-up + Cropland

These are not suitability scores. They are transparent summary percentages intended for rapid screening and reporting.

Example for ESA WorldCover:

```text
Tree=10;Shrubland=20;Grassland=30;Cropland=40;Builtup=50;Bare=60;SnowIce=70;Water=80;Wetland=90;Mangroves=95;MossLichen=100
```

## Important CRS note

For correct metric buffers, road lengths, and nearest distances, use a projected CRS such as UTM.

If the target layer is in a geographic CRS, QGIS layer units are used and distance/length outputs may not be in metres/kilometres. The plugin will warn users when a geographic CRS is detected.

## Scoring note

OpenGeoEnrich intentionally does not create a built-in suitability, vulnerability, or priority score in this build. It outputs direct, auditable indicators only. Users can build their own weighted index after validating the logic for their specific application.

## Current limitations

- OSM roads, OSM facilities, WorldPop population, ESA WorldCover land cover, and Copernicus DEM terrain are supported by the auto-data workflow.
- ESA WorldCover auto-download is supported for 2020 v100 and 2021 v200; users can still provide local land-cover rasters.
- Overpass requests may timeout for large AOIs.
- True background-task download handling is a next reliability target; QGIS Processing may still appear busy during network requests.
- Travel-time/network routing is not included.

## Source code and issue tracker


## Repository-readiness checklist

Before submitting a public QGIS repository update, test at least:

- Quick mode on a fresh empty folder.
- Full mode on a fresh empty folder.
- soft-fail mode with one intentionally broken optional dataset.
- a larger AOI than the sample Swabi tehsil test.
- a run using cached prepared data.
- a run where local user-supplied layers override prepared auto-data.

The current build is designed as a strong beta candidate, but larger-AOI and no/poor-internet behavior should be tested before official repository upload.

- Repository: https://github.com/mahmoodirfan/OpenGeoEnrich
- Issues: https://github.com/mahmoodirfan/OpenGeoEnrich/issues

## License

GNU General Public License v2.0 or later (GPL-2.0-or-later).


## WorldPop auto-download notes

OpenGeoEnrich can optionally prepare a WorldPop population raster for the selected study area.

- Global 2 candidate URLs are used for 2015-2030 where available.
- Legacy Global 1 candidate URLs are retained for 2000-2020.
- Country rasters can be large; the tool attempts to report file size before the download starts.
- The output `population.tif` is clipped to the AOI bounding box when GDAL clipping succeeds; otherwise the full country raster is retained as fallback.
- WorldPop values are modelled population estimates, not census counts.


## Copernicus DEM auto-download notes

OpenGeoEnrich can optionally prepare Copernicus DEM GLO-30 terrain rasters for the selected study area.

- The downloader uses public 1-degree Copernicus DEM COG tiles.
- Tiles are cached locally under `_cache/copernicus_dem_30m`.
- The prepared output `dem.tif` is clipped to the AOI bounding box when GDAL clipping succeeds.
- The tool also derives `slope.tif` and `tri.tif` when the relevant GDAL terrain algorithms are available in QGIS.
- `slope.tif` is in degrees. For auto-downloaded Copernicus DEM tiles, OpenGeoEnrich applies a metre-per-degree GDAL slope scale so geographic DEM coordinates do not create false near-90 degree slopes. `tri.tif` is a terrain ruggedness / roughness indicator derived from local DEM variation.
- Large AOIs may require multiple DEM tiles; start with a small AOI for testing.
