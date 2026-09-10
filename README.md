# OpenGeoEnrich
### Turn locations into contextual indicators.

A QGIS Processing plugin that enriches points, lines and polygons with population, land-cover, terrain, road and facility indicators. Use your own local data or prepare supported open datasets inside QGIS.

**QGIS 3.22+ declared in plugin metadata** · Python · QGIS Processing

[Detailed guide](docs/guide.md) · [Report a problem](https://github.com/mahmoodirfan/OpenGeoEnrich/issues) · [Contribute](CONTRIBUTING.md)

## Start here

1. Start with a small target layer in an appropriate **projected CRS with metre units**.
2. Open **OpenGeoEnrich → Data preparation → Prepare OpenGeoEnrich data** in the Processing Toolbox.
3. Choose **Quick** mode for OSM roads/facilities and WorldPop, then select a cache folder. Review the log for failed sources.
4. Open **OpenGeoEnrich → GeoEnrichment → Enrich layer with open geospatial data**.
5. Select your target layer and prepared folder. Set a buffer for point/line targets; polygon targets use their polygon zones.
6. Save the enriched layer and, optionally, QA zones, CSV and HTML report.

Already have local data? Skip preparation and supply the population, land-cover, DEM, road or facility layers directly.

## Install

In QGIS, open **Plugins → Manage and Install Plugins** and search for **OpenGeoEnrich**. If a compatible listing is unavailable, install from this repository:

1. Download and extract the source archive.
2. Rename the extracted plugin directory to `OpenGeoEnrich` (remove a branch suffix such as `-main`).
3. In QGIS, open **Settings → User Profiles → Open Active Profile Folder**.
4. Copy the directory into `python/plugins/`, creating those subfolders if needed. `metadata.txt` and `__init__.py` must sit directly inside `python/plugins/OpenGeoEnrich/`.
5. Restart QGIS and enable **OpenGeoEnrich** in the plugin manager.

A GitHub source ZIP is not necessarily a correctly packaged QGIS install ZIP. Use the extracted-folder steps above for source downloads. Declared minimum versions are not a substitute for testing your QGIS build.

## What you get

| Output | Use |
| :--- | :--- |
| Enriched vector layer | Contextual indicators attached to each target feature |
| Optional QA zones | Inspect the polygons/buffers used for aggregation |
| Optional CSV | Work with indicators outside QGIS |
| Optional HTML report | Review indicator groups and populated/empty fields |
| Preparation folder and `manifest.json` | Reuse downloaded inputs and inspect recorded provenance |

## Before interpreting results

- Use **Quick** for initial population/OSM runs; **Full** adds WorldCover and DEM-derived terrain; **Custom** selects individual sources.
- Missing optional data can leave indicators empty. Empty values are not zeros and do not mean absence.
- Population sums require population-count cells; do not interpret sums of population-density values as people.
- OSM coverage varies. Nearest-feature distance is not travel time or network accessibility.
- Use metre-based projected coordinates for metric buffers and distance/area indicators. Geographic coordinates can produce misleading units.
- The supplied WorldCover grouping example in the detailed guide follows the dataset's class codes; adapt groups explicitly for other land-cover products.
- Downloads depend on external services and AOI size. Cached or local inputs are useful when internet access is unreliable.
- Review the detailed guide's larger-AOI and download checks before an operational rollout.

## Documentation & support

The [detailed guide](docs/guide.md) contains extended settings, interpretation examples and workflow notes.

For a bug report, include your QGIS version, operating system, plugin version, parameters, Processing log and a small shareable example. See [contribution guidance](CONTRIBUTING.md).

## Related tools

[RasterTrend](https://github.com/mahmoodirfan/RasterTrend) · [TrendShift](https://github.com/mahmoodirfan/TrendShift) · [OpenGeoEnrich](https://github.com/mahmoodirfan/OpenGeoEnrich) · [spatialdrought](https://github.com/mahmoodirfan/spatialdrought)

## Author & license

**[Irfan Mahmood](https://github.com/mahmoodirfan)** · Remote Sensing & GIS Specialist  
[Email](mailto:irfan-mahmood@outlook.com) · [License](LICENSE)

For research use, cite the repository and record the version or commit you used. Existing citation details are retained in the detailed guide where provided.
