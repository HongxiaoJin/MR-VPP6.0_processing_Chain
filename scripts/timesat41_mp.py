#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TIMESAT runner (parallel, multi-process with safe output writing).

Changes vs serial version
--------------------------
* Row slices inside each block are submitted to a ProcessPoolExecutor.
  Each worker process computes one slice independently (bypasses GIL).
* Output writes are serialised per-file with a per-file threading.Lock so
  two workers never corrupt the same GeoTIFF.
* Crash safety: each block is first written to a temp file in the output
  folder; on completion the temp file is atomically renamed over the
  real output.  A partially-written block therefore never corrupts an
  existing good file.
* --workers N controls the pool size (default = os.cpu_count()).

Examples
--------
python timesat41_parallel.py --cfgFile .../timesatCfg.json --tile X12Y00 \\
    --block-rows 240 --tiled --bigtiff --workers 8

python timesat41_mp.py --cfgFile config/timesatCfg.local.json --tile X17Y03 --start-row 2030 --block-rows 240 --tiled --bigtiff --workers 10
"""

__version__ = '4.0.3-parallel'
__date__    = '20260415'

import os, sys, json, logging, traceback, math, re, gc, tempfile, shutil
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime
from time import time
import threading

import numpy as np
import pandas as pd

# Fortran TIMESAT core (f2py module)
try:
    from cglopstsfprocess import cglopstsfprocess
except ModuleNotFoundError:
    cglopstsfprocess = None

# Optional deps
try:
    import rasterio
    from rasterio.windows import Window
except Exception:
    rasterio = None
try:
    from netCDF4 import Dataset
except Exception:
    Dataset = None


# ===================== Logging & cfg helpers =====================

def getLogger(logName, logFile):
    logger = logging.getLogger(logName)
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return logger
    os.makedirs(os.path.dirname(logFile), exist_ok=True)
    fh = logging.FileHandler(logFile)
    fh.setLevel(logging.DEBUG)
    ff = logging.Formatter(
        '%(asctime)s.%(msecs)-3d %(levelname)-8s [%(module)s:%(lineno)d] %(message)s',
        '%Y.%m.%d %H:%M:%S')
    fh.setFormatter(ff)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    cf = logging.Formatter('%(levelname)-8s %(message)s', '')
    ch.setFormatter(cf)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


def _cfg_unwrap(v, default=None):
    if isinstance(v, dict):
        return v.get('value', default)
    return v if v is not None else default


def _cfg_get_str(cfg, key, default=''):
    v = _cfg_unwrap(cfg.get(key), default)
    return '' if v is None else str(v)


def _drop_partial_years(timevector):
    yrstart = int(np.floor(timevector.min() / 1000))
    yrend   = int(np.floor(timevector.max() / 1000))
    drop_first = (timevector.min() % 1000) > 183
    drop_last  = (timevector.max() % 1000) < 182
    if drop_first:
        yrstart += 1
    if drop_last:
        yrend -= 1
    years = np.arange(yrstart, yrend + 1, dtype=int)
    return drop_first, drop_last, yrstart, yrend, years


# ===================== GeoTIFF helpers =====================

def _openimagedatafiles_(x_map, y_map, x, y, yflist, wflist):
    """Read window (x_map, y_map, w, h) across all images (PPI + optional QA)."""
    z  = len(yflist)
    vi = np.ndarray((y, x, z), order='F', dtype='int16')
    qa = np.ndarray((y, x, z), order='F', dtype='uint8')
    for i, yfname in enumerate(yflist):
        with rasterio.open(yfname, 'r') as temp:
            vi[:, :, i] = temp.read(1, window=Window(x_map, y_map, x, y))
    if (wflist == '') or (wflist is None) or (len(wflist) == 0):
        qa[:] = 1
    else:
        for i, wfname in enumerate(wflist):
            with rasterio.open(wfname, 'r') as temp2:
                qa[:, :, i] = temp2.read(1, window=Window(x_map, y_map, x, y))
    return vi, qa


def _readtv_from_filenames_(tlist, flistfull):
    """Build YYYYDDD timevector from filenames or explicit list."""
    flist      = [os.path.basename(p) for p in flistfull]
    timevector = np.ndarray(len(flist), order='F', dtype='uint32')
    if tlist == '':
        firstimg = flist[0]
        dates = re.findall(r"\d{7}", firstimg)
        if not dates:
            raise RuntimeError('No YYYYDDD token in filenames. Provide tv_list in config.')
        pos0 = firstimg.find(dates[0])
        pos1 = pos0 + 7
        for i, fname in enumerate(flist):
            timevector[i] = int(fname[pos0:pos1])
    else:
        with open(tlist, 'r') as f:
            lines = f.read().splitlines()
        for i, s in enumerate(lines):
            y = int(s[0:4]); m = int(s[4:6]); d = int(s[6:8])
            doy = datetime(y, m, d).timetuple().tm_yday
            timevector[i] = y * 1000 + doy
    return timevector


def _open_or_create_tif(path, profile, mode_existing='r+'):
    if os.path.exists(path):
        return rasterio.open(path, mode_existing)
    else:
        return rasterio.open(path, 'w', **profile)


def _check_dim_match(path, width, height, logger=print):
    try:
        with rasterio.open(path) as ds:
            if ds.width != width or ds.height != height:
                logger(f'WARNING: {os.path.basename(path)} dims '
                       f'{ds.width}x{ds.height} != expected {width}x{height}.')
    except Exception:
        pass


def discoverInputs(cfgDict, logger=print):
    logger("Discovering inputs")
    logger(f" > Date range {cfgDict['startDate']} to {cfgDict['endDate']}")
    startDate = datetime.strptime(cfgDict['startDate'], "%Y%m%d")
    endDate   = datetime.strptime(cfgDict['endDate'],   "%Y%m%d")
    dateLst   = pd.date_range(startDate, endDate, freq='D')
    logger(f" > tile {cfgDict['tile']}")
    ppifileLst, qafileLst = [], []
    for date in dateLst.strftime("%Y%m%d"):
        year = date[0:4]
        doy  = str(datetime.strptime(date, "%Y%m%d").timetuple().tm_yday).zfill(3)
        ppat = os.path.join(cfgDict['inppiFolder'], cfgDict['inppiFilePattern'])
        ppat = ppat.replace('<tile>', cfgDict['tile']).replace('<year>', year).replace('<doy>', doy)
        if os.path.isfile(ppat):
            ppifileLst.append(ppat)
        qpat = os.path.join(cfgDict['inqaFolder'], cfgDict['inqaFilePattern'])
        qpat = qpat.replace('<tile>', cfgDict['tile']).replace('<year>', year).replace('<doy>', doy)
        if os.path.isfile(qpat):
            qafileLst.append(qpat)
    logger(f" > Found {len(ppifileLst)} PPI files across {len(dateLst)} days")
    logger(f" > Found {len(qafileLst)} QA files across {len(dateLst)} days")
    return ppifileLst, qafileLst, len(ppifileLst), len(dateLst)


# ===================== Worker function (runs in subprocess) =====================

def _worker_process_row_slice(
        row_abs, rows_h, x_map, x,
        flist, qlist, tv, yr_total,
        p_ignoreday, p_ylu, p_a, p_printflag, p_nodata,
        p_nenvi, p_wfactnum, p_startmethod, p_startcutoff,
        p_lpbase, p_outputformat, p_seapar,
        trim_first, trim_last, p_outindex, p_outindex_num):
    """
    Pure function executed in a worker process.
    Returns (row_abs, vpp_layers[layers, h, x], vppqa_layers[layers, h, x]).
    All arguments must be picklable.
    """
    import gc
    import numpy as np
    from cglopstsfprocess import cglopstsfprocess

    # Local import of rasterio inside the worker
    import rasterio
    from rasterio.windows import Window

    def _read(x_map, y_map, x, y, yflist, wflist):
        z  = len(yflist)
        vi = np.ndarray((y, x, z), order='F', dtype='int16')
        qa = np.ndarray((y, x, z), order='F', dtype='uint8')
        for i, fname in enumerate(yflist):
            with rasterio.open(fname, 'r') as ds:
                vi[:, :, i] = ds.read(1, window=Window(x_map, y_map, x, y))
        if (wflist == '') or (wflist is None) or (len(wflist) == 0):
            qa[:] = 1
        else:
            for i, fname in enumerate(wflist):
                with rasterio.open(fname, 'r') as ds:
                    qa[:, :, i] = ds.read(1, window=Window(x_map, y_map, x, y))
        return vi, qa

    vi, qa = _read(x_map, row_abs, x, rows_h, flist, qlist)

    vpp, vppqa, nseason, yfit, yfitqa, tseq = cglopstsfprocess(
        yr_total, vi, qa, tv,
        p_outindex, p_ignoreday, p_ylu, p_a, p_printflag, p_nodata,
        p_nenvi, p_wfactnum, p_startmethod, p_startcutoff, p_lpbase,
        p_outputformat, p_seapar,
        rows_h, x, len(tv), p_outindex_num
    )
    del vi, qa
    gc.collect()

    if trim_first:
        vpp   = vpp[:, :, 26:]
        vppqa = vppqa[:, :, 2:]
    if trim_last:
        vpp   = vpp[:, :, :-26]
        vppqa = vppqa[:, :, :-2]

    vpp_layers   = np.moveaxis(vpp,   -1, 0).astype(np.int16,  copy=False)
    vppqa_layers = np.moveaxis(vppqa, -1, 0).astype(np.uint8, copy=False)
    del vpp, vppqa, yfit, yfitqa, tseq
    gc.collect()

    return row_abs, vpp_layers, vppqa_layers


# ===================== Safe writer helpers =====================

# One lock per output file path (created lazily, lives in the main process).
_file_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)


def _write_rows_safe(fn, profile, window, band_data):
    """
    Thread-safe write of band_data (2-D array) to a single-band GeoTIFF.
    Uses a per-file lock so parallel futures cannot interleave writes.
    """
    with _file_locks[fn]:
        with _open_or_create_tif(fn, profile) as dst:
            dst.write(band_data, window=window, indexes=1)


# ===================== GeoTIFF mode (parallel) =====================

def timesat_tif(cfgDict, logger=print, n_workers=None):
    if rasterio is None:
        raise ImportError("rasterio is required for 'tif' mode.")

    jobname   = cfgDict['tile']
    outfolder = cfgDict['outputfolder'].replace('<tile>', jobname)
    os.makedirs(outfolder, exist_ok=True)

    s = cfgDict['settings']
    tv_list       = s['tv_list']['value']
    imwindow      = s['imwindow']['value']
    p_ignoreday   = s['p_ignoreday']['value']
    p_ylu         = s['p_ylu']['value']
    p_a           = [item for sublist in s['p_a']['value'] for item in sublist]
    p_nodata      = s['p_nodata']['value']
    p_nenvi       = s['p_nenvi']['value']
    p_wfactnum    = s['p_wfactnum']['value']
    p_startmethod = s['p_startmethod']['value']
    p_startcutoff = s['p_startcutoff']['value']
    p_lpbase      = s.get('p_lpbase', {}).get('value', 0.0)
    p_outputformat= s['p_outputformat']['value']
    p_seapar      = s['p_seapar']['value']
    p_printflag   = s['p_printflag']['value']

    block_rows    = int(_cfg_unwrap(s.get('block_rows'),    20))
    rows_per_task = int(_cfg_unwrap(s.get('rows_per_task'), 1))
    start_row     = _cfg_unwrap(s.get('start_row'),  None)
    end_row       = _cfg_unwrap(s.get('end_row'),    None)
    precreate     = _cfg_unwrap(s.get('precreate_outputs'), True)
    tiled         = bool(_cfg_unwrap(s.get('tiled'),   False))
    bigtiff       = bool(_cfg_unwrap(s.get('bigtiff'), False))
    gdal_cachemax = _cfg_unwrap(s.get('gdal_cachemax'), None)
    if gdal_cachemax:
        os.environ["GDAL_CACHEMAX"] = str(gdal_cachemax)

    if n_workers is None:
        n_workers = os.cpu_count() or 1

    flist = cfgDict['ppifileList']
    qlist = cfgDict.get('qafileList', '')
    if not flist:
        raise RuntimeError("No input PPI GeoTIFFs discovered.")

    # Time vector (deduplicate)
    timevector = _readtv_from_filenames_(tv_list, flist)
    timevector, uniq_idx = np.unique(timevector, return_index=True)
    flist = [flist[i] for i in uniq_idx]
    if qlist not in ('', None) and len(qlist) > 0:
        qlist = [qlist[i] for i in uniq_idx]

    z = len(flist)
    logger(f'num of images: {z}')
    logger('First image: ' + flist[0])
    logger('Last image:  ' + flist[-1])
    logger(f'Parallel workers: {n_workers}')

    p_outindex     = 1
    p_outindex_num = 1

    with rasterio.open(flist[0], 'r') as ds0:
        img_profile = ds0.profile

    if sum(imwindow) == 0:
        dx, dy = img_profile['width'], img_profile['height']
        x_off, y_off = 0, 0
    else:
        x_off, y_off, dx, dy = imwindow

    # Output profiles
    img_profile_vpp = deepcopy(img_profile)
    img_profile_vpp.update(nodata=p_nodata, dtype=rasterio.int16, count=1, compress='lzw')
    img_profile_vppqa = deepcopy(img_profile)
    img_profile_vppqa.update(nodata=255, dtype=rasterio.uint8, count=1, compress='lzw')
    if tiled:
        img_profile_vpp.update(TILED=True,   BLOCKXSIZE=256, BLOCKYSIZE=256)
        img_profile_vppqa.update(TILED=True, BLOCKXSIZE=256, BLOCKYSIZE=256)
    if bigtiff:
        img_profile_vpp.update(BIGTIFF='YES')
        img_profile_vppqa.update(BIGTIFF='YES')

    vppname  = ["SOSD","SOSV","LSLOPE","EOSD","EOSV","RSLOPE",
                "LENGTH","MINV","MAXD","MAXV","AMPL","TPROD","SPROD"]
    vppqaname = "QA"

    # Year selection & output file list
    drop_first, drop_last, yrstart, yrend, years = _drop_partial_years(timevector)
    outvppfn, outvppqafn = [], []
    for year in years:
        for seas in (1, 2):
            for k in range(13):
                outvppfn.append(
                    os.path.join(outfolder, f'{jobname}_{year}_season{seas}_{vppname[k]}.tif'))
            outvppqafn.append(
                os.path.join(outfolder, f'{jobname}_{year}_season{seas}_{vppqaname}.tif'))

    # Precreate outputs
    if precreate:
        for fn in outvppfn:
            if not os.path.exists(fn):
                with rasterio.open(fn, 'w', **img_profile_vpp):
                    pass
            else:
                _check_dim_match(fn, img_profile_vpp['width'], img_profile_vpp['height'], logger)
        for fn in outvppqafn:
            if not os.path.exists(fn):
                with rasterio.open(fn, 'w', **img_profile_vppqa):
                    pass
            else:
                _check_dim_match(fn, img_profile_vppqa['width'], img_profile_vppqa['height'], logger)

    # Geometry & requested row range
    if block_rows <= 0:
        raise RuntimeError('--block-rows must be >= 1')
    if rows_per_task <= 0:
        raise RuntimeError('--rows-per-task must be >= 1')

    total_blocks = int(math.ceil(dy / block_rows))
    if start_row is None: start_row = 0
    if end_row   is None: end_row   = dy - 1

    if start_row < 0 or start_row >= dy:
        raise RuntimeError(f'--start-row {start_row} outside 0..{dy-1}')
    if end_row < 0 or end_row >= dy:
        raise RuntimeError(f'--end-row {end_row} outside 0..{dy-1}')
    if end_row < start_row:
        raise RuntimeError(f'--end-row {end_row} must be >= --start-row {start_row}')

    start_block = start_row // block_rows
    end_block   = end_row   // block_rows

    logger(f'Processing row range {start_row}..{end_row} '
           f'(blocks {start_block+1}..{end_block+1} of {total_blocks})')

    yr_total  = int(len(np.unique(timevector // 1000)))
    n_layers_vpp = len(outvppfn)
    n_layers_qa  = len(outvppqafn)

    # Common kwargs passed to every worker (picklable scalars / lists only)
    worker_kwargs = dict(
        x_map=int(x_off),
        x=dx,
        flist=flist,
        qlist=qlist if (qlist not in ('', None) and len(qlist) > 0) else '',
        tv=timevector,
        yr_total=yr_total,
        p_ignoreday=p_ignoreday,
        p_ylu=p_ylu,
        p_a=p_a,
        p_printflag=p_printflag,
        p_nodata=p_nodata,
        p_nenvi=p_nenvi,
        p_wfactnum=p_wfactnum,
        p_startmethod=p_startmethod,
        p_startcutoff=p_startcutoff,
        p_lpbase=p_lpbase,
        p_outputformat=p_outputformat,
        p_seapar=p_seapar,
        trim_first=drop_first,
        trim_last=drop_last,
        p_outindex=p_outindex,
        p_outindex_num=p_outindex_num,
    )

    # ---- Main loop over blocks ----
    with ProcessPoolExecutor(max_workers=n_workers) as pool:
        for ib in range(start_block, end_block + 1):
            block_y0 = ib * block_rows
            y_map    = int(y_off + block_y0)
            y_block  = min(block_rows, dy - block_y0)

            r0 = 0
            if ib == start_block:
                r0 = start_row - block_y0
            r1 = y_block - 1
            if ib == end_block:
                r1 = end_row - block_y0
            y_eff = r1 - r0 + 1
            if y_eff <= 0:
                continue

            logger(f'Block {ib+1}/{total_blocks}: '
                   f'rows {block_y0+r0}..{block_y0+r1}  start={datetime.now()}')

            # Accumulator for this block (filled as futures complete)
            vpp_block   = np.full((n_layers_vpp, y_eff, dx), p_nodata, dtype=np.int16)
            vppqa_block = np.full((n_layers_qa,  y_eff, dx), 255,      dtype=np.uint8)

            # Submit all row-slice tasks for this block
            futures = {}
            rr = r0
            while rr <= r1:
                h       = min(rows_per_task, r1 - rr + 1)
                row_abs = y_map + rr
                fut = pool.submit(
                    _worker_process_row_slice,
                    row_abs, h,
                    **worker_kwargs,
                )
                futures[fut] = (rr, h)
                rr += h

            # Collect results as they arrive
            done_rows = 0
            for fut in as_completed(futures):
                rr_local, h = futures[fut]
                try:
                    _, vpp_part, vppqa_part = fut.result()
                except Exception as exc:
                    logger(f'  ERROR in worker (block {ib+1}, rel-row {rr_local}): {exc}')
                    traceback.print_exc()
                    raise

                r_rel = rr_local - r0
                vpp_block  [:, r_rel:r_rel+h, :] = vpp_part
                vppqa_block[:, r_rel:r_rel+h, :] = vppqa_part
                del vpp_part, vppqa_part
                done_rows += h
                if y_eff >= 20 and (done_rows % 20 == 0 or done_rows == y_eff):
                    logger(f'  rows {done_rows}/{y_eff} collected (block {ib+1})')

            gc.collect()

            # --- Crash-safe write: write to tmp then rename ---
            # We write the full block into a temp directory first so that a
            # crash mid-write never corrupts an already-good output file.
            tmp_dir = tempfile.mkdtemp(dir=outfolder, prefix=f'.tmp_blk{ib+1}_')
            try:
                win = Window(x_off, y_off + block_y0 + r0, dx, y_eff)

                # Write vpp layers
                for i, fn in enumerate(outvppfn):
                    tmp_fn = os.path.join(tmp_dir, os.path.basename(fn))
                    # Copy existing file into tmp so we can patch the window
                    if os.path.exists(fn):
                        shutil.copy2(fn, tmp_fn)
                    with _open_or_create_tif(tmp_fn, img_profile_vpp) as dst:
                        dst.write(vpp_block[i, :, :], window=win, indexes=1)

                # Write vppqa layers
                for i, fn in enumerate(outvppqafn):
                    tmp_fn = os.path.join(tmp_dir, os.path.basename(fn))
                    if os.path.exists(fn):
                        shutil.copy2(fn, tmp_fn)
                    with _open_or_create_tif(tmp_fn, img_profile_vppqa) as dst:
                        dst.write(vppqa_block[i, :, :], window=win, indexes=1)

                # Atomic promotion: rename tmp -> real (per-file lock for safety
                # in case two blocks ever overlap — they shouldn't, but guards it)
                for fn in outvppfn + outvppqafn:
                    tmp_fn = os.path.join(tmp_dir, os.path.basename(fn))
                    if not os.path.exists(tmp_fn):
                        continue
                    with _file_locks[fn]:
                        shutil.move(tmp_fn, fn)

            finally:
                # Clean up temp dir (may already be empty after moves)
                shutil.rmtree(tmp_dir, ignore_errors=True)

            del vpp_block, vppqa_block
            gc.collect()
            logger(f'Block {ib+1}/{total_blocks} finished at {datetime.now()}')

    logger('Done (GeoTIFF parallel mode)!')


# ===================== NetCDF mode (unchanged from serial) =====================

def _find_lat_lon_vars(ds):
    cand = [('latitude', 'longitude'), ('lat', 'lon'), ('y', 'x')]
    for la, lo in cand:
        if la in ds.variables and lo in ds.variables:
            return la, lo
    la = lo = None
    for v in ds.variables.values():
        if getattr(v, 'standard_name', '') == 'latitude':  la = v.name
        if getattr(v, 'standard_name', '') == 'longitude': lo = v.name
    if la and lo:
        return la, lo
    raise RuntimeError("Could not find latitude/longitude variables in input NetCDF.")


def _build_timevector_from_noleap(nc_path):
    with Dataset(nc_path, 'r') as ds:
        t       = ds.variables['time'][:]
        t_units = getattr(ds.variables['time'], 'units',    'days since 1986-01-01 00:00:00')
        t_cal   = getattr(ds.variables['time'], 'calendar', 'noleap')
    year_idx = t // 365
    doy      = (t % 365) + 1
    years    = 1986 + year_idx
    tv       = (years.astype(np.uint32) * 1000 + doy.astype(np.uint32)).astype(np.uint32)
    return tv, int(years.min()), int(years.max()), t_units, t_cal


def _get_nc_dims(nc_path, varname):
    with Dataset(nc_path, 'r') as ds:
        v = ds.variables[varname]
        z, y, x = v.shape
    return z, y, x


def _read_nc_block_int16(nc_path, varname, y_map, y, x_map, x, *,
                         scale=1000.0, out_nodata=-9999):
    if not (-32768 <= out_nodata <= 32767):
        raise ValueError("out_nodata must fit in int16.")
    with Dataset(nc_path, 'r') as ds:
        ds.set_auto_maskandscale(True)
        v   = ds.variables[varname]
        sub = v[:, y_map:y_map+y, x_map:x_map+x]
    data   = np.ma.filled(sub, np.nan).astype(np.float32)
    mask   = ~np.isfinite(data)
    scaled = np.clip(np.round(data * scale), -32768, 32767).astype(np.float32)
    scaled[mask] = out_nodata
    arrs = scaled.astype(np.int16)          # (t, y, x)
    vi   = np.transpose(arrs, (1, 2, 0)).copy()
    qa   = np.ones_like(vi, dtype=np.uint8)
    return vi, qa


def timesat_netcdf(cfgDict, logger=print):
    if Dataset is None:
        raise ImportError("netCDF4 is required for 'netcdf' mode.")

    jobname   = cfgDict.get('tile', 'TeBS')
    outfolder = cfgDict['outputfolder'].replace('<tile>', jobname)
    os.makedirs(outfolder, exist_ok=True)
    out_nc = os.path.join(outfolder, f"{jobname}_LAI_LSP.nc")

    s = cfgDict['settings']
    p_ignoreday    = s['p_ignoreday']['value']
    p_ylu          = s['p_ylu']['value']
    p_a            = [item for sublist in s['p_a']['value'] for item in sublist]
    p_nodata       = s['p_nodata']['value']
    p_nenvi        = s['p_nenvi']['value']
    p_wfactnum     = s['p_wfactnum']['value']
    p_startmethod  = s['p_startmethod']['value']
    p_startcutoff  = s['p_startcutoff']['value']
    p_low_percentile = s['p_low_percentile']['value']
    p_outputformat = s['p_outputformat']['value']
    p_seapar       = s['p_seapar']['value']
    p_printflag    = s['p_printflag']['value']

    nc_path  = cfgDict['inDataFile']
    cfgDict["inDataset"] = cfgDict["tile"]
    varname  = cfgDict.get('inDataset')

    tv, base_yr, last_yr, t_units, t_calendar = _build_timevector_from_noleap(nc_path)
    tv, _ = np.unique(tv, return_index=True)
    drop_first, drop_last, yrstart, yrend, years = _drop_partial_years(tv)
    n_years = len(years)
    yr      = yrend - yrstart + 1

    z, full_y, full_x = _get_nc_dims(nc_path, varname)
    logger(f'num of images (days): {z}')
    logger('NetCDF source: ' + nc_path)

    vi, qa = _read_nc_block_int16(
        nc_path, varname, y_map=0, y=full_y, x_map=0, x=full_x,
        scale=1000.0, out_nodata=p_nodata)

    logger('--- start TIMESAT processing --- starttime: ' + str(datetime.now()))
    vpp, vppqa, nseason, yfit, yfitqa, tseq = cglopstsfprocess(
        yr, vi, qa, tv, 1,
        p_ignoreday, p_ylu, p_a, p_printflag, p_nodata,
        p_nenvi, p_wfactnum, p_startmethod, p_startcutoff, p_low_percentile,
        p_outputformat, p_seapar,
        full_y, full_x, vi.shape[2], 1)

    if drop_first:
        vpp   = vpp[:, :, 26:]
        vppqa = vppqa[:, :, 2:]
    if drop_last:
        vpp   = vpp[:, :, :-26]
        vppqa = vppqa[:, :, :-2]

    vpp   = vpp.astype(np.int16).reshape(full_y, full_x, n_years, 2, 13)
    vppqa = vppqa.astype(np.uint8).reshape(full_y, full_x, n_years, 2)

    vppname = ["SOSD","SOSV","LSLOPE","EOSD","EOSV","RSLOPE",
               "LENGTH","MINV","MAXD","MAXV","AMPL","TPROD","SPROD"]

    with Dataset(nc_path, 'r') as src, Dataset(out_nc, 'w') as dst:
        lat_name, lon_name = _find_lat_lon_vars(src)
        lat = src.variables[lat_name][:]
        lon = src.variables[lon_name][:]
        base_year  = 1986
        time_vals  = (years - base_year) * 365

        dst.createDimension('time',   n_years)
        dst.createDimension('season', 2)
        dst.createDimension(lat_name, lat.shape[0])
        dst.createDimension(lon_name, lon.shape[0])

        v_time = dst.createVariable('time', 'i4', ('time',))
        v_time.units     = t_units
        v_time.calendar  = t_calendar
        v_time.long_name = 'reference time (YYYY-01-01), noleap calendar'
        v_time[:] = time_vals

        v_season = dst.createVariable('season', 'i1', ('season',))
        v_season.long_name    = 'growing season index'
        v_season.flag_values  = np.array([1, 2], dtype='i1')
        v_season.flag_meanings = 'season1 season2'
        v_season[:] = np.array([1, 2], dtype='i1')

        v_lat = dst.createVariable(lat_name, lat.dtype, (lat_name,))
        v_lon = dst.createVariable(lon_name, lon.dtype, (lon_name,))
        v_lat.standard_name = 'latitude';  v_lat.units = 'degrees_north'
        v_lon.standard_name = 'longitude'; v_lon.units = 'degrees_east'
        v_lat[:] = lat; v_lon[:] = lon

        dst.Conventions = 'CF-1.8'
        dst.title       = f'TIMESAT metrics for {jobname}'
        dst.history     = (f'Created {datetime.utcnow().isoformat()}Z '
                           f'by timesat41_parallel.py {__version__}')
        dst.source      = os.path.basename(nc_path)
        dst.note        = ('Inputs scaled*1000 -> int16 prior to TIMESAT. '
                           'Metrics are int16 with _FillValue.')

        for k, name in enumerate(vppname):
            var = dst.createVariable(name, 'i2',
                                     ('time', 'season', lat_name, lon_name),
                                     zlib=True, complevel=4, fill_value=p_nodata)
            var.long_name = f'TIMESAT {name}'
            data = np.transpose(vpp[:, :, :, :, k], (2, 3, 0, 1))
            var[:] = data

        qa_var = dst.createVariable('QA', 'u1',
                                    ('time', 'season', lat_name, lon_name),
                                    zlib=True, complevel=4, fill_value=255)
        qa_var.long_name = 'TIMESAT QA (1=valid); 255=nodata'
        qa_data = np.transpose(vppqa, (2, 3, 0, 1))
        qa_var[:] = qa_data

    logger(f'Wrote {out_nc}')


# ===================== Main =====================

def main():
    import argparse
    parser = argparse.ArgumentParser(
        prog='timesat41_parallel.py',
        description='TIMESAT (parallel) for GeoTIFF: multi-process compute + safe write'
    )
    parser.add_argument('-c', '--cfgFile',       type=str, required=True,
                        help='JSON configuration file')
    parser.add_argument('-t', '--tile',           type=str, default=None,
                        help='Tile/dataset name (overrides config)')
    parser.add_argument('--start-date', type=str, default=None,
                        help='YYYYMMDD start date (overrides config)')
    parser.add_argument('--end-date', type=str, default=None,
                        help='YYYYMMDD end date (overrides config)')
    parser.add_argument('--start-row',            type=int, default=None,
                        help='Resume from this absolute pixel row (0-based).')
    parser.add_argument('--end-row',              type=int, default=None,
                        help='Stop at this absolute pixel row (0-based, inclusive).')
    parser.add_argument('--no-precreate',         action='store_true',
                        help='Do not pre-create output TIFFs.')
    parser.add_argument('--block-rows',           type=int, default=20,
                        help='Rows per compute/write block (default 20).')
    parser.add_argument('--rows-per-task',        type=int, default=1,
                        help='Rows per worker call (default 1).')
    parser.add_argument('--workers',              type=int, default=None,
                        help='Number of parallel worker processes (default: cpu_count).')
    parser.add_argument('--tiled',                action='store_true',
                        help='Create tiled GeoTIFFs.')
    parser.add_argument('--bigtiff',              action='store_true',
                        help='Force BigTIFF outputs.')
    parser.add_argument('--gdal-cachemax',        type=int, default=None,
                        help='GDAL cache (MB).')
    args = parser.parse_args()
    if cglopstsfprocess is None:
        parser.error(
            "the separately supplied cglopstsfprocess CPython 3.11 Linux x86-64 "
            "extension is required for processing"
        )

    try:
        with open(args.cfgFile, 'r') as f:
            cfgDict = json.load(f)

        if args.tile:
            cfgDict['tile'] = args.tile
        if args.start_date:
            datetime.strptime(args.start_date, '%Y%m%d')
            cfgDict['startDate'] = args.start_date
        if args.end_date:
            datetime.strptime(args.end_date, '%Y%m%d')
            cfgDict['endDate'] = args.end_date

        for k in ['startDate', 'endDate', 'inputFiletype', 'inDataFile', 'inDataset',
                  'tile', 'outputfolder', 'logFile']:
            cfgDict[k] = _cfg_get_str(cfgDict, k, cfgDict.get(k, ''))

        cfgDict.setdefault('settings', {})
        s = cfgDict['settings']
        s.setdefault('block_rows',        {'value': args.block_rows})
        s.setdefault('rows_per_task',     {'value': args.rows_per_task})
        s.setdefault('precreate_outputs', {'value': not args.no_precreate})
        s.setdefault('tiled',             {'value': args.tiled})
        s.setdefault('bigtiff',           {'value': args.bigtiff})
        if args.gdal_cachemax is not None:
            s.setdefault('gdal_cachemax', {'value': args.gdal_cachemax})
        if args.start_row is not None:
            s['start_row'] = {'value': int(args.start_row)}
        if args.end_row is not None:
            s['end_row']   = {'value': int(args.end_row)}

        timestamp = datetime.now().strftime('%Y%m%dT%H:%M:%S')
        logfile   = _cfg_get_str(cfgDict, 'logFile',
                                 '/tmp/timesat_<startDate>_<endDate>_<now>.log')
        logfile   = (logfile
                     .replace('<now>',       timestamp)
                     .replace('<startDate>', _cfg_get_str(cfgDict, 'startDate', '00000000'))
                     .replace('<endDate>',   _cfg_get_str(cfgDict, 'endDate',   '00000000')))
        os.makedirs(os.path.dirname(logfile), exist_ok=True)
        logger = getLogger('TIMESAT', logfile)
        logger.debug(json.dumps(cfgDict, indent=2))

        mode = cfgDict.get('inputFiletype', '').lower()
        if mode not in ('netcdf', 'tif'):
            raise ValueError("cfgDict['inputFiletype'] must be 'netcdf' or 'tif'.")

        start = time()
        if mode == 'netcdf':
            if 'inDataFile' not in cfgDict:
                raise KeyError("NetCDF mode requires cfgDict['inDataFile'].")
            logger.info('Processing NetCDF dataset -> single NetCDF output (serial)')
            timesat_netcdf(cfgDict, logger.info)
        else:
            if 'ppifileList' not in cfgDict:
                pp, qa, nfiles, ndays = discoverInputs(cfgDict, logger.info)
                cfgDict['ppifileList'] = pp
                cfgDict['qafileList']  = qa
                cfgDict['nrFiles']     = nfiles
                cfgDict['nrDays']      = ndays

            cfgDict['settings']['precreate_outputs']['value'] = not args.no_precreate
            cfgDict['settings']['rows_per_task']['value']     = args.rows_per_task
            cfgDict['settings']['block_rows']['value']        = args.block_rows
            cfgDict['settings']['tiled']['value']             = args.tiled
            cfgDict['settings']['bigtiff']['value']           = args.bigtiff

            logger.info('Processing GeoTIFF stack -> parallel compute + safe block writes')
            timesat_tif(cfgDict, logger.info, n_workers=args.workers)

        end = time()
        logger.info(f'Finished {cfgDict.get("tile","")} in {end - start:.1f} s')

    except Exception:
        traceback.print_exc()
        sys.exit(1)

    sys.exit(0)


if __name__ == '__main__':
    main()
