#!/usr/bin/env python3
# ============================================================
# MODIS MCD43A4 → PROBA-V tile (NBAR)
# Bands: Nadir_Reflectance_Band1 (RED)
#        Nadir_Reflectance_Band2 (NIR)
#
# Features:
#  - network-robust download (retry + skip)
#  - --resume flag
#  - per-year checkpointing
#  - clean, compact logging
#
# Example:
#   python Download_MODIS_MCD43A4_to_SEN3.py --tile X19Y02 --resume
# ============================================================

import os, math, re, argparse, subprocess, requests, time
from datetime import datetime
from netrc import netrc
from urllib.parse import urlparse
from requests.exceptions import RequestException

# ============================================================
# Constants
# ============================================================
DATASET = "MCD43A4"

PIXEL_SIZE = 0.002976190476189799483
NPIX = 3360
NODATA = 32767

PI = math.pi
Sphere = 6371007.181
CellSize = 926.62543305
TileSize = 1200 * CellSize
ULx = -20015109.354
ULy =  10007554.677
RAD = PI / 180.0

NOMINAL_DAYS_DEFAULT = [1, 6, 11, 16, 21, 26]

# ============================================================
# Logging helpers
# ============================================================
def hr(): print("=" * 70)
def log(msg): print(msg, flush=True)
def sub(msg): print(f"    {msg}", flush=True)
def ok(): print("✓", flush=True)

# ============================================================
# CLI
# ============================================================
def parse_args():
    p = argparse.ArgumentParser("MODIS MCD43A4 → PROBA-V tile")
    p.add_argument("--tile", required=True)
    p.add_argument("--start-year", type=int, default=2000)
    p.add_argument("--end-year", type=int, default=datetime.now().year)
    p.add_argument("--nominal-days", type=int, nargs="+",
                   default=NOMINAL_DAYS_DEFAULT)
    p.add_argument("--base-dir", required=True,
                   help="Workflow data root (contains downloads and prepared tiles)")
    p.add_argument("--resume", action="store_true",
                   help="Resume from existing checkpoints")
    return p.parse_args()

# ============================================================
# Geometry helpers
# ============================================================
def date_to_doy(d):
    return (d - datetime(d.year,1,1)).days + 1

def probav_tile_geometry(tile):
    X, Y = int(tile[1:3]), int(tile[4:6])
    lon_min = -180 + 10 * X
    lat_max = 75 - 10 * Y
    return lon_min, lat_max, lon_min - PIXEL_SIZE/2, lat_max + PIXEL_SIZE/2

def lonlat2modis_tile(lon, lat):
    x = lon * RAD * Sphere * math.cos(lat * RAD)
    y = lat * RAD * Sphere
    h = int((x - ULx) / TileSize)
    v = int(-(y - ULy) / TileSize)
    return h, v

def intersecting_modis_tiles(tile):
    X, Y = int(tile[1:3]), int(tile[4:6])
    corners = [
        (-180+10*X, 75-10*(Y+1)),
        (-180+10*X, 75-10*Y),
        (-170+10*X, 75-10*(Y+1)),
        (-170+10*X, 75-10*Y),
    ]
    hs, vs = zip(*(lonlat2modis_tile(lon, lat) for lon, lat in corners))
    return [f"h{h:02d}v{v:02d}"
            for h in range(min(hs), max(hs)+1)
            for v in range(min(vs), max(vs)+1)]

# ============================================================
# MODIS helpers
# ============================================================
def get_credentials(url):
    auth = netrc(os.path.expanduser("~/.netrc")).authenticators(
        urlparse(url).hostname
    )
    return auth[0], auth[2]

def download_hdf(tile, year, doy, raw_dir, retries=3, sleep=5):
    outdir = f"{raw_dir}/{year}/{doy:03d}"
    os.makedirs(outdir, exist_ok=True)

    base_url = (
        f"https://ladsweb.modaps.eosdis.nasa.gov/archive/"
        f"allData/61/{DATASET}/{year}/{doy:03d}/"
    )

    user, pwd = get_credentials(base_url)

    for attempt in range(1, retries + 1):
        try:
            s = requests.Session()
            s.auth = (user, pwd)
            s.timeout = 30

            r = s.get(base_url)
            r.raise_for_status()

            for link in re.findall(r'href="([^"]+\.hdf)"', r.text):
                fname = os.path.basename(link)
                if tile not in fname:
                    continue

                out = os.path.join(outdir, fname)
                if os.path.exists(out):
                    return out

                url = link if link.startswith("http") else base_url + fname

                with s.get(url, stream=True) as rr:
                    rr.raise_for_status()
                    with open(out, "wb") as f:
                        for c in rr.iter_content(8192):
                            f.write(c)

                return out

            return None

        except RequestException:
            if attempt < retries:
                sub(f"⚠ network error, retry {attempt}/{retries}")
                time.sleep(sleep)
            else:
                sub(f"⊘ network unreachable, skip DOY {doy:03d}")
                return None

