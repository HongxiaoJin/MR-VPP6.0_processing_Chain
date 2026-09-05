import importlib.util
import sys
import types
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    osgeo = types.ModuleType("osgeo")
    osgeo.gdal = types.SimpleNamespace(UseExceptions=lambda: None)
    sys.modules.setdefault("osgeo", osgeo)
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_filename_parsing_and_construction():
    ppi = load_script("ppi_modis_sbaf")
    assert ppi.parse_year_doy("x_2001_006.tif") == (2001, 6)
    assert ppi.parse_year_doy("not-a-date.tif") == (None, None)
    expected = "MCD43A4_NBAR_CGLS_PBV_X01Y02_RED_sbaf_2001_006.tif"
    assert ppi.build_refl_path(Path("base"), "X01Y02", "RED_sbaf", 2001, 6).name == expected


def test_confirmed_sbaf_expressions_are_unchanged():
    sbaf = load_script("sbaf_modis_mcd43a4_to_s3")
    red = np.array([0.1, 0.2], dtype=np.float32)
    nir = np.array([0.3, 0.4], dtype=np.float32)
    v = np.clip((nir - red) / (nir + red + 1e-10), -1.0, 1.0)
    expected_red = red * (1.063247 + 0.255060 * v - 0.471804 * v**2)
    expected_nir = nir * (1.044383 + 0.007799 * v + 0.0 * v**2)
    np.testing.assert_allclose(sbaf.sbaf_red(red, nir), expected_red)
    np.testing.assert_allclose(sbaf.sbaf_nir(red, nir), expected_nir)


def test_timesat_filename_timevector():
    # Avoid importing the unavailable restricted extension at collection time.
    core = types.ModuleType("cglopstsfprocess")
    core.cglopstsfprocess = lambda *args, **kwargs: None
    sys.modules.setdefault("cglopstsfprocess", core)
    timesat = load_script("timesat41_mp")
    values = timesat._readtv_from_filenames_(
        "", ["MODIS_sbaf_X01Y02.2000001-ppi.tif", "MODIS_sbaf_X01Y02.2000006-ppi.tif"]
    )
    assert values.tolist() == [2000001, 2000006]
