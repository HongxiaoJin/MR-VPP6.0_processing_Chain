#!/usr/bin/env bash
set -euo pipefail

usage() { echo "Usage: $0 --config FILE [--dry-run]" >&2; }
CONFIG=""; DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) [[ $# -ge 2 ]] || { usage; exit 2; }; CONFIG="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done
[[ -n "$CONFIG" ]] || { usage; exit 2; }
[[ -f "$CONFIG" ]] || { echo "Configuration not found: $CONFIG" >&2; exit 2; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS_DIR="${REPO_ROOT}/scripts"

eval "$(python3 - "$CONFIG" <<'PY'
import datetime, json, shlex, sys
from pathlib import Path
c = json.loads(Path(sys.argv[1]).read_text())
required = ("data_root", "tiles", "start_year", "timesat_config")
missing = [k for k in required if k not in c]
if missing:
    raise SystemExit("Missing configuration keys: " + ", ".join(missing))
if not isinstance(c["tiles"], list) or not c["tiles"]:
    raise SystemExit("Configuration key 'tiles' must be a non-empty list")
values = {
    "DATA_ROOT": str(Path(c["data_root"]).expanduser()),
    "START_YEAR": int(c["start_year"]),
    "END_YEAR": int(c.get("end_year") or datetime.date.today().year),
    "WORKERS": int(c.get("workers", 2)),
    "BLOCK_ROWS": int(c.get("block_rows", 240)),
    "RASTER_BLOCK": int(c.get("raster_block", 840)),
    "PYTHON_BIN": str(c.get("python_executable", sys.executable)),
    "TIMESAT_CONFIG": str(Path(c["timesat_config"]).expanduser()),
    "TILES": " ".join(str(x) for x in c["tiles"]),
}
if values["START_YEAR"] > values["END_YEAR"]:
    raise SystemExit("start_year must not be later than end_year")
for key, value in values.items():
    print(f"{key}={shlex.quote(str(value))}")
PY
)"

read -r -a TILES_ARRAY <<< "$TILES"
[[ "$DRY_RUN" -eq 1 || -x "$PYTHON_BIN" ]] || { echo "Python executable is unavailable: $PYTHON_BIN" >&2; exit 2; }
[[ "$DRY_RUN" -eq 1 || -d "$DATA_ROOT" ]] || { echo "Data root does not exist: $DATA_ROOT" >&2; exit 2; }
[[ "$DRY_RUN" -eq 1 || -f "$TIMESAT_CONFIG" ]] || { echo "TIMESAT configuration not found: $TIMESAT_CONFIG" >&2; exit 2; }

run() {
  printf '+ '; printf '%q ' "$@"; printf '\n'
  [[ "$DRY_RUN" -eq 1 ]] || "$@"
}

for TILE in "${TILES_ARRAY[@]}"; do
  A4_TILE="${DATA_ROOT}/MCD43A4_Sen3_tile/${TILE}"
  A2_TILE="${DATA_ROOT}/MCD43A2_Sen3_tile/${TILE}"
  MDVI_OUT="${DATA_ROOT}/MDVI/MDVI_Q95_${TILE}_${START_YEAR}_${END_YEAR}.tif"
  [[ "$DRY_RUN" -eq 1 ]] || mkdir -p "${DATA_ROOT}/logs/${TILE}" "$(dirname "$MDVI_OUT")"

  run "$PYTHON_BIN" "$SCRIPTS_DIR/Download_MODIS_MCD43A4_to_SEN3.py" --tile "$TILE" --base-dir "$DATA_ROOT" --start-year "$START_YEAR" --end-year "$END_YEAR" --resume
  run "$PYTHON_BIN" "$SCRIPTS_DIR/Download_MODIS_MCD43A2_to_SEN3.py" --tile "$TILE" --base-dir "$DATA_ROOT" --start-year "$START_YEAR" --end-year "$END_YEAR" --resume
  run "$PYTHON_BIN" "$SCRIPTS_DIR/sbaf_modis_mcd43a4_to_s3.py" --tile "$TILE" --base-dir "${DATA_ROOT}/MCD43A4_Sen3_tile"
  run "$PYTHON_BIN" "$SCRIPTS_DIR/mdvi_q95_modis_sbaf.py" --tile "$TILE" --nir-dir "${A4_TILE}/NIR_sbaf" --red-dir "${A4_TILE}/RED_sbaf" --nir-qa-dir "${A2_TILE}/NIR_QA" --red-qa-dir "${A2_TILE}/RED_QA" --out "$MDVI_OUT" --year-min "$START_YEAR" --year-max "$END_YEAR" --block "$RASTER_BLOCK"
  run "$PYTHON_BIN" "$SCRIPTS_DIR/ppi_modis_sbaf.py" --tile "$TILE" --red-dir "${A4_TILE}/RED_sbaf" --nir-dir "${A4_TILE}/NIR_sbaf" --red-qa-dir "${A2_TILE}/RED_QA" --nir-qa-dir "${A2_TILE}/NIR_QA" --sza-dir "${A2_TILE}/SZA" --mdvi "$MDVI_OUT" --out-dir "$DATA_ROOT" --block "$RASTER_BLOCK"
  run "$PYTHON_BIN" "$SCRIPTS_DIR/timesat41_mp.py" --cfgFile "$TIMESAT_CONFIG" --tile "$TILE" --start-date "${START_YEAR}0101" --end-date "${END_YEAR}1231" --workers "$WORKERS" --rows-per-task 1 --block-rows "$BLOCK_ROWS" --tiled --bigtiff
done
