# GSE234903 — full stan* chain demo

Fifth and most complete demo under `examples/`. Walks the pipeline
**end-to-end through four of the five stan* tools**:

```
standl run  →  stanobj  →  stangene  →  standl validate --h5ad
                  │            │
                  └── (+ stancounts round-trip demo on a synthesized
                        log1p copy of the harmonized output)
```

Builds on `gse234903_full_pipeline/` (standl → stanobj only); this demo
adds **stangene** gene-identifier harmonization per sample and exercises
**stancounts** as a downstream round-trip verification.

## What each tool did

- **standl run GSE234903** (see `../gse234903_skill_demo/`): geo-soft
  pulls 5 CellRanger raw_feature.h5 files, ~65 MB. Audit of the dataset
  dir alone is ok / warn (the 2 structural confounds).

- **stanobj** (per sample, ~5 s each): ``10x_h5`` format → canonical
  h5ad with ``X = int32 counts``, ``layers['counts']``,
  canonicalised obs (``cell_id``, ``dataset``).

- **stangene harmonize --species human** (per sample, ~11 s each):
  36,601 Ensembl features →
    - 23,162 exact HGNC symbol (63.3%)
    - 837 previous_symbol rescues (2.3%)
    - 190 alias_symbol rescues (0.5%)
    - 11 ambiguous many-to-one conflicts
    - 12,401 unmapped (mostly non-coding / pseudogenes /
      not-in-HGNC Ensembl accessions)
  Enriched var with `gene_symbol_harmonized`, `status`, `mapping_tier`.
  Report copied into this directory as `stangene_report.md`.

- **concat + standl validate --h5ad** (wall-time ~30 s): 1,766,936 cells
  × 36,601 genes combined, obs stamped with
  ``sample`` / ``condition`` / ``batch`` / ``donor_id`` / ``tissue`` /
  ``disease`` / ``accession`` from ``design.yaml``.
  Final audit: worst severity **warn** (ok=8 warn=2 fail=0) — identical
  result to the pre-stangene pipeline, demonstrating that harmonization
  is behaviour-preserving from standl's perspective.

- **stancounts round-trip** (separate demo, not in the chain):
  Took the stangene-harmonized h5ad → subset to 2000 cells with
  lib_size > 500 → synthesized log1p normalization
  (``log1p(X / lib * 1e4)``) → fed through
  ``stancounts.reverse_log1p_anndata``. Result:

      [stancounts] original sum: 2233834  recovered sum: 2233834
      [stancounts] nonzero entries (orig / rec): 579038 / 579038
      [stancounts] entry-level mismatches: 0  worst |Δ|: 0
      [stancounts] EXACT round-trip across all non-zero entries

  Every one of 579,038 non-zero counts recovered bitwise from the
  log1p-normalized copy. The standl raw path always produces integer
  counts, so stancounts doesn't apply as a forward chain step — but it
  validates that if a user arrives at stan* via a log-normalized h5ad
  (not uncommon from published CELLxGENE datasets), the counts layer
  can be recovered losslessly.

## Scripts

Both committed under `scripts/`:

- `standl_stan_chain.py <dataset_dir> [--species human]` — drives
  standl-produced dataset dir through stanobj (subprocess) and then
  stangene (subprocess), concats, revalidates.
- `demo_stancounts_roundtrip.py <harmonized.h5ad> [--n-cells 2000]` —
  pure round-trip verification; doesn't touch the main pipeline.

## Artifacts committed

| file | source |
|---|---|
| `audit.md` | final `standl validate --h5ad` on the combined chain output |
| `pipeline_summary.json` | `standl_stan_chain.py` stdout |
| `stangene_report.md` | one sample's stangene report (all 5 samples produced identical tier counts because CellRanger uses a single reference) |

`raw/`, `stanobj/*.h5ad`, `stangene/*_harmonized.h5ad`,
`combined.h5ad` are all regenerable (~several GB total);
`scripts/standl_stan_chain.py` reproduces them from design.yaml alone.

## Running locally

```bash
# Starts from the hand-edited design.yaml in gse234903_skill_demo/.
standl run GSE234903 -o /tmp/gse234903
cp examples/gse234903_skill_demo/design.yaml /tmp/gse234903/design.yaml

# Chain standl → stanobj → stangene + final validate.
python scripts/standl_stan_chain.py /tmp/gse234903 --species human
# Wall time ~2 min (5 × stanobj ~5s, 5 × stangene ~11s, concat + validate ~30s).

# Optional: stancounts round-trip on one harmonized sample.
python scripts/demo_stancounts_roundtrip.py \
    /tmp/gse234903/standardized_chain/stangene/GSM7476348_harmonized.h5ad
```

## Why stanhue isn't in the chain

Per the pipeline diagram, stanhue is a plotting / visualisation tool;
its "upstream contract" is a fully-analysed AnnData with UMAP
embeddings, cluster labels, etc. — an analysis step beyond both the
standl and the stan* standardisation boundary. Exercising the chain
through stanhue would require running a full clustering / annotation
pipeline here, which is outside standl's scope.
