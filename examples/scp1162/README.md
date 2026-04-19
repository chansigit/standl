# SCP1162 — Broad Single Cell Portal demo

Reference output of `standl run SCP1162` on **Pelka et al. 2021**,
*Spatially organized multicellular immune hubs in human colorectal cancer*
(Human Colon Cancer Atlas c295, ~371k cells, MMRd vs MMRp).

Exercises the **`scp-broad`** extractor against the public SCP search API
(`singlecell.broadinstitute.org/single_cell/api/v1/search?type=study&terms=SCP1162`).

## What's here

| file | produced by |
|---|---|
| `design.yaml` | `scp-broad` — organism, assay, tissue, disease, sex, cell_count, title, study_url all auto-filled from the search response |
| `audit.md` | `modes.validate` — worst severity `warn` |
| `manifest.json` | empty (see note below) |
| `provenance.json` | every PV sourced from `scp-broad`, confidence 0.9–1.0 |

## The warn is intentional

SCP's bulk-download endpoint (`/studies/{acc}/file_info`) is **auth-gated**;
anonymous requests get 401. The `scp-broad` extractor is therefore
**metadata-only** by design — it records a partial failure under the
`files` field, which surfaces in `audit.md`:

    WARN — extractor_partial_failure
      'scp-broad' could not extract 'files': SCP file listing requires
      bearer token; download via the SCP web UI or Terra

To get the actual count matrices, follow the `study_url` in
`design.yaml`, sign in, and use the SCP "Download" tab. Drop the files
into `raw/` and re-run `standl validate` to promote the audit to `ok`.

## Reproduce

    standl run SCP1162 -o examples/scp1162
