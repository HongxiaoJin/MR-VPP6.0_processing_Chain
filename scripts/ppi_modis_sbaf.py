#!/usr/bin/env python3
# ============================================================
# PPI computation from MODIS SBAF RED/NIR + MDVI + SZA
# Output: PPI (Int16) + PPI_QA (UInt8) GeoTIFFs
"""
python ppi_modis_sbaf.py \
  --tile X20Y00 \
  --red-dir DATA_ROOT/MCD43A4_Sen3_tile/X20Y00/RED_sbaf \
  --nir-dir DATA_ROOT/MCD43A4_Sen3_tile/X20Y00/NIR_sbaf \
  --red-qa-dir DATA_ROOT/MCD43A2_Sen3_tile/X20Y00/RED_QA \
  --nir-qa-dir DATA_ROOT/MCD43A2_Sen3_tile/X20Y00/NIR_QA \
  --sza-dir DATA_ROOT/MCD43A2_Sen3_tile/X20Y00/SZA \
  --mdvi DATA_ROOT/MDVI/MDVI_Q95_X20Y00_2000_2025.tif \
  --out-dir DATA_ROOT \
  --block 840

"""
# ============================================================

import argparse
import re
from pathlib import Path
import numpy as np
try:
    from osgeo import gdal
except ModuleNotFoundError:
    gdal = None
from datetime import datetime
import time

if gdal is not None:
    gdal.UseExceptions()

# ------------------------------------------------------------
# Constants
# ------------------------------------------------------------
REF_NODATA = 32767
QA_NODATA  = 255
VALID_QA   = (0, 1, 2, 3)
REF_SCALE  = 0.0001
OUT_NODATA = 32767          # Int16 nodata
PPI_SCALE  = 1000.0         # scale factor for output PPI

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def parse_year_doy(fname):
    m = re.search(r"_(\d{4})_(\d{3})\.tif$", fname)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def build_refl_path(base, tile, tag, year, doy):
    return base / f"MCD43A4_NBAR_CGLS_PBV_{tile}_{tag}_{year}_{doy:03d}.tif"


def build_qa_path(base, tile, tag, year, doy):
    return base / f"MCD43A2_NBAR_CGLS_PBV_{tile}_{tag}_{year}_{doy:03d}.tif"


