# `design.yaml` schema

The canonical contract produced by `standl` and consumed by `stanobj` and
everything downstream. Schema is stable; extraction backends (LLM, regex,
GEO SOFT parser) are swappable behind it.

## Top-level

| field | type | required | notes |
|---|---|---|---|
| `dataset_id` | str | yes | Stable slug, e.g. `GSE123456` or `biorxiv-2024-xyz` |
| `source` | object | yes | See [Source](#source) |
| `organism` | str | yes | NCBI taxon name, e.g. `Homo sapiens` |
| `assay` | str | yes | e.g. `10x Chromium v3`, `Smart-seq2`, `snRNA-seq` |
| `samples` | list[Sample] | yes | See [Sample](#sample) |
| `factors` | list[Factor] | no | Declared experimental factors + levels |
| `contrasts` | list[Contrast] | no | Comparisons the paper actually makes |
| `batches` | list[str] | no | Names of columns that capture batch structure |
| `notes` | str | no | Free-form caveats |
| `extraction` | object | yes | Provenance: how this file was produced |

## Source

| field | type | notes |
|---|---|---|
| `paper_doi` | str | DOI if known |
| `paper_url` | str | PMC / bioRxiv / publisher URL |
| `accessions` | list[str] | e.g. `[GSE123456, SRP000000]` |
| `repositories` | list[str] | `GEO`, `ArrayExpress`, `CELLxGENE`, `HCA`, `Zenodo` |

## Sample

A **sample** is one biological unit that maps to one or more files in
`manifest.json`. Typically: one library / one 10x run.

| field | type | required | notes |
|---|---|---|---|
| `sample_id` | str | yes | Must match `obs['sample']` in final h5ad |
| `accession` | str | no | e.g. `GSM1234567` |
| `files` | list[str] | yes | Relative paths under `raw/`; links to `manifest.json` entries |
| `organism` | str | no | Overrides top-level if heterogeneous |
| `tissue` | str | no | Prefer UBERON term |
| `tissue_ontology` | str | no | e.g. `UBERON:0002107` |
| `cell_type` | str | no | Prefer CL term (if sample is pre-sorted) |
| `disease` | str | no | Prefer EFO/MONDO term |
| `age` | str | no | Raw as reported |
| `sex` | str | no | `male` / `female` / `unknown` |
| `donor_id` | str | no | For multi-sample-per-donor designs |
| `condition` | str | no | The primary factor level, e.g. `treated` |
| `timepoint` | str | no | e.g. `day7` |
| `replicate` | str | no | e.g. `rep1` |
| `batch` | str | no | Processing batch if known |
| `extra` | dict | no | Anything else extracted verbatim |

## Factor

```yaml
- name: treatment
  levels: [control, drug_A, drug_B]
  reference: control
```

## Contrast

```yaml
- name: drug_A_vs_control
  numerator: {treatment: drug_A}
  denominator: {treatment: control}
  source: "Figure 2a"
```

## Extraction provenance

Top-level `extraction` holds only the high-level extractor summary. Per-field
attribution (model, confidence, evidence pointer, conflicts) lives in the
`provenance.json` sidecar — see `schema.py::ProvenanceRecord`.

| field | notes |
|---|---|
| `methods` | list of extractor names that ran, e.g. `[geo-soft, manual]` |
| `extracted_at` | ISO timestamp |
| `notes` | optional free-form caveats |

## Validation rules (enforced by `standl validate`)

1. Every `sample.files[*]` resolves to an entry in `manifest.json` with
   `status: ok`.
2. `len(samples)` matches the count stated in the paper (if extractable).
3. `sample_id` values are unique and filesystem-safe.
4. If `contrasts` reference a factor/level, that factor/level must exist.
5. `condition` and `batch` are not perfectly confounded (warn, don't fail).
6. Ontology terms resolve (CL / UBERON / EFO) when present.

Failures land in `audit.md`, not a silent exception — the downstream pipeline
reads `audit.md` severity to decide whether to proceed.
