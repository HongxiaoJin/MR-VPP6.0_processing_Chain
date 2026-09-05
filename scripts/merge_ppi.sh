#!/usr/bin/env bash
#
# Mosaic 5-day MODIS SBAF PPI and PPI-QA tiles over the 44-tile domain,
# reproject to the fixed MR-VPP EPSG:3035 grid at 300 m, and write
# Cloud Optimized GeoTIFFs.
#
# No land-cover mask is applied.
#
# Input examples:
#
#   ROOT/X12Y00/PPI/MODIS_sbaf_X12Y00.2000057-ppi.tif
#   ROOT/X12Y00/QA/MODIS_sbaf_X12Y00.2000057-qa.tif
#
# Standardized output names:
#
#   PPI:
#   clms_MR-VPP_300-PPI_200002261200_EU_MODIS_V6.0.1.tiff
#
#   QA:
#   clms_MR-VPP_300-QA_ppi_200002261200_EU_MODIS_V6.0.1.tiff
#
# NoData handling:
#
#   PPI input  : Int16, NoData = 32767
#   PPI output : Int16, NoData = -9999
#
#   QA input   : Byte/UInt8, NoData = 255
#   QA output  : Byte/UInt8, NoData = 255
#
# Output grid:
#
#   CRS        : EPSG:3035
#   Resolution : 300 x 300 m
#   Extent:
#     xmin = -1904947.8001090204
#     ymin =   239631.81475310214
#     xmax =  8281852.19989098
#     ymax =  6728331.814753102
#
#   Size:
#     33956 columns x 21629 rows
#
# Save as:
#
#   merge_ppi.sh
#
# Submit with:
#
#   ./merge_ppi.sh \
#       DATA_ROOT/PPI_QA \
#       ./MRVPP60_TS_LAEA_300m \
#       both \
#       2
#
# Arguments:
#
#   1. Input root
#   2. Output root
#   3. Product mode: ppi, qa, or both
#   4. Number of dates processed concurrently
#
# Recommended:
#
#   Start with 1 or 2 concurrent dates.

set -Eeuo pipefail

# ---------------------------------------------------------------------
# Command-line arguments
# ---------------------------------------------------------------------

if [[ $# -lt 2 || $# -gt 4 ]]; then
    cat >&2 <<EOF
Usage:
  $0 <input_root> <output_root> [ppi|qa|both] [parallel_dates]

Example:
  $0 \
    DATA_ROOT/PPI_QA \
    ./MRVPP60_TS_LAEA_300m \
    both \
    2
EOF
    exit 1
fi

ROOT="${1%/}"
OUTROOT="${2%/}"
MODE="${3:-both}"
WORKERS="${4:-2}"

if [[ ! -d "$ROOT" ]]; then
    echo "ERROR: Input root does not exist:" >&2
    echo "       $ROOT" >&2
    exit 2
fi

case "$MODE" in
    ppi|qa|both)
        ;;
    *)
        echo "ERROR: Mode must be ppi, qa, or both." >&2
        echo "       Received: $MODE" >&2
        exit 2
        ;;
esac