def build_sza_path(base, tile, year, doy):
    return base / f"MCD43A2_NBAR_CGLS_PBV_{tile}_SZA_{year}_{doy:03d}.tif"


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Compute PPI from MODIS SBAF data")
    parser.add_argument("--tile", required=True)
    parser.add_argument("--red-dir", required=True)
    parser.add_argument("--nir-dir", required=True)
    parser.add_argument("--red-qa-dir", required=True)
    parser.add_argument("--nir-qa-dir", required=True)
    parser.add_argument("--sza-dir", required=True)
    parser.add_argument("--mdvi", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--block", type=int, default=512)
    args = parser.parse_args()
    if gdal is None:
        raise RuntimeError("GDAL Python bindings are required for processing")

    red_dir    = Path(args.red_dir)
    nir_dir    = Path(args.nir_dir)
    red_qa_dir = Path(args.red_qa_dir)
    nir_qa_dir = Path(args.nir_qa_dir)
    sza_dir    = Path(args.sza_dir)


    out_root = Path(args.out_dir) / "PPI_QA" / args.tile
    out_ppi  = out_root / "PPI"
    out_qa   = out_root / "QA"

    out_ppi.mkdir(parents=True, exist_ok=True)
    out_qa.mkdir(parents=True, exist_ok=True)


    # --------------------------------------------------------
    # Load MDVI
    # --------------------------------------------------------
    mdvi_ds = gdal.Open(args.mdvi, gdal.GA_ReadOnly)
    MDVI = mdvi_ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
    MDVI_origin = MDVI.copy()

    MDVI = np.clip(MDVI, 0.18, 0.99)
    MDVI += 0.005
    DVI_soil = min(0.09, np.nanmax(MDVI) / 4)

    gt = mdvi_ds.GetGeoTransform()
    proj = mdvi_ds.GetProjection()
    xsize, ysize = mdvi_ds.RasterXSize, mdvi_ds.RasterYSize
    mdvi_ds = None

    # --------------------------------------------------------
    # Discover dates from RED directory
    # --------------------------------------------------------
    dates = []
    for f in red_dir.glob("*.tif"):
        y, d = parse_year_doy(f.name)
        if y is not None:
            dates.append((y, d))
    dates = sorted(set(dates))

    print(f"[PPI] {len(dates)} candidate dates found")

    # --------------------------------------------------------
    # Loop over dates
    # --------------------------------------------------------
    for year, doy in dates:
        red_f = build_refl_path(red_dir, args.tile, "RED_sbaf", year, doy)
        nir_f = build_refl_path(nir_dir, args.tile, "NIR_sbaf", year, doy)
        rq_f  = build_qa_path(red_qa_dir, args.tile, "RED_QA", year, doy)
        nq_f  = build_qa_path(nir_qa_dir, args.tile, "NIR_QA", year, doy)
        sza_f = build_sza_path(sza_dir, args.tile, year, doy)

        if not all(p.exists() for p in [red_f, nir_f, rq_f, nq_f, sza_f]):
            continue

        print(f"[PPI] Processing {args.tile} {year}_{doy:03d}")

        datestr = f"{year}{doy:03d}"

        out_ppi_f = out_ppi / f"MODIS_sbaf_{args.tile}.{datestr}-ppi.tif"
        out_qa_f  = out_qa  / f"MODIS_sbaf_{args.tile}.{datestr}-qa.tif"

        driver = gdal.GetDriverByName("GTiff")

        ds_ppi = driver.Create(
            str(out_ppi_f), xsize, ysize, 1, gdal.GDT_Int16,
            options=["TILED=YES", "COMPRESS=LZW", "BIGTIFF=IF_SAFER"]
        )
        ds_ppi.SetGeoTransform(gt)
        ds_ppi.SetProjection(proj)
        band_ppi = ds_ppi.GetRasterBand(1)
        band_ppi.SetNoDataValue(OUT_NODATA)

        ds_qa = driver.Create(
            str(out_qa_f), xsize, ysize, 1, gdal.GDT_Byte,
            options=["TILED=YES", "COMPRESS=LZW", "BIGTIFF=IF_SAFER"]
        )
        ds_qa.SetGeoTransform(gt)
        ds_qa.SetProjection(proj)
        band_qa = ds_qa.GetRasterBand(1)
        band_qa.SetNoDataValue(QA_NODATA)

        red_ds = gdal.Open(str(red_f))
        nir_ds = gdal.Open(str(nir_f))
        rq_ds  = gdal.Open(str(rq_f))
        nq_ds  = gdal.Open(str(nq_f))
        sza_ds = gdal.Open(str(sza_f))

        bs = args.block

        for y0 in range(0, ysize, bs):
            ywin = min(bs, ysize - y0)
            for x0 in range(0, xsize, bs):
                xwin = min(bs, xsize - x0)

                RED = red_ds.GetRasterBand(1).ReadAsArray(x0, y0, xwin, ywin)
                NIR = nir_ds.GetRasterBand(1).ReadAsArray(x0, y0, xwin, ywin)
                RQ  = rq_ds.GetRasterBand(1).ReadAsArray(x0, y0, xwin, ywin)
                NQ  = nq_ds.GetRasterBand(1).ReadAsArray(x0, y0, xwin, ywin)
                SZA = sza_ds.GetRasterBand(1).ReadAsArray(x0, y0, xwin, ywin)

                MDVI_blk  = MDVI[y0:y0+ywin, x0:x0+xwin]
                MDVIo_blk = MDVI_origin[y0:y0+ywin, x0:x0+xwin]

                PPI = np.full((ywin, xwin), np.nan, np.float32)
                # --- base weighted QA ---
                PPI_QA = (0.9 * NQ + 0.1 * RQ) * 10
                PPI_QA = PPI_QA.astype(np.float32)

                valid = (
                    (RED != REF_NODATA) &
                    (NIR != REF_NODATA) &
                    np.isin(RQ, VALID_QA) &
                    np.isin(NQ, VALID_QA)
                )

                RED = RED.astype(np.float32) * REF_SCALE
                NIR = NIR.astype(np.float32) * REF_SCALE
                DVI = NIR - RED

                sza_rad = np.deg2rad(SZA.astype(np.float32))
                cos_sza = np.cos(sza_rad)

                dc = 0.0336 + 0.0477 / cos_sza
                dc = np.clip(dc, 0, 1)

                num = MDVI_blk - DVI
                den = MDVI_blk - DVI_soil

                ok = valid & (num > 0) & (den > 0) & (cos_sza > 0.05)

                if np.any(ok):
                    mdvi_ok = MDVI_blk[ok]
                    num_ok  = num[ok]
                    den_ok  = den[ok]
                    cos_ok  = cos_sza[ok]
                    dc_ok   = dc[ok]

                    PPI_val = (
                        -0.25 *
                        (1 + mdvi_ok) / (1 - mdvi_ok) *
                        np.log(num_ok / den_ok) /
                        ((0.5 / cos_ok) * (1 - dc_ok) + dc_ok)
                    )
                    PPI[ok] = PPI_val

                # ---------------- QA logic ----------------
                # invalid input
                PPI_QA[~valid] = QA_NODATA

                # computation failed
                PPI_QA[valid & ~ok] = 254

                # MDVI soil / invalid physics
                PPI_QA[(MDVIo_blk < DVI_soil) & valid] = 254

                # sand
                sand = (RED > 0.35) & (DVI > 0.05)
                PPI[sand] = 0
                PPI_QA[sand] = 252

                # ---------------- Finalize ----------------
                PPI = np.real(PPI)
                PPI = np.clip(PPI, -1, 5)
                PPI = np.round(PPI * PPI_SCALE)
                PPI[np.isnan(PPI)] = OUT_NODATA
                # final consistency
                PPI_QA[np.isnan(PPI)] = QA_NODATA
                # convert at very end
                PPI_QA = np.clip(PPI_QA, 0, 255).astype(np.uint8)

                band_ppi.WriteArray(PPI.astype(np.int16), xoff=x0, yoff=y0)
                band_qa.WriteArray(PPI_QA.astype(np.uint8), xoff=x0, yoff=y0)

        ds_ppi.FlushCache()
        ds_qa.FlushCache()
        ds_ppi = ds_qa = None

        print(f"[PPI] written {out_ppi_f}")
        print(f"[PPI] written {out_qa_f}")

    print("[PPI] DONE")


# ------------------------------------------------------------
if __name__ == "__main__":
    t0 = time.time()
    print("[PPI] START:", datetime.now())
    main()
    print("[PPI] END  :", datetime.now())
    print(f"[PPI] runtime {(time.time() - t0)/60:.2f} min")
