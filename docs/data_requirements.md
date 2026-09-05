# Data requirements

The downloaders target MCD43A4 NBAR band 1 (red), band 2 (NIR), MCD43A2 band
quality for those two bands, and MCD43A2 local solar noon. They create 3360 by
3360 pixel geographic tiles at the pixel size encoded in the scripts.

Prepared filenames follow these forms:

```text
MCD43A4_NBAR_CGLS_PBV_TILE_RED_toc_YYYY_DDD.tif
MCD43A4_NBAR_CGLS_PBV_TILE_NIR_toc_YYYY_DDD.tif
MCD43A4_NBAR_CGLS_PBV_TILE_RED_sbaf_YYYY_DDD.tif
MCD43A4_NBAR_CGLS_PBV_TILE_NIR_sbaf_YYYY_DDD.tif
MCD43A2_NBAR_CGLS_PBV_TILE_RED_QA_YYYY_DDD.tif
MCD43A2_NBAR_CGLS_PBV_TILE_NIR_QA_YYYY_DDD.tif
MCD43A2_NBAR_CGLS_PBV_TILE_SZA_YYYY_DDD.tif
MODIS_sbaf_TILE.YYYYDDD-ppi.tif
MODIS_sbaf_TILE.YYYYDDD-qa.tif
```

The scripts require consistent raster dimensions, geotransforms, projections,
and date coverage. They skip incomplete date sets rather than synthesizing an
observation. Optional VPP water masking expects one land-cover raster per tile,
named `TILE_LC_resampled.tif`, with water class 210.

No satellite data, land-cover data, generated products, or production logs are
distributed by this repository.
