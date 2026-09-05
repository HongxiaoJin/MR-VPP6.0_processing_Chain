#!/usr/bin/env python3
# ============================================================
# SBAF correction for MODIS MCD43A4 → Sentinel-3
# Tile-based, CLI-driven
#
# Example:
#   python sbaf_modis_mcd43a4_to_s3.py --tile X22Y03
# ============================================================

import os
import glob
import argparse
import numpy as np
try:
    from osgeo import gdal
except ModuleNotFoundError:
    gdal = None

if gdal is not None:
    gdal.UseExceptions()

# ============================================================
# CONSTANTS (MUST MATCH PRODUCER SCRIPT)
# ============================================================
MODIS_SCALE  = 1e-4
OUTPUT_SCALE = 1e4
NODATA       = 32767

# ============================================================
# Confirmed operational MR-VPP 6.0 SBAF coefficients
# ============================================================
COEF_RED = dict(b0=1.063247, b1=0.255060, b2=-0.471804)
COEF_NIR = dict(b0=1.044383, b1=0.007799, b2=0.0)

# ============================================================
# CLI
# ============================================================
def parse_args():
    p = argparse.ArgumentParser(
        description="Apply SBAF correction to MODIS MCD43A4 (RED/NIR)"
    )
    p.add_argument(
        "--tile",
        required=True,
        help="PROBA-V / Sentinel-3 tile, e.g. X19Y01"
    )
    p.add_argument(
        "--base-dir",
        required=True,
        help="Base directory containing per-tile folders"
    )
    return p.parse_args()

# ============================================================
# SBAF FUNCTIONS
# ============================================================
def ndvi(red, nir):
    v = (nir - red) / (nir + red + 1e-10)
    return np.clip(v, -1.0, 1.0)

def sbaf_red(red, nir):
    v = ndvi(red, nir)
    return red * (COEF_RED["b0"] + COEF_RED["b1"] * v + COEF_RED["b2"] * v**2)

def sbaf_nir(red, nir):
    v = ndvi(red, nir)
    return nir * (COEF_NIR["b0"] + COEF_NIR["b1"] * v + COEF_NIR["b2"] * v**2)

# ============================================================
# CORE PROCESSING
# ============================================================
def process_pair(red_path, nir_path, red_out, nir_out):
    red_ds = gdal.Open(red_path, gdal.GA_ReadOnly)
    nir_ds = gdal.Open(nir_path, gdal.GA_ReadOnly)

    red_i = red_ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
    nir_i = nir_ds.GetRasterBand(1).ReadAsArray().astype(np.float32)

    # nodata → NaN
    red_i[red_i == NODATA] = np.nan
    nir_i[nir_i == NODATA] = np.nan

    # scale to reflectance
    red = red_i * MODIS_SCALE
    nir = nir_i * MODIS_SCALE

    valid = (
        np.isfinite(red) &
        np.isfinite(nir) &
        (red > 0.0) & (nir > 0.0) &
        (red < 1.2) & (nir < 1.2)
    )

    red_sbaf = np.full(red.shape, np.nan, np.float32)
    nir_sbaf = np.full(nir.shape, np.nan, np.float32)

    if np.any(valid):
        red_sbaf[valid] = sbaf_red(red[valid], nir[valid])
        nir_sbaf[valid] = sbaf_nir(red[valid], nir[valid])

    # back to Int16
    red_o = np.full(red.shape, NODATA, np.int16)
    nir_o = np.full(nir.shape, NODATA, np.int16)

    ok = np.isfinite(red_sbaf) & np.isfinite(nir_sbaf)
    red_o[ok] = np.clip(np.round(red_sbaf[ok] * OUTPUT_SCALE), 0, 32766).astype(np.int16)
    nir_o[ok] = np.clip(np.round(nir_sbaf[ok] * OUTPUT_SCALE), 0, 32766).astype(np.int16)

    driver = gdal.GetDriverByName("GTiff")

    for out_path, data, ref_ds in [
        (red_out, red_o, red_ds),
        (nir_out, nir_o, nir_ds),
    ]:
        ds = driver.Create(
            out_path,
            ref_ds.RasterXSize,
            ref_ds.RasterYSize,
            1,
            gdal.GDT_Int16,
            options=["TILED=YES", "COMPRESS=LZW"]
        )
        ds.SetGeoTransform(ref_ds.GetGeoTransform())
        ds.SetProjection(ref_ds.GetProjection())
        band = ds.GetRasterBand(1)
        band.WriteArray(data)
        band.SetNoDataValue(NODATA)
        ds = None

    red_ds = None
    nir_ds = None

# ============================================================
# DRIVER
# ============================================================
def main():
    args = parse_args()
    if gdal is None:
        raise RuntimeError("GDAL Python bindings are required for processing")
    tile = args.tile

    base_path = os.path.join(args.base_dir, tile)

    RED_IN_DIR  = os.path.join(base_path, "RED_toc")
    NIR_IN_DIR  = os.path.join(base_path, "NIR_toc")
    RED_OUT_DIR = os.path.join(base_path, "RED_sbaf")
    NIR_OUT_DIR = os.path.join(base_path, "NIR_sbaf")

    os.makedirs(RED_OUT_DIR, exist_ok=True)
    os.makedirs(NIR_OUT_DIR, exist_ok=True)

    red_files = sorted(glob.glob(os.path.join(RED_IN_DIR, "*.tif")))

    if not red_files:
        raise RuntimeError(f"No RED_toc files found for tile {tile}")

    print(f"[SBAF] Tile {tile}: {len(red_files)} files")

    for red_path in red_files:
        red_fname = os.path.basename(red_path)
        nir_fname = red_fname.replace("_RED_toc_", "_NIR_toc_")
        nir_path = os.path.join(NIR_IN_DIR, nir_fname)

        if not os.path.exists(nir_path):
            print(f"[SBAF] Missing NIR for {red_fname}")
            continue

        red_out = os.path.join(
            RED_OUT_DIR,
            red_fname.replace("_RED_toc_", "_RED_sbaf_")
        )
        nir_out = os.path.join(
            NIR_OUT_DIR,
            nir_fname.replace("_NIR_toc_", "_NIR_sbaf_")
        )

        if os.path.exists(red_out) and os.path.exists(nir_out):
            continue

        print(f"[SBAF] Processing {red_fname}")
        process_pair(red_path, nir_path, red_out, nir_out)

    print(f"[SBAF] DONE for tile {tile}")

# ============================================================
if __name__ == "__main__":
    main()
