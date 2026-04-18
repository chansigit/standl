# Audit: GSE96583

Worst severity: **warn** | Records: 7 (ok=6 warn=1 fail=0)
Generated: 2026-04-18T03:19:11.474088+00:00

## Results

### WARN — no_confound
- **WARN** condition is perfectly confounded with 'donor_id'
    - `column`: donor_id
    - `mapping`: ['batch1_well1→baseline', 'batch1_well2→baseline', 'batch1_well3→baseline', 'batch2_ctrl→control', 'batch2_stim→IFN_beta']

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
