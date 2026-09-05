import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_example_json_files_parse():
    for path in (ROOT / "config").glob("*.json"):
        assert isinstance(json.loads(path.read_text()), dict)


def test_launcher_dry_run(tmp_path):
    timesat = tmp_path / "timesat.local.json"
    timesat.write_text("{}")
    config = tmp_path / "workflow.local.json"
    config.write_text(json.dumps({
        "data_root": str(tmp_path / "data"),
        "tiles": ["X01Y02"],
        "start_year": 2000,
        "end_year": 2001,
        "python_executable": "/not/required/in/dry-run",
        "timesat_config": str(timesat),
    }))
    result = subprocess.run(
        [str(ROOT / "scripts" / "run_modis_sbaf_lsp_chain.sh"),
         "--config", str(config), "--dry-run"],
        text=True, capture_output=True, check=True,
    )
    assert "Download_MODIS_MCD43A4_to_SEN3.py" in result.stdout
    assert "timesat41_mp.py" in result.stdout
    assert not (tmp_path / "data").exists()
