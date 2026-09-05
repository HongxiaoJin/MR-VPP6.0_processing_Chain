#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MDVI_Q95 computation from MODIS MCD43A4 SBAF RED/NIR GeoTIFF time series.

MDVI_Q95 = 95th percentile over time of:
    DVI = rho_NIR - rho_RED

Conditions:
- Reflectance nodata: 32767 (Int16)
- Scale factor: 0.0001
- QA valid values: 0,1,2,3
- QA fill: 4
- QA nodata: 255

Block-wise processing to limit memory usage.

python mdvi_q95_modis_sbaf.py \
  --tile X19Y01 \
  --nir-dir DATA_ROOT/MCD43A4_Sen3_tile/X19Y01/NIR_sbaf \
  --red-dir DATA_ROOT/MCD43A4_Sen3_tile/X19Y01/RED_sbaf \
  --nir-qa-dir DATA_ROOT/MCD43A2_Sen3_tile/X19Y01/NIR_QA \
  --red-qa-dir DATA_ROOT/MCD43A2_Sen3_tile/X19Y01/RED_QA \
  --out DATA_ROOT/MDVI/MDVI_Q95_X19Y01_2000_2025.tif \
  --block 840

Author: Hongxiao Jin (adapted & cleaned)
"""

import argparse
import re
from pathlib import Path
import numpy as np
try:
    from osgeo import gdal
except ModuleNotFoundError:
    gdal = None

if gdal is not None:
    gdal.UseExceptions()

# -------------------------------
# Constants
# -------------------------------
REF_NODATA = 32767
QA_NODATA = 255
VALID_QA = (0, 1, 2, 3)
SCALE = 0.0001
OUT_NODATA = -9999.0


# -------------------------------
# Helpers
# -------------------------------
def parse_year_doy(fname):
    """
    Extract (year, doy) from filename ending with _YYYY_DDD.tif
    """
    m = re.search(r"_(\d{4})_(\d{3})\.tif$", fname)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def build_path(base_dir, tile, tag, year, doy):
    doy = f"{doy:03d}"
    year = f"{year:04d}"

    if tag in ("RED_sbaf", "NIR_sbaf"):
        name = f"MCD43A4_NBAR_CGLS_PBV_{tile}_{tag}_{year}_{doy}.tif"
    elif tag in ("RED_QA", "NIR_QA"):
        name = f"MCD43A2_NBAR_CGLS_PBV_{tile}_{tag}_{year}_{doy}.tif"
    else:
        raise ValueError(tag)

    return base_dir / name


# -------------------------------
# Main
# -------------------------------
def main():
    parser = argparse.ArgumentParser(description="Compute MDVI_Q95 from MODIS SBAF data")
    parser.add_argument("--tile", required=True)
    parser.add_argument("--nir-dir", required=True)
    parser.add_argument("--red-dir", required=True)
    parser.add_argument("--nir-qa-dir", required=True)
    parser.add_argument("--red-qa-dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--year-min", type=int, default=2000)
    parser.add_argument("--year-max", type=int, default=2025)
    parser.add_argument("--block", type=int, default=512)
    parser.add_argument("--compress", default="LZW", choices=["LZW", "DEFLATE", "NONE"])
    args = parser.parse_args()
    if gdal is None:
        raise RuntimeError("GDAL Python bindings are required for processing")

    nir_dir = Path(args.nir_dir)
    red_dir = Path(args.red_dir)
    nir_qa_dir = Path(args.nir_qa_dir)
    red_qa_dir = Path(args.red_qa_dir)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # -------------------------------
    # Discover dates from NIR
    # -------------------------------
    dates = []
    for f in nir_dir.glob("*.tif"):
        y, d = parse_year_doy(f.name)
        if y is not None and args.year_min <= y <= args.year_max:
            dates.append((y, d))
    dates = sorted(set(dates))

    if not dates:
        raise RuntimeError("No valid NIR dates found")

    # -------------------------------
    # Keep only dates with full data
    # -------------------------------
    files = []
    for y, d in dates:
        nir = build_path(nir_dir, args.tile, "NIR_sbaf", y, d)
        red = build_path(red_dir, args.tile, "RED_sbaf", y, d)
        nq = build_path(nir_qa_dir, args.tile, "NIR_QA", y, d)
        rq = build_path(red_qa_dir, args.tile, "RED_QA", y, d)

        if nir.exists() and red.exists() and nq.exists() and rq.exists():
            files.append((y, d, nir, red, nq, rq))

    if not files:
        raise RuntimeError("No dates with complete RED/NIR/QA sets")

    print(f"[MDVI_Q95] Tile {args.tile}: {len(files)} valid dates")

    # -------------------------------
    # Use first file as template
    # -------------------------------
    tmpl = gdal.Open(str(files[0][2]), gdal.GA_ReadOnly)
    xsize, ysize = tmpl.RasterXSize, tmpl.RasterYSize
    gt = tmpl.GetGeoTransform()
    proj = tmpl.GetProjection()

    # -------------------------------
    # Create output
    # -------------------------------
    driver = gdal.GetDriverByName("GTiff")
    co = ["TILED=YES", "BIGTIFF=IF_SAFER"]
    if args.compress != "NONE":
        co.append(f"COMPRESS={args.compress}")

    out_ds = driver.Create(
        str(out_path), xsize, ysize, 1, gdal.GDT_Float32, options=co
    )
    out_ds.SetGeoTransform(gt)
    out_ds.SetProjection(proj)

    band = out_ds.GetRasterBand(1)
    band.SetNoDataValue(OUT_NODATA)
    band.SetDescription("MDVI_Q95 = 95th percentile of (NIR-RED)")

    out_ds.SetMetadata({
        "INDEX": "MDVI_Q95",
        "QUANTILE": "0.95",
        "TILE": args.tile,
        "YEAR_MIN": str(args.year_min),
        "YEAR_MAX": str(args.year_max),
        "QA_VALID": "0,1,2,3",
        "REF_SCALE": "0.0001",
    })

    # -------------------------------
    # Block-wise processing
    # -------------------------------
    bs = args.block
    for y0 in range(0, ysize, bs):
        ywin = min(bs, ysize - y0)
        for x0 in range(0, xsize, bs):
            xwin = min(bs, xsize - x0)

            dvi_stack = []

            for _, _, nir_f, red_f, nq_f, rq_f in files:
                nir_ds = gdal.Open(str(nir_f))
                red_ds = gdal.Open(str(red_f))
                nq_ds = gdal.Open(str(nq_f))
                rq_ds = gdal.Open(str(rq_f))

                nir = nir_ds.GetRasterBand(1).ReadAsArray(x0, y0, xwin, ywin)
                red = red_ds.GetRasterBand(1).ReadAsArray(x0, y0, xwin, ywin)
                nq = nq_ds.GetRasterBand(1).ReadAsArray(x0, y0, xwin, ywin).astype(np.uint8)
                rq = rq_ds.GetRasterBand(1).ReadAsArray(x0, y0, xwin, ywin).astype(np.uint8)

                valid = (
                    (nir != REF_NODATA) &
                    (red != REF_NODATA) &
                    np.isin(nq, VALID_QA) &
                    np.isin(rq, VALID_QA)
                )

                if np.any(valid):
                    dvi = (nir.astype(np.float32) - red.astype(np.float32)) * SCALE
                    dvi[~valid] = np.nan
                    dvi_stack.append(dvi)

                nir_ds = red_ds = nq_ds = rq_ds = None

            if dvi_stack:
                stack = np.stack(dvi_stack, axis=0)
                mdvi = np.nanquantile(stack, 0.95, axis=0)
                mdvi[np.isnan(mdvi)] = OUT_NODATA
            else:
                mdvi = np.full((ywin, xwin), OUT_NODATA, dtype=np.float32)

            band.WriteArray(mdvi, xoff=x0, yoff=y0)

        print(f"[MDVI_Q95] processed rows {y0}–{y0 + ywin - 1}")

    band.FlushCache()
    out_ds.FlushCache()
    out_ds = None
    tmpl = None

    print(f"[MDVI_Q95] DONE → {out_path}")


if __name__ == "__main__":

    from datetime import datetime
    import time

    t_start = time.time()
    print(f"[MDVI_Q95] START: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    main()

    t_end = time.time()
    elapsed = (t_end - t_start) / 60.0

    print(f"[MDVI_Q95] END  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[MDVI_Q95] TOTAL RUNTIME: {elapsed:.2f} minutes")
