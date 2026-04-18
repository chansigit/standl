# Audit: GSE234903

Worst severity: **warn** | Records: 8 (ok=6 warn=2 fail=0)
Generated: 2026-04-18T09:13:07.536963+00:00

## Results

### WARN — no_confound
- **WARN** condition is perfectly confounded with 'batch'
    - `column`: batch
    - `mapping`: ['2021_autumn→NVW', '2021_spring→VW', '2021_summer→NVW']
- **WARN** condition is perfectly confounded with 'donor_id'
    - `column`: donor_id
    - `mapping`: ['Pt1→VW', 'Pt2→VW', 'Pt3→NVW', 'Pt4→NVW', 'Pt5→NVW']

### OK — contrasts_valid
- **OK** all 1 contrast(s) reference declared factors/levels

### OK — files_in_manifest
- **OK** every sample file has a manifest entry with status=ok

### OK — files_on_disk
- **OK** all manifest files present; shallow (size) integrity ok

### OK — no_orphan_raw
- **OK** no orphan files under raw/

### OK — ontology_format
- **OK** all ontology terms match expected prefix:ID pattern

### OK — sample_id_valid
- **OK** all 5 sample_ids unique and filesystem-safe
