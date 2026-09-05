# MR-VPP Version 6.0 processing chain

This repository contains the workflow used for the MODIS-based part of MR-VPP
6.0. It prepares MODIS MCD43A2 and MCD43A4 observations on the workflow tile
grid and spectrally adjusts red and near-infrared reflectance toward
Sentinel-3 OLCI consistency before PPI and phenology processing.

The operational record uses MODIS from 2000 through the latest available MODIS
input date. Sentinel-3 OLCI supplies the record from 2019 onward; this repository
does not contain that Sentinel-3 processing chain. It must therefore not be
interpreted as a MODIS-only implementation of the complete 2000-present record.

Input satellite data and generated MR-VPP products are not included. Users must
obtain source datasets under their applicable terms.

## Processing order

1. Prepare MCD43A4 red/NIR NBAR and MCD43A2 red/NIR quality plus local solar noon.
2. Apply the confirmed MR-VPP 6.0 MODIS-to-Sentinel-3 SBAF coefficients.
3. Calculate the temporal 95th percentile of valid NIR-red differences (MDVI_Q95).
4. Calculate scaled PPI and PPI QA for each available date.
5. Run the TIMESAT-backed phenology processing.
6. Optionally mosaic PPI/QA and VPP outputs using the separate merge scripts.

See [workflow details](docs/workflow.md), [configuration](docs/configuration.md),
and [data requirements](docs/data_requirements.md).

## Requirements

- Linux; the separately supplied compiled extension is Linux x86-64 and CPython
  3.11 specific.
- Python 3.11, NumPy, pandas, requests, Rasterio, netCDF4, and GDAL Python bindings.
- GDAL command-line programs used by the scripts.
- The authorised `cglopstsfprocess.cpython-311-x86_64-linux-gnu.so` extension for
  the TIMESAT stage. It is not distributed here.
- Sufficient storage and compute for multi-year, tile-based raster processing.
  The scripts do not provide enough evidence for a reliable capacity estimate.

Create the environment with:

```bash
conda env create -f environment.yml
conda activate mrvpp60-modis
```

Place an authorised copy of the compiled extension where Python can import it,
for example in the active environment's site-packages directory. Do not obtain
or redistribute it without permission from the Lund-EO research group.

## Configuration and execution

Copy both examples and replace every placeholder:

```bash
cp config/config.example.json config/config.local.json
cp config/timesatCfg.example.json config/timesatCfg.local.json
```

Set `end_year` to `null` to select the current calendar year. The scripts skip
dates for which a complete input set cannot be assembled; “current year” does
not imply that data exist through the current day. Supply the authoritative
tile list for the run rather than relying on the single placeholder tile.

Inspect the complete plan without downloading or processing data:

```bash
scripts/run_modis_sbaf_lsp_chain.sh --config config/config.local.json --dry-run
```

Run the workflow:

```bash
scripts/run_modis_sbaf_lsp_chain.sh --config config/config.local.json
```

Each Python stage supports `--help` and can be invoked independently. The two
merge scripts are optional downstream tools, not steps in the main launcher.

## Authentication

The download scripts read NASA Earthdata credentials from the standard
user-managed `~/.netrc` file. Keep that file outside the repository and restrict
its permissions. No credential belongs in either JSON configuration.

## Scientific conventions and limitations

- MCD43A4 reflectance nodata is 32767 and its scale is 0.0001.
- MCD43A2 QA values 0, 1, 2, and 3 are accepted; QA nodata is 255.
- MDVI_Q95 output nodata is -9999.0.
- PPI is multiplied by 1000 and stored as Int16 with nodata 32767; PPI QA is
  Byte with nodata 255.
- Optional mosaics use nearest-neighbour reprojection to the fixed EPSG:3035,
  300 m grid encoded in the merge scripts.
- Production equivalence has not been established without representative
  operational inputs and the authorised compiled extension.
- `timesat41_mp.py` accepts GeoTIFF and NetCDF modes, but the launcher uses the
  GeoTIFF PPI/QA path.
- The GeoTIFF TIMESAT path reads `p_lpbase` if supplied; the example retains the
  operational configuration key `p_low_percentile`, so the code's existing
  default `p_lpbase=0.0` remains in effect. This behavior is documented rather
  than silently changed.

## Licence and citation

The source scripts and documentation are licensed under the [MIT License](LICENSE).
The excluded compiled extension is not covered by that licence. Citation
metadata are provided in [CITATION.cff](CITATION.cff).
