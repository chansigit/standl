---
name: standl
description: >
  Use when the user asks to "download single-cell data", "fetch GEO dataset",
  "get data from a paper", "extract experimental design", "parse study metadata",
  "figure out what samples a paper has", or gives a DOI / PMC URL / bioRxiv URL /
  GEO (GSE/GSM) / ArrayExpress / CELLxGENE / HCA accession and expects raw files
  + a structured design. standl is the upstream entry of the stan* pipeline and
  produces design.yaml + manifest.json + raw/ for stanobj to consume. Do NOT use
  for format conversion (that is stanobj), gene harmonization (stangene), count
  recovery (stancounts), or plotting (stanhue).
version: 0.1.0
allowed-tools: [Bash, Read, Write, Edit, Glob, Grep, WebFetch]
---

# standl — Download + Design Extraction

Given a paper or accession, produce a self-contained dataset directory:

```
<dataset>/
  raw/              # raw files, unmodified
  paper/            # PDF / PMC XML / supplementary cache
  manifest.json     # file-level provenance
  design.yaml       # sample-level experimental design
  audit.md          # design ↔ data consistency report
```

## Principles

- **Schema first.** `design.yaml` is the stable contract. See
  `docs/design-schema.md` (in repo) for the authoritative schema.
- **Closed loop.** Design extraction tells us what files *should* exist;
  downloading tells us what files *do* exist; validation reconciles the two.
  Never skip the reconciliation step — it is the whole point of this tool.
- **No silent failures.** Inconsistencies go to `audit.md` with severity,
  not exceptions. Downstream tools read `audit.md` to decide whether to run.
- **Offline after `standl`.** This is the only stage that touches the network.
  Downstream stan* tools must be able to run fully offline from the output dir.

## Three modes

| command | when to use |
|---|---|
| `standl run <source> -o <dir>` | Starting fresh: extract design + download + validate |
| `standl validate <dir> [--h5ad X]` | Files already downloaded; reconcile with design.yaml |
| `standl meta-check <dir> [--paper URL]` | h5ad already processed; only verify paper/metadata claims — no downloads |

The third mode covers the "data's here, just tell me if the labels are right"
case. It re-runs extractors on the paper, reads ``obs`` from the h5ad, and
diffs them into ``audit.md``. Read-only by default.

## Extractors are pluggable, not hardcoded

The core never says "if GSE, use GEO parser". Each extractor implements
``can_handle(source) -> float`` and self-rates. The merger runs *every*
extractor above threshold and reconciles per-field, so GEO format drift
degrades one extractor's confidence instead of breaking the pipeline.

Known extractors (schemas first, implementations stubbed):
- ``geo-soft`` — deterministic facts from SOFT/MINiML/series-matrix
- ``llm-paper`` — condition/batch/contrast from PDF/PMC XML

Add a new source: drop a module under ``src/standl/extractors/`` with a
class satisfying the ``DesignExtractor`` protocol, call ``register(...)``.
Import it from ``extractors/__init__.py``. No core changes needed.

## When to pick paper-first vs data-first

- **Paper-first** (DOI / PMC / bioRxiv URL): extract first to discover which
  accessions and files to pull.
- **Data-first** (GSE / accession only): pull metadata first (SOFT/MINiML),
  cross-reference to the paper, then decide whether raw or processed files
  are needed.

## Cross-validation checklist

When producing `audit.md`, check:

1. Every `sample.files[*]` resolves to an `ok` entry in `manifest.json`.
2. Paper-stated sample count matches `len(design.samples)`.
3. Paper-stated cell count is within tolerance of data (if h5ad available).
4. `sample_id` values unique and filesystem-safe.
5. Contrast-referenced factors/levels exist.
6. `condition` not perfectly confounded with `batch` / `donor_id` (warn).
7. Ontology terms resolve (CL / UBERON / EFO) when present.

Each item: `ok` / `warn` / `fail` with evidence.
