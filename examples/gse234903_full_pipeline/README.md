# GSE234903 — full pipeline demo (standl → stanobj → standl validate)

Fourth demo under `examples/`. Where the others stop at `standl validate`
on the dataset dir, this one closes the loop with the downstream
**stanobj** tool: per-sample CellRanger `.h5` files are standardized into
canonical `.h5ad`, concatenated, and fed back to
`standl validate --h5ad` to verify the design ↔ data handshake on the
standardized object.

This is the first worked demonstration that the **standl → stanobj**
contract actually holds on real data.

## Flow

```
GSE234903/
  design.yaml              (hand-edited per Ito et al. 2025 — see gse234903_skill_demo/)
  manifest.json
  raw/GSM*/..._raw_feature.h5
       │
       │  scripts/standl_to_stanobj.py
       │    for each sample in design.samples:
       │      stanobj <raw.h5> -o standardized/<GSM>.h5ad
       │      stamp obs['sample', 'condition', 'batch', 'donor_id', 'tissue', 'disease']
       │    concat → standardized/combined.h5ad
       │    standl validate <dataset_dir> --h5ad standardized/combined.h5ad
       ▼
standardized/
  GSM*.h5ad                       (~35-92 MB each; per-sample canonical)
  GSM*_report.json                 (stanobj's conversion report)
  GSM*_audit.log                   (stanobj's human-readable audit)
  combined.h5ad                    (~243 MB; 1,766,936 cells × 36,601 genes)
  pipeline_summary.json
```

## Result

`standl validate --h5ad` on the combined object:

    Worst severity: **warn** | Records: 10 (ok=8 warn=2 fail=0)

**8 OK** — including the two checks that only activate with `--h5ad`:
`h5ad_samples_match` (obs['sample'] unique set == design.samples) and
`h5ad_cell_count`.

**2 WARN** — the same real confounds flagged in `gse234903_skill_demo/`:
condition ↔ donor_id, condition ↔ batch. Nothing new surfaced by the
downstream step.

## Per-sample outputs (provenance snapshot)

Each stanobj h5ad carries a `uns['stanobj']` record like:

```json
{
  "source_format": "10x_h5",
  "x_contents": "counts",
  "matrix_type": "counts",
  "transposed": true,
  "var_name_strategy": "make_unique",
  "warnings": "['Matrix is nearly empty (sparsity 99.9498%)']"
}
```

The sparsity warning is GEO curation quality — the uploader shipped
CellRanger's `raw_feature_bc_matrix.h5` (pre-cell-calling) rather than
the filtered version. stanobj converts it faithfully; downstream cell-
calling (e.g. EmptyDrops) is a `stangene` / analysis-stage concern, not
standl's or stanobj's.

See `GSM7476348_report.json` and `GSM7476348_audit.log` in this directory
for a sample's full stanobj output.

## Running locally

```bash
# Produce the dataset dir (or use examples/gse234903_skill_demo/design.yaml):
standl run GSE234903 -o /tmp/gse234903
cp examples/gse234903_skill_demo/design.yaml /tmp/gse234903/design.yaml

# Drive the pipeline (assumes stanobj at the default path):
python scripts/standl_to_stanobj.py /tmp/gse234903

# Inspect:
cat /tmp/gse234903/standardized/pipeline_summary.json
cat /tmp/gse234903/audit.md   # now includes h5ad_samples_match + h5ad_cell_count
```

Wall time end-to-end: ~65 s (5 samples × ~5 s each for stanobj, ~30 s for
concat + validate). No network after `standl run`.

## What this demo validates

- **standl's `sample.files` + `obs['sample']` contract survives `stanobj`
  standardization.** stanobj preserves `uns['stanobj']` but drops obs
  columns beyond `cell_id` / `dataset`; the pipeline script re-stamps the
  design-level columns after conversion. Without that stamp,
  `standl validate --h5ad` would fail check 8 (missing obs['sample']).

- **`standl validate --h5ad` is usable on a real downstream artifact.**
  Previously the h5ad checks only ran in unit tests with tiny synthetic
  fixtures; this is the first run against a multi-sample combined
  AnnData.

- **The confound warnings are stable across the pipeline boundary.** A
  real-world weakness of the experiment doesn't appear or disappear when
  going from design-dir to standardized-h5ad — which is what you'd want
  from a good audit contract.
