# Configuration

`config/config.local.json` controls the launcher:

- `data_root`: input, intermediate, output, and log root.
- `tiles`: explicit workflow tile identifiers.
- `start_year`: first MODIS year.
- `end_year`: final year, or `null` for the current year.
- `workers`: TIMESAT worker count.
- `block_rows`: TIMESAT rows per processing block.
- `raster_block`: GDAL block width/height for MDVI and PPI.
- `python_executable`: Python interpreter used for every stage.
- `timesat_config`: local TIMESAT JSON path.

The TIMESAT example preserves the operational numerical settings. Replace path
placeholders only unless a scientifically reviewed parameter change is intended.
The `<tile>`, `<year>`, and `<doy>` tokens are expanded by `timesat41_mp.py`.

Both local files are ignored by Git. Paths in examples are placeholders.

The optional merge commands are:

```bash
scripts/merge_ppi.sh INPUT_PPI_QA_ROOT OUTPUT_ROOT both 2
scripts/merge_vpp.sh INPUT_LSP_ROOT OUTPUT_ROOT 0
scripts/merge_vpp.sh INPUT_LSP_ROOT OUTPUT_ROOT 1 LANDCOVER_ROOT 2000 2025
```

The last form applies the existing water-mask rule and permits an explicit year
range. The end year shown is only an example.
