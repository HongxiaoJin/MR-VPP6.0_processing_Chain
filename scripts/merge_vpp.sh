#!/usr/bin/env bash
#
# Mosaic MR-VPP tiles, optionally mask water pixels using the
# PROBA-V-grid land-cover tiles, reproject to EPSG:3035 at 300 m,
# and write final Cloud Optimized GeoTIFFs.
#
# Water mask:
#   LC = 210
#
# Output datatype and NoData:
#   QA            : Byte / UInt8, NoData = 255
#   Other metrics : Int16,       NoData = -9999
#
# Input tile structure:
#   LSP_ROOT/X12Y00/
#   LSP_ROOT/X12Y01/
#   ...
#
# Corresponding land-cover files:
#   LANDCOVER_ROOT/X12Y00_LC_resampled.tif
#   LANDCOVER_ROOT/X12Y01_LC_resampled.tif
#   ...
#
# Usage:
#   ./merge_vpp.sh \
#       LSP_ROOT \
#       ./MRVPP60_VPP_LAEA_300m \
#       1 LANDCOVER_ROOT 2000 2024
# Note with LC mask, some VPP values at LC=210 edge are changed somehow

# Arguments:
#   1. Input LSP root folder
#   2. Output folder
#   3. Apply land-cover water mask:
#        1 = yes
#        0 = no
# Example without water masking:
#   ./merge_vpp.sh LSP_ROOT ./MRVPP60_VPP_LAEA_300m_0 0

set -Eeuo pipefail

echo "[START] merge_vpp_watermask.sh"
echo "[START] Date: $(date)"
echo "[START] Working directory: $(pwd)"
echo "[START] Arguments: $*"

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

WATER_CLASS=210

