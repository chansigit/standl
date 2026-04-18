# standl — Download + Design Extraction for Single-Cell Datasets

The upstream entry point of the `stan*` pipeline. Given a paper URL / DOI / GEO
accession, `standl`:

1. Extracts the **experimental design** (samples, conditions, batches, contrasts)
   from the paper and associated metadata.
2. Downloads the **raw data files** referenced by that design.
3. **Cross-validates** the two: does the design expect N samples, and did we
   actually get N samples' worth of files with matching identifiers?

Outputs a self-contained dataset directory that serves as the input contract
for `stanobj` (and everything downstream).

## Pipeline position

```
standl → stanobj → stangene → stancounts → (QC/integrate/annotate) → stanhue
 下+抽+核  规格式     对基因       反归一            分析               出图
```

## Output layout

```
<dataset>/
  raw/                 # downloaded raw files (unmodified)
  paper/               # cached PDF / PMC XML / supplementary tables
  manifest.json        # file-level: url, checksum, size, status
  design.yaml          # sample-level: sample_id → condition / batch / ...
  audit.md             # design ↔ data consistency report
```

See `docs/design-schema.md` for the `design.yaml` schema and
`examples/design.example.yaml` for a filled example.

## Quickstart

```bash
# GEO series — SOFT parsing + supplementary-file download
standl run GSE96583 -o /tmp/gse96583

# CELLxGENE Discover dataset (direct h5ad download)
standl run <cxg-dataset-uuid> -o /tmp/cxg \
    # can_handle fires when passed via paper_url too:
    # standl run https://cellxgene.cziscience.com/e/<uuid>.cxg/ -o /tmp/cxg

# HCA project (Azul async fetch; contributor-generated matrices)
standl run <hca-project-uuid> -o /tmp/hca

# EBI BioStudies / ArrayExpress study (filters SRA raw formats by default)
standl run E-MTAB-10553 -o /tmp/liver
```

Each run produces `design.yaml` + `manifest.json` + `provenance.json` +
`raw/` + `audit.md`. The CLI exits ``1`` when the audit's worst severity
is ``fail`` so it slots straight into CI. A Zenodo / Figshare / BioStudies
record that only exposes raw fastq triggers a structured
``data_format`` / ``data_layout`` failure instead of silently producing
an empty dataset dir.

`geo-soft` (and every other deterministic extractor) does not guess
`condition` / `donor_id` / `contrasts` from free-text — that's a human
step. Hand-edit `design.yaml` (see `skills/standl/SKILL.md`), then re-run
`standl validate` to surface issues like perfect confounds. Worked demos
under `examples/`:

- **`gse96583/`** — happy path: GEO 10x + hand-edit + confound warning.
- **`gse149689/`** — rescue path: pooled-series matrix split into 20
  per-donor h5ad files via `scripts/demo_gse149689_rescue.py`.

## Supported sources

Seven extractors ship in v0.2:

| extractor | dispatches on |
|---|---|
| `geo-soft` | GSE/GSM/GPL/GDS accession, `ncbi.nlm.nih.gov/geo` URL |
| `cellxgene-api` | `cellxgene.cziscience.com/e/<uuid>.cxg/` URL, or UUID + `repositories=["CELLxGENE"]` |
| `hca-dcp` | `data.humancellatlas.org/explore/projects/<uuid>` URL, or UUID + `repositories=["HCA"]` |
| `biostudies` | `E-MTAB-*` / `E-GEOD-*` / `S-BIAD*` / etc. accession, `ebi.ac.uk/biostudies` URL |
| `zenodo` | `10.5281/zenodo.<id>` DOI, `zenodo.org/records/<id>` URL |
| `figshare` | `10.6084/m9.figshare.<id>[.v<N>]` DOI, `figshare.com/articles/.../<id>` URL |
| `h5ad-observed` | local processed h5ad (used by `standl meta-check`) |

Controlled-access repos (dbGaP, EGA, Synapse) are intentionally out of
scope — each needs auth plumbing that doesn't fit the zero-credential
CLI. Paper extraction (reading Methods / Tables / Figures to fill in
`condition` / `contrasts`) is handled by Claude Code directly via the
skill rather than by a programmatic `llm-paper` extractor — fewer moving
parts, same output artifacts.

## Status

MVP complete + polished. 151 tests passing. Registered extractors: 7.

See `docs/roadmap.md` for the implementation plan and
`docs/design-schema.md` for the authoritative `design.yaml` contract.