if ! [[ "$WORKERS" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: parallel_dates must be a positive integer." >&2
    echo "       Received: $WORKERS" >&2
    exit 2
fi

# ---------------------------------------------------------------------
# Fixed 44-tile domain
# ---------------------------------------------------------------------

TILES=(
    X12Y00 X12Y01 X12Y02
    X13Y00 X13Y01
    X14Y00 X14Y01 X14Y03
    X15Y00 X15Y01 X15Y03
    X16Y00 X16Y01 X16Y02 X16Y04
    X17Y00 X17Y01 X17Y02 X17Y03 X17Y04
    X18Y01 X18Y02 X18Y03 X18Y04
    X19Y00 X19Y01 X19Y02 X19Y03 X19Y04
    X20Y00 X20Y01 X20Y02 X20Y03 X20Y04
    X21Y00 X21Y01 X21Y02 X21Y03 X21Y04
    X22Y00 X22Y01 X22Y02 X22Y03 X22Y04
)

EXPECTED_TILE_COUNT="${#TILES[@]}"
TILE_STRING="${TILES[*]}"

# ---------------------------------------------------------------------
# Fixed output grid, identical to the VPP products
# ---------------------------------------------------------------------

T_SRS="EPSG:3035"

TE_XMIN="-1904947.8001090204"
TE_YMIN="239631.81475310214"
TE_XMAX="8281852.19989098"
TE_YMAX="6728331.814753102"

XRES="300"
YRES="300"

EXPECTED_WIDTH="33956"
EXPECTED_HEIGHT="21629"

RESAMPLING="near"

# ---------------------------------------------------------------------
# CPU configuration
# ---------------------------------------------------------------------

TOTAL_CPUS="${SLURM_CPUS_PER_TASK:-20}"
THREADS_PER_JOB=$(( TOTAL_CPUS / WORKERS ))

if [[ "$THREADS_PER_JOB" -lt 1 ]]; then
    THREADS_PER_JOB=1
fi

# ---------------------------------------------------------------------
# Temporary processing directory
# ---------------------------------------------------------------------

if [[ -n "${SNIC_TMP:-}" && -d "${SNIC_TMP:-}" ]]; then
    TMPROOT="${SNIC_TMP}/ppiqa_mosaic_${SLURM_JOB_ID:-$$}"
elif [[ -n "${TMPDIR:-}" && -d "${TMPDIR:-}" ]]; then
    TMPROOT="${TMPDIR}/ppiqa_mosaic_${SLURM_JOB_ID:-$$}"
else
    TMPROOT="${OUTROOT}/.tmp_ppiqa_mosaic_${SLURM_JOB_ID:-$$}"
fi

mkdir -p \
    "$OUTROOT/PPI" \
    "$OUTROOT/QA" \
    "$OUTROOT/logs" \
    "$TMPROOT"

# ---------------------------------------------------------------------
# Check required programs
# ---------------------------------------------------------------------

REQUIRED_COMMANDS=(
    gdalbuildvrt
    gdalwarp
    gdal_translate
    gdalinfo
    date
    find
    sort
    sed
    grep
    xargs
)

for command_name in "${REQUIRED_COMMANDS[@]}"; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "ERROR: Required command is unavailable: $command_name" >&2
        exit 1
    fi
done

echo "============================================================"
echo "MODIS SBAF PPI and QA mosaicking"
echo "============================================================"
echo "SLURM job ID         : ${SLURM_JOB_ID:-interactive}"
echo "Input root           : $ROOT"
echo "Output root          : $OUTROOT"
echo "Mode                 : $MODE"
echo "Expected tiles/date  : $EXPECTED_TILE_COUNT"
echo "Parallel dates       : $WORKERS"
echo "GDAL threads/date    : $THREADS_PER_JOB"
echo "Temporary root       : $TMPROOT"
echo "GDAL                 : $(gdalinfo --version)"
echo "Target CRS           : $T_SRS"
echo "Target resolution    : ${XRES} x ${YRES} m"
echo "Expected raster size : ${EXPECTED_WIDTH} x ${EXPECTED_HEIGHT}"
echo "Land-cover mask      : none"
echo "PPI NoData           : 32767 -> -9999"
echo "QA NoData            : 255 -> 255"
echo "============================================================"

# ---------------------------------------------------------------------
# Convert YYYYDDD to YYYYMMDD1200
#
# Example:
#   2000057 -> 200002261200
# ---------------------------------------------------------------------

yyyyddd_to_timestamp() {
    local yyyyddd="$1"
    local year
    local doy_text
    local doy
    local calendar_date
    local converted_year

    if [[ ! "$yyyyddd" =~ ^[0-9]{7}$ ]]; then
        echo "ERROR: Invalid YYYYDDD value: $yyyyddd" >&2
        return 1
    fi

    year="${yyyyddd:0:4}"
    doy_text="${yyyyddd:4:3}"

    # Force decimal interpretation.
    doy=$((10#$doy_text))

    if (( doy < 1 || doy > 366 )); then
        echo "ERROR: Invalid day of year: $yyyyddd" >&2
        return 1
    fi

    calendar_date="$(
        date \
            --date="${year}-01-01 +$((doy - 1)) days" \
            "+%Y%m%d"
    )"

    converted_year="${calendar_date:0:4}"

    # Reject day 366 in a non-leap year.
    if [[ "$converted_year" != "$year" ]]; then
        echo "ERROR: Invalid YYYYDDD date: $yyyyddd" >&2
        return 1
    fi

    printf '%s1200\n' "$calendar_date"
}

# ---------------------------------------------------------------------
# Discover all available dates for one product
# ---------------------------------------------------------------------

discover_dates() {
    local product="$1"
    local subdir
    local suffix

    case "$product" in
        PPI)
            subdir="PPI"
            suffix="ppi"
            ;;
        QA)
            subdir="QA"
            suffix="qa"
            ;;
        *)
            echo "ERROR: Unsupported product: $product" >&2
            return 1
            ;;
    esac

    find "$ROOT" \
        -type f \
        -path "*/${subdir}/MODIS_sbaf_X??Y??.*-${suffix}.tif" \
        -printf '%f\n' \
        | sed -nE \
            "s/^MODIS_sbaf_X[0-9]{2}Y[0-9]{2}\.([0-9]{7})-${suffix}\.tif$/\1/p" \
        | sort -u
}