if [[ $# -lt 2 || $# -gt 6 ]]; then
    echo "Usage: $0 <input_LSP_root> <output_folder> [LC_mask:1|0] [landcover_root] [start_year] [end_year]" >&2
    exit 1
fi

ROOT="${1%/}"
OUTDIR="${2%/}"
LC_MASK="${3:-1}"
LCROOT="${4:-}"
START_YEAR="${5:-2000}"
END_YEAR="${6:-$(date +%Y)}"

if [[ "$LC_MASK" != "0" && "$LC_MASK" != "1" ]]; then
    echo "ERROR: LC_mask must be either 1 or 0." >&2
    echo "Received: $LC_MASK" >&2
    exit 2
fi

if [[ ! -d "$ROOT" ]]; then
    echo "ERROR: Input root does not exist:" >&2
    echo "       $ROOT" >&2
    exit 2
fi

if [[ "$LC_MASK" == "1" && -z "$LCROOT" ]]; then
    echo "ERROR: landcover_root is required when LC_mask=1." >&2
    exit 2
fi

if [[ "$LC_MASK" == "1" && ! -d "$LCROOT" ]]; then
    echo "ERROR: Land-cover folder does not exist:" >&2
    echo "       $LCROOT" >&2
    exit 2
fi

SEASONS=(
    "season1"
    "season2"
)

mapfile -t YEARS < <(seq "$START_YEAR" "$END_YEAR")

PARAMS=(
    "SOSD"
    "SOSV"
    "LSLOPE"
    "EOSD"
    "EOSV"
    "RSLOPE"
    "LENGTH"
    "MINV"
    "MAXD"
    "MAXV"
    "AMPL"
    "TPROD"
    "SPROD"
    "QA"
)

# Target projection
T_SRS="EPSG:3035"

# Nearest-neighbour resampling preserves integer and QA values.
RESAMP="near"

# Target resolution in metres
TR=(300 300)

# Fixed target extent in EPSG:3035:
# xmin ymin xmax ymax
TE=(
    -1904947.8001090204
    239631.81475310214
    8281852.19989098
    6728331.814753102
)

mkdir -p "$OUTDIR"

# ---------------------------------------------------------------------
# Check required programs
# ---------------------------------------------------------------------

REQUIRED_COMMANDS=(
    "gdal_calc.py"
    "gdalbuildvrt"
    "gdal_translate"
    "gdalwarp"
    "gdalinfo"
)

for command_name in "${REQUIRED_COMMANDS[@]}"; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "ERROR: Required command is not available: $command_name" >&2
        echo "Activate the cglops environment before running the script." >&2
        exit 1
    fi
done

echo "[INFO] GDAL: $(gdalinfo --version)"
echo "[INFO] Input root: $ROOT"
echo "[INFO] Output directory: $OUTDIR"
echo "[INFO] Water masking: $LC_MASK"
echo "[INFO] Target CRS: $T_SRS"
echo "[INFO] Target resolution: ${TR[0]} x ${TR[1]} m"

if [[ "$LC_MASK" == "1" ]]; then
    echo "[INFO] Land-cover root: $LCROOT"
    echo "[INFO] Water class: $WATER_CLASS"
fi

# ---------------------------------------------------------------------
# Temporary-file cleanup
# ---------------------------------------------------------------------

rawlist=""
listfile=""
maskdir=""
vrt=""
mosaic_tif=""
warp_tif=""
tmp_cog=""

cleanup_current() {
    [[ -n "$rawlist" ]] && rm -f "$rawlist"
    [[ -n "$listfile" ]] && rm -f "$listfile"
    [[ -n "$vrt" ]] && rm -f "$vrt"
    [[ -n "$mosaic_tif" ]] && rm -f "$mosaic_tif"
    [[ -n "$warp_tif" ]] && rm -f "$warp_tif"
    [[ -n "$tmp_cog" ]] && rm -f "$tmp_cog"

    if [[ -n "$maskdir" && -d "$maskdir" ]]; then
        rm -rf "$maskdir"
    fi
}

trap 'status=$?;
      echo "[ERROR] Script failed near line ${BASH_LINENO[0]} with status ${status}" >&2;
      cleanup_current;
      exit "$status"' ERR

trap 'cleanup_current; exit 130' INT TERM

# ---------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------

for season in "${SEASONS[@]}"; do

    case "$season" in
        season1)
            scode="S1"
            ;;
        season2)
            scode="S2"
            ;;
        *)
            echo "ERROR: Unsupported season: $season" >&2
            exit 1
            ;;
    esac

    for year in "${YEARS[@]}"; do
        for param in "${PARAMS[@]}"; do

            # Reset current temporary-file variables.
            rawlist=""
            listfile=""
            maskdir=""
            vrt=""
            mosaic_tif=""
            warp_tif=""
            tmp_cog=""

            # ---------------------------------------------------------
            # Datatype and NoData
            # ---------------------------------------------------------

            if [[ "$param" == "QA" ]]; then
                GDAL_TYPE="Byte"
                NODATA="255"
                PREDICTOR_OPTIONS=()
            else
                GDAL_TYPE="Int16"
                NODATA="-9999"
                PREDICTOR_OPTIONS=(-co PREDICTOR=2)
            fi

            rawlist="$(
                mktemp \
                "/tmp/raw_${param}_${year}_${season}_XXXXXX.txt"
            )"

            listfile="$(
                mktemp \
                "/tmp/list_${param}_${year}_${season}_XXXXXX.txt"
            )"

            vrt="${OUTDIR}/${param}_${year}_${season}.vrt"

            mosaic_tif="${OUTDIR}/${param}_${year}_${season}_mosaic_tmp.tif"

            warp_tif="${OUTDIR}/${param}_${year}_${season}_epsg3035_tmp.tif"

            cog_tif="${OUTDIR}/clms_MR-VPP_300-${param}-${scode}_${year}01010000_EU_MODIS_V6.0.1.tiff"

            tmp_cog="${cog_tif}.tmp.tif"

            # Remove files left by an interrupted previous execution.
            rm -f \
                "$vrt" \
                "$mosaic_tif" \
                "$warp_tif" \
                "$tmp_cog"

            # ---------------------------------------------------------
            # Find input VPP tiles
            # ---------------------------------------------------------

            find "$ROOT" \
                -type f \
                -name "*_${year}_${season}_${param}.tif" \
                -print \
                | sort > "$rawlist"

            n=$(wc -l < "$rawlist")
            n=${n//[[:space:]]/}

            echo
            echo "============================================================"
            echo "Parameter : $param"
            echo "Year      : $year"
            echo "Season    : $season"
            echo "Tiles     : $n"
            echo "Datatype  : $GDAL_TYPE"
            echo "NoData    : $NODATA"
            echo "LC mask   : $LC_MASK"
            echo "============================================================"

            if [[ "$n" -eq 0 ]]; then
                echo "[$param $year $season] No input tiles found; skipping."

                cleanup_current
                continue
            fi

            > "$listfile"

            # ---------------------------------------------------------
            # Apply water mask separately to each tile
            # ---------------------------------------------------------

            if [[ "$LC_MASK" == "1" ]]; then

                maskdir="$(
                    mktemp -d \
                    "/tmp/masked_${param}_${year}_${season}_XXXXXX"
                )"

                while IFS= read -r src; do

                    [[ -z "$src" ]] && continue

                    # Extract tile identifier such as X12Y00 from the
                    # complete path.
                    tile="$(
                        grep -oE 'X[0-9]{2}Y[0-9]{2}' <<< "$src" \
                        | head -n 1 \
                        || true
                    )"

                    if [[ -z "$tile" ]]; then
                        echo "ERROR: Could not identify an X##Y## tile from:" >&2
                        echo "       $src" >&2
                        exit 1
                    fi

                    lc="${LCROOT}/${tile}_LC_resampled.tif"

                    if [[ ! -f "$lc" ]]; then
                        echo "ERROR: Missing land-cover file for tile $tile:" >&2
                        echo "       $lc" >&2
                        exit 1
                    fi

                    src_filename="$(basename "$src")"
                    src_stem="${src_filename%.*}"

                    masked="${maskdir}/${src_stem}_watermasked.tif"

                    echo "[$param $year $season] Masking tile $tile"

                    # Pixels with LC = 210 are replaced by the output
                    # layer's NoData value.
                    #
                    # QA:
                    #   LC == 210 -> 255
                    #
                    # Other parameters:
                    #   LC == 210 -> -9999
                    #
                    # --hideNoData means the land-cover raster's own
                    # NoData is not automatically masked. Only LC=210
                    # is explicitly treated as water.
                    gdal_calc.py \
                        -A "$src" \
                        -B "$lc" \
                        --calc="where(B==${WATER_CLASS},${NODATA},A)" \
                        --type="$GDAL_TYPE" \
                        --NoDataValue="$NODATA" \
                        --hideNoData \
                        --creation-option=TILED=YES \
                        --creation-option=BLOCKXSIZE=512 \
                        --creation-option=BLOCKYSIZE=512 \
                        --creation-option=COMPRESS=LZW \
                        --creation-option=BIGTIFF=YES \
                        --overwrite \
                        --outfile="$masked" \
                        --quiet

                    if [[ ! -s "$masked" ]]; then
                        echo "ERROR: Masked tile was not created:" >&2
                        echo "       $masked" >&2
                        exit 1
                    fi

                    echo "$masked" >> "$listfile"

                done < "$rawlist"

            else
                cp "$rawlist" "$listfile"
            fi

            prepared_n=$(wc -l < "$listfile")
            prepared_n=${prepared_n//[[:space:]]/}

            if [[ "$prepared_n" -ne "$n" ]]; then
                echo "ERROR: Number of prepared tiles does not match input." >&2
                echo "Input tiles:    $n" >&2
                echo "Prepared tiles: $prepared_n" >&2
                exit 1
            fi

            # ---------------------------------------------------------
            # Build source mosaic VRT
            # ---------------------------------------------------------

            echo "[$param $year $season] Building VRT mosaic"

            gdalbuildvrt \
                -overwrite \
                -srcnodata "$NODATA" \
                -vrtnodata "$NODATA" \
                -input_file_list "$listfile" \
                "$vrt"

            if [[ ! -s "$vrt" ]]; then
                echo "ERROR: VRT was not created:" >&2
                echo "       $vrt" >&2
                exit 1
            fi

            # ---------------------------------------------------------
            # Translate the VRT to a temporary tiled GeoTIFF
            # ---------------------------------------------------------

            echo "[$param $year $season] Creating temporary mosaic"

            gdal_translate \
                "$vrt" \
                "$mosaic_tif" \
                -of GTiff \
                -ot "$GDAL_TYPE" \
                -a_nodata "$NODATA" \
                -co TILED=YES \
                -co BLOCKXSIZE=512 \
                -co BLOCKYSIZE=512 \
                -co COMPRESS=LZW \
                "${PREDICTOR_OPTIONS[@]}" \
                -co BIGTIFF=YES \
                -co NUM_THREADS=ALL_CPUS

            if [[ ! -s "$mosaic_tif" ]]; then
                echo "ERROR: Temporary mosaic was not created:" >&2
                echo "       $mosaic_tif" >&2
                exit 1
            fi

            # ---------------------------------------------------------
            # Reproject to EPSG:3035
            # ---------------------------------------------------------

            echo "[$param $year $season] Reprojecting to EPSG:3035"

            gdalwarp \
                "$mosaic_tif" \
                "$warp_tif" \
                -overwrite \
                -of GTiff \
                -ot "$GDAL_TYPE" \
                -t_srs "$T_SRS" \
                -te "${TE[@]}" \
                -te_srs "$T_SRS" \
                -tr "${TR[@]}" \
                -r "$RESAMP" \
                -srcnodata "$NODATA" \
                -dstnodata "$NODATA" \
                -multi \
                -wo NUM_THREADS=ALL_CPUS \
                -wo INIT_DEST=NO_DATA \
                -co TILED=YES \
                -co BLOCKXSIZE=512 \
                -co BLOCKYSIZE=512 \
                -co COMPRESS=LZW \
                "${PREDICTOR_OPTIONS[@]}" \
                -co BIGTIFF=YES \
                -co NUM_THREADS=ALL_CPUS

            if [[ ! -s "$warp_tif" ]]; then
                echo "ERROR: Reprojected raster was not created:" >&2
                echo "       $warp_tif" >&2
                exit 1
            fi

            # ---------------------------------------------------------
            # Convert to Cloud Optimized GeoTIFF
            #
            # This uses the same proven COG options as your working
            # Linux/SLURM conversion script.
            # ---------------------------------------------------------

            echo "[$param $year $season] Creating final COG"

            rm -f "$tmp_cog"

            gdal_translate \
                "$warp_tif" \
                "$tmp_cog" \
                -of COG \
                -ot "$GDAL_TYPE" \
                -a_nodata "$NODATA" \
                -co COMPRESS=LZW \
                -co BIGTIFF=YES

            if [[ ! -s "$tmp_cog" ]]; then
                echo "ERROR: Temporary COG was not created:" >&2
                echo "       $tmp_cog" >&2
                exit 1
            fi

            # Replace the final output only after successful creation.
            mv -f "$tmp_cog" "$cog_tif"
            tmp_cog=""

            # ---------------------------------------------------------
            # Validate datatype and NoData
            # ---------------------------------------------------------

            info="$(gdalinfo "$cog_tif")"

            if [[ "$param" == "QA" ]]; then

                if ! grep -qE 'Band 1 Block=.*Type=Byte' <<< "$info"; then
                    echo "ERROR: QA output is not Byte/UInt8:" >&2
                    echo "       $cog_tif" >&2
                    exit 1
                fi

                if ! grep -q 'NoData Value=255' <<< "$info"; then
                    echo "ERROR: QA NoData is not 255:" >&2
                    echo "       $cog_tif" >&2
                    exit 1
                fi

            else

                if ! grep -qE 'Band 1 Block=.*Type=Int16' <<< "$info"; then
                    echo "ERROR: Metric output is not Int16:" >&2
                    echo "       $cog_tif" >&2
                    exit 1
                fi

                if ! grep -q 'NoData Value=-9999' <<< "$info"; then
                    echo "ERROR: Metric NoData is not -9999:" >&2
                    echo "       $cog_tif" >&2
                    exit 1
                fi

            fi

            echo "[$param $year $season] Validation passed"
            echo "[$param $year $season] Output: $cog_tif"

            # ---------------------------------------------------------
            # Remove intermediate files
            # ---------------------------------------------------------

            cleanup_current

        done
    done
done

trap - ERR INT TERM

echo
echo "[DONE] All processing completed successfully."
echo "[DONE] Output directory: $OUTDIR"
echo "[DONE] Date: $(date)"
