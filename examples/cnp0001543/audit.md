# Audit: CNP0001543

Worst severity: **warn** | Records: 8 (ok=7 warn=1 fail=0)
Generated: 2026-04-19T05:18:55.626566+00:00

## Results

### WARN — extractor_partial_failure
- **WARN** 'cngbdb' could not extract 'files': CNGBdb/CNSA file URLs live under https://ftp.cngb.org/pub/CNSA/data<N>/CNP0001543/ where the data<N> shard (data1..data9) is only exposed via the project page's JavaScript. Browse to https://db.cngb.org/search/project/CNP0001543/ manually to find the shard, then fetch the FTP listing.
    - `extractor`: cngbdb
    - `field`: files
    - `reason`: CNGBdb/CNSA file URLs live under https://ftp.cngb.org/pub/CNSA/data<N>/CNP0001543/ where the data<N> shard (data1..data9) is only exposed via the project page's JavaScript. Browse to https://db.cngb.org/search/project/CNP0001543/ manually to find the shard, then fetch the FTP listing.

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