# ---------------------------------------------------------------------
# Process one product for one YYYYDDD date
# ---------------------------------------------------------------------

process_one_date() {
    local product="$1"
    local yyyyddd="$2"

    local subdir
    local suffix
    local datatype
    local input_nodata
    local output_nodata
    local product_lower
    local timestamp

    local outdir
    local outfile
    local tmp_cog

    local workdir
    local listfile
    local vrt
    local warp_tif

    local tile
    local src
    local found
    local missing

    local info
    local output_ok

    local -a local_tiles

    case "$product" in
        PPI)
            subdir="PPI"
            suffix="ppi"
            datatype="Int16"
            input_nodata="32767"
            output_nodata="-9999"
            product_lower="ppi"
            ;;
        QA)
            subdir="QA"
            suffix="qa"
            datatype="Byte"
            input_nodata="255"
            output_nodata="255"
            product_lower="qa"
            ;;
        *)
            echo "ERROR: Unsupported product: $product" >&2
            return 1
            ;;
    esac

    timestamp="$(yyyyddd_to_timestamp "$yyyyddd")"

    outdir="${OUTROOT}/${product}"

    if [[ "$product" == "PPI" ]]; then
        outfile="${outdir}/clms_MR-VPP_300-PPI_${timestamp}_EU_MODIS_V6.0.1.tiff"
    else
        outfile="${outdir}/clms_MR-VPP_300-QA_ppi_${timestamp}_EU_MODIS_V6.0.1.tiff"
    fi

    tmp_cog="${outfile}.tmp.tiff"

    workdir="${TMPROOT}/${product_lower}_${yyyyddd}_$$"
    listfile="${workdir}/tiles.txt"
    vrt="${workdir}/${product_lower}_${yyyyddd}.vrt"
    warp_tif="${workdir}/${product_lower}_${yyyyddd}_epsg3035.tif"

    mkdir -p "$workdir" "$outdir"

    cleanup_one_date() {
        rm -rf "$workdir"
        rm -f "$tmp_cog"
    }

    trap cleanup_one_date EXIT INT TERM

    # -------------------------------------------------------------
    # Skip an existing output only when it is valid
    # -------------------------------------------------------------

    if [[ -s "$outfile" ]]; then
        info="$(gdalinfo "$outfile" 2>/dev/null || true)"
        output_ok=1

        if ! grep -q \
            "Size is ${EXPECTED_WIDTH}, ${EXPECTED_HEIGHT}" \
            <<< "$info"; then
            output_ok=0
        fi

        if ! grep -q \
            "NoData Value=${output_nodata}" \
            <<< "$info"; then
            output_ok=0
        fi

        if [[ "$product" == "PPI" ]]; then
            if ! grep -qE \
                "Band 1 Block=.*Type=Int16" \
                <<< "$info"; then
                output_ok=0
            fi
        else
            if ! grep -qE \
                "Band 1 Block=.*Type=Byte" \
                <<< "$info"; then
                output_ok=0
            fi
        fi

        if [[ "$output_ok" -eq 1 ]]; then
            echo "[SKIP] ${product} ${yyyyddd} -> ${timestamp}"
            return 0
        fi

        echo "[WARN] Existing output failed validation and will be rebuilt:"
        echo "       $outfile"

        rm -f "$outfile"
    fi

    # -------------------------------------------------------------
    # Build the input list for the 44 expected tiles
    # -------------------------------------------------------------

    : > "$listfile"

    found=0
    missing=0

    read -r -a local_tiles <<< "$TILE_STRING"

    for tile in "${local_tiles[@]}"; do
        src="${ROOT}/${tile}/${subdir}/MODIS_sbaf_${tile}.${yyyyddd}-${suffix}.tif"

        if [[ -s "$src" ]]; then
            printf '%s\n' "$src" >> "$listfile"
            found=$((found + 1))
        else
            echo \
                "[WARN] ${product} ${yyyyddd}: missing ${tile}" \
                >&2

            missing=$((missing + 1))
        fi
    done

    if [[ "$found" -eq 0 ]]; then
        echo \
            "[ERROR] ${product} ${yyyyddd}: no input tiles found" \
            >&2

        return 1
    fi

    echo \
        "[START] ${product} ${yyyyddd} -> ${timestamp}: " \
        "${found}/${EXPECTED_TILE_COUNT} tiles"

    if [[ "$missing" -gt 0 ]]; then
        echo \
            "[WARN] ${product} ${yyyyddd}: " \
            "${missing} tile(s) missing" \
            >&2
    fi

    # -------------------------------------------------------------
    # Build source mosaic VRT
    #
    # Input tile NoData is translated into the desired output NoData:
    #
    # PPI: 32767 -> -9999
    # QA :   255 ->   255
    # -------------------------------------------------------------

    gdalbuildvrt \
        -overwrite \
        -srcnodata "$input_nodata" \
        -vrtnodata "$output_nodata" \
        -input_file_list "$listfile" \
        "$vrt"

    if [[ ! -s "$vrt" ]]; then
        echo \
            "[ERROR] ${product} ${yyyyddd}: VRT was not created" \
            >&2

        return 1
    fi

    # -------------------------------------------------------------
    # Reproject the VRT directly to the fixed VPP grid
    #
    # The gdalwarp source is the VRT, whose NoData value is already
    # output_nodata. Therefore, gdalwarp must use output_nodata for
    # both source and destination.
    #
    # No -tap is used because the exact supplied extent must remain
    # unchanged.
    # -------------------------------------------------------------

    gdalwarp \
        "$vrt" \
        "$warp_tif" \
        -overwrite \
        -of GTiff \
        -ot "$datatype" \
        -t_srs "$T_SRS" \
        -te "$TE_XMIN" "$TE_YMIN" "$TE_XMAX" "$TE_YMAX" \
        -te_srs "$T_SRS" \
        -tr "$XRES" "$YRES" \
        -r "$RESAMPLING" \
        -srcnodata "$output_nodata" \
        -dstnodata "$output_nodata" \
        -multi \
        -wo "NUM_THREADS=${THREADS_PER_JOB}" \
        -wo INIT_DEST=NO_DATA \
        -co TILED=YES \
        -co BLOCKXSIZE=512 \
        -co BLOCKYSIZE=512 \
        -co COMPRESS=LZW \
        -co BIGTIFF=YES \
        -co "NUM_THREADS=${THREADS_PER_JOB}"

    if [[ ! -s "$warp_tif" ]]; then
        echo \
            "[ERROR] ${product} ${yyyyddd}: warped raster missing" \
            >&2

        return 1
    fi

    # -------------------------------------------------------------
    # Create final Cloud Optimized GeoTIFF
    #
    # These COG options are already verified on the Linux system.
    # -------------------------------------------------------------

    rm -f "$tmp_cog"

    gdal_translate \
        "$warp_tif" \
        "$tmp_cog" \
        -of COG \
        -ot "$datatype" \
        -a_nodata "$output_nodata" \
        -co COMPRESS=LZW \
        -co BIGTIFF=YES

    if [[ ! -s "$tmp_cog" ]]; then
        echo \
            "[ERROR] ${product} ${yyyyddd}: COG was not created" \
            >&2

        return 1
    fi

    # Publish the output only after successful COG creation.
    mv -f "$tmp_cog" "$outfile"

    # -------------------------------------------------------------
    # Validate final output
    # -------------------------------------------------------------

    info="$(gdalinfo "$outfile")"

    if ! grep -q \
        "Size is ${EXPECTED_WIDTH}, ${EXPECTED_HEIGHT}" \
        <<< "$info"; then

        echo \
            "[ERROR] ${product} ${yyyyddd}: unexpected dimensions" \
            >&2

        echo \
            "Expected: ${EXPECTED_WIDTH} x ${EXPECTED_HEIGHT}" \
            >&2

        rm -f "$outfile"
        return 1
    fi

    if ! grep -q \
        "NoData Value=${output_nodata}" \
        <<< "$info"; then

        echo \
            "[ERROR] ${product} ${yyyyddd}: incorrect NoData; " \
            "expected ${output_nodata}" \
            >&2

        rm -f "$outfile"
        return 1
    fi

    if [[ "$product" == "PPI" ]]; then
        if ! grep -qE \
            "Band 1 Block=.*Type=Int16" \
            <<< "$info"; then

            echo \
                "[ERROR] PPI ${yyyyddd}: output is not Int16" \
                >&2

            rm -f "$outfile"
            return 1
        fi
    else
        if ! grep -qE \
            "Band 1 Block=.*Type=Byte" \
            <<< "$info"; then

            echo \
                "[ERROR] QA ${yyyyddd}: output is not Byte/UInt8" \
                >&2

            rm -f "$outfile"
            return 1
        fi
    fi

    if ! grep -q 'ID\["EPSG",3035\]' <<< "$info" &&
       ! grep -q 'AUTHORITY.*3035' <<< "$info"; then

        echo \
            "[WARN] ${product} ${yyyyddd}: EPSG:3035 was not " \
            "explicitly found in gdalinfo output" \
            >&2
    fi

    if ! grep -q "LAYOUT=COG" <<< "$info"; then
        echo \
            "[WARN] ${product} ${yyyyddd}: " \
            "LAYOUT=COG not reported by gdalinfo" \
            >&2
    fi

    echo "[DONE ] ${product} ${yyyyddd}"
    echo "        $outfile"

    return 0
}

