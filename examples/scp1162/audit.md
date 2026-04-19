# Audit: SCP1162

Worst severity: **warn** | Records: 8 (ok=7 warn=1 fail=0)
Generated: 2026-04-19T05:18:49.864148+00:00

## Results

### WARN — extractor_partial_failure
- **WARN** 'scp-broad' could not extract 'files': SCP file listing requires bearer token; download files manually via SCP web UI (https://singlecell.broadinstitute.org/single_cell/study/SCP1162/human-colon-cancer-atlas-c295) or Terra
    - `extractor`: scp-broad
    - `field`: files
    - `reason`: SCP file listing requires bearer token; download files manually via SCP web UI (https://singlecell.broadinstitute.org/single_cell/study/SCP1162/human-colon-cancer-atlas-c295) or Terra

### OK — contrasts_valid
- **OK** no contrasts declared; nothing to check

### OK — files_in_manifest
- **OK** every sample file has a manifest entry with status=ok

### OK — files_on_disk
- **OK** all manifest files present; shallow (size) integrity ok

### OK — no_confound
- **OK** fewer than two condition levels; confound check N/A

### OK — no_orphan_raw
- **OK** no orphan files under raw/

### OK — ontology_format
- **OK** all ontology terms match expected prefix:ID pattern

### OK — sample_id_valid
- **OK** all 1 sample_ids unique and filesystem-safe