def warp(hdf, band, out, geom):
    lon_min, lat_max, ox, oy = geom
    subprocess.run([
        "gdalwarp", "-overwrite", "-of", "GTiff",
        "-t_srs", "EPSG:4326", "-r", "near",
        "-srcnodata", str(NODATA), "-dstnodata", str(NODATA),
        "-te", str(ox), str(lat_max-10),
               str(ox+NPIX*PIXEL_SIZE), str(lat_max),
        "-tr", str(PIXEL_SIZE), str(PIXEL_SIZE),
        f'HDF4_EOS:EOS_GRID:"{hdf}":MOD_Grid_BRDF:{band}',
        out
    ], check=True)

# ============================================================
# Main
# ============================================================
def main():
    a = parse_args()

    if a.start_year > a.end_year:
        raise ValueError("--start-year must not be later than --end-year")
    if not all(1 <= day <= 31 for day in a.nominal_days):
        raise ValueError("--nominal-days values must be between 1 and 31")

    raw_dir = f"{a.base_dir}/{DATASET}"
    out_base = f"{a.base_dir}/MCD43A4_Sen3_tile/{a.tile}"
    geom = probav_tile_geometry(a.tile)
    modis_tiles = intersecting_modis_tiles(a.tile)

    # Header
    hr()
    log("MODIS MCD43A4 → PROBA-V NBAR")
    hr()
    log(f"Tile  : {a.tile}")
    log(f"Years : {a.start_year}–{a.end_year}")
    log("Bands : RED, NIR")
    hr()
    log(f"MODIS tiles: {modis_tiles}")
    log("")

    # Build date list
    dates = []
    for y in range(a.start_year, a.end_year+1):
        for m in range(1,13):
            for d in a.nominal_days:
                try:
                    dates.append(datetime(y,m,d))
                except ValueError:
                    pass

    total = len(dates)
    log(f"[A4] Tile {a.tile}, {total} dates")

    # Checkpoint dirs
    chk_dir = f"{out_base}/.checkpoints"
    os.makedirs(chk_dir, exist_ok=True)

    missing_count = 0

    for i, date in enumerate(dates, 1):
        doy = date_to_doy(date)
        chk_done = f"{chk_dir}/{date.year}_{doy:03d}.done"
        chk_miss = f"{chk_dir}/{date.year}_{doy:03d}.missing"

        if a.resume and (os.path.exists(chk_done) or os.path.exists(chk_miss)):
            continue

        log(f"[{i}/{total}] {date.year} DOY {doy:03d}")

        red_out = f"{out_base}/RED_toc/MCD43A4_NBAR_CGLS_PBV_{a.tile}_RED_toc_{date.year}_{doy:03d}.tif"
        nir_out = f"{out_base}/NIR_toc/MCD43A4_NBAR_CGLS_PBV_{a.tile}_NIR_toc_{date.year}_{doy:03d}.tif"

        if os.path.exists(red_out) and os.path.exists(nir_out):
            open(chk_done, "w").close()
            continue

        os.makedirs(os.path.dirname(red_out), exist_ok=True)
        os.makedirs(os.path.dirname(nir_out), exist_ok=True)

        tmp = f"{out_base}/temp/{date.year}_{doy:03d}"
        os.makedirs(tmp, exist_ok=True)

        reds, nirs = [], []

        for mt in modis_tiles:
            hdf = download_hdf(mt, date.year, doy, raw_dir)
            if not hdf:
                continue

            sub(os.path.basename(hdf))

            r = f"{tmp}/{mt}_RED.tif"
            n = f"{tmp}/{mt}_NIR.tif"

            sub("→ warp RED")
            warp(hdf, "Nadir_Reflectance_Band1", r, geom)

            sub("→ warp NIR")
            warp(hdf, "Nadir_Reflectance_Band2", n, geom)

            reds.append(r)
            nirs.append(n)

        if not reds:
            open(chk_miss, "w").close()
            missing_count += 1
            continue

        sub("→ merge")
        subprocess.run(
            ["gdal_merge.py", "-o", red_out,
             "-n", str(NODATA), "-a_nodata", str(NODATA)] + reds,
            check=True
        )
        subprocess.run(
            ["gdal_merge.py", "-o", nir_out,
             "-n", str(NODATA), "-a_nodata", str(NODATA)] + nirs,
            check=True
        )

        open(chk_done, "w").close()
        ok()

    hr()
    log("Run finished")
    log(f"Missing dates (network / no data): {missing_count}")
    hr()

# ============================================================
if __name__ == "__main__":
    main()