# ---------------------------------------------------------------------
# Export functions and settings for xargs child shells
# ---------------------------------------------------------------------

export -f yyyyddd_to_timestamp
export -f process_one_date

export ROOT
export OUTROOT
export TMPROOT

export T_SRS
export TE_XMIN
export TE_YMIN
export TE_XMAX
export TE_YMAX

export XRES
export YRES

export EXPECTED_WIDTH
export EXPECTED_HEIGHT
export EXPECTED_TILE_COUNT

export RESAMPLING
export THREADS_PER_JOB
export TILE_STRING

# ---------------------------------------------------------------------
# Process all dates for one product
# ---------------------------------------------------------------------

process_product() {
    local product="$1"
    local dates_file
    local count
    local first_date
    local last_date

    dates_file="${TMPROOT}/dates_${product,,}.txt"

    discover_dates "$product" > "$dates_file"

    count=$(wc -l < "$dates_file")
    count=${count//[[:space:]]/}

    if [[ "$count" -eq 0 ]]; then
        echo "WARNING: No dates found for $product" >&2
        return 0
    fi

    first_date="$(head -n 1 "$dates_file")"
    last_date="$(tail -n 1 "$dates_file")"

    echo
    echo "============================================================"
    echo "Processing product : $product"
    echo "Number of dates    : $count"
    echo "First YYYYDDD      : $first_date"
    echo "First timestamp    : $(yyyyddd_to_timestamp "$first_date")"
    echo "Last YYYYDDD       : $last_date"
    echo "Last timestamp     : $(yyyyddd_to_timestamp "$last_date")"
    echo "Parallel dates     : $WORKERS"
    echo "============================================================"

    xargs \
        -r \
        -n 1 \
        -P "$WORKERS" \
        bash -c 'process_one_date "$1" "$2"' \
        _ \
        "$product" \
        < "$dates_file"
}

# ---------------------------------------------------------------------
# Run requested product series
# ---------------------------------------------------------------------

case "$MODE" in
    ppi)
        process_product "PPI"
        ;;
    qa)
        process_product "QA"
        ;;
    both)
        process_product "PPI"
        process_product "QA"
        ;;
esac

echo
echo "============================================================"
echo "All requested mosaicking completed successfully."
echo
echo "PPI outputs:"
echo "  $OUTROOT/PPI"
echo
echo "QA outputs:"
echo "  $OUTROOT/QA"
echo "============================================================"

rm -rf "$TMPROOT"
