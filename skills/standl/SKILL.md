---
name: standl
description: >
  Use when the user asks to "download single-cell data", "fetch GEO dataset",
  "get data from a paper", "extract experimental design", "parse study metadata",
  "figure out what samples a paper has", or gives a DOI / PMC URL / bioRxiv URL /
  GEO (GSE/GSM) accession and expects raw files + a structured design. standl is
  the upstream entry of the stan* pipeline and produces design.yaml +
  manifest.json + raw/ for stanobj to consume. Do NOT use for format conversion
  (that is stanobj), gene harmonization (stangene), count recovery (stancounts),
  or plotting (stanhue).
version: 0.2.0
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

## You are the paper extractor

standl ships two programmatic extractors: `geo-soft` (SOFT/MINiML parser) and
`h5ad-observed` (reads an existing processed h5ad). Neither reads papers.

When a paper needs to be read to fill in `condition`, `batch`, `donor_id`,
`contrasts`, or cell-count claims, **you do it**: WebFetch the PMC XML / PDF,
read the Methods / Tables / Figure legends, write the relevant fields into
`design.yaml` by hand. The merger treats hand-edited `design.yaml` as a
`manual` source at confidence 1.0 — the highest-priority slot — so your work
outranks every programmatic extractor on conflicts.

This replaces the deferred `llm-paper` extractor; it's the same LLM
(you), just without the intermediate RPC.

## Principles

- **Schema first.** `design.yaml` is the stable contract. See
  `docs/design-schema.md` for the authoritative schema.
- **Closed loop.** Design extraction tells us what files *should* exist;
  downloading tells us what files *do* exist; validation reconciles the two.
  Never skip the reconciliation step — it is the whole point of this tool.
- **No silent failures.** Inconsistencies go to `audit.md` with severity,
  not exceptions. Downstream tools read `audit.md` to decide whether to run.
- **Offline after `standl`.** The CLI stages that touch the network are
  `run` (downloads) and `geo-soft` (fetches SOFT). Downstream stan* tools
  run fully offline from the output dir.

## Three CLI modes

| command | when to use |
|---|---|
| `standl run <source> -o <dir>` | Starting fresh from a GEO accession: geo-soft extracts, fetch downloads, validate writes audit.md |
| `standl validate <dir> [--h5ad X]` | Files already downloaded; reconcile with design.yaml |
| `standl meta-check <dir> [--paper URL] [--h5ad X] [--write-design]` | h5ad already processed; verify paper/metadata claims — read-only by default |

## The typical flow (GEO accession with a paper)

1. **Kick off the deterministic pass.** Run `standl run GSE123456 -o datasets/GSE123456`.
   - `geo-soft` fetches `GSE123456_family.soft.gz`, extracts sample skeletons
     (GSM ids, `Sample_characteristics_ch1` → `Sample.extra`, supplementary
     URLs → `sample.files` relative paths).
   - `fetch` pulls each supplementary file to `raw/<GSM>/<basename>`.
   - `validate` writes `audit.md`.
2. **Read the paper.** Use WebFetch on the DOI / PMC URL. If the publisher
   blocks, grab the bioRxiv preprint or the PMC XML. Cache under `<dir>/paper/`.
3. **Fill in the human-only fields.** Edit `design.yaml` to add:
   - `condition` — the primary factor level per sample (e.g. `tumor`, `control`).
     GEO often has the raw string in `Sample.extra["tissue"]` already;
     cross-reference the paper's Methods to decide the canonical level name.
   - `donor_id` — collapse per-sample donor info from `Sample.extra["donor"]`
     or from paper tables.
   - `batch` — processing batch if disclosed (often in Methods' "data generation"
     section). Warn if not stated; leave `None`.
   - `timepoint`, `replicate`, `sex`, `age` — similarly.
   - `factors` + `contrasts` — the comparisons the paper actually makes
     (e.g. `Figure 2a: tumor vs PBL`). Put the figure/table ref into
     `Contrast.source`.
   - Fix `sample_id` to whatever the paper + h5ad will use downstream (often
     a human-readable label like `HN01_Tumor`, NOT the GSM id). Keep the
     GSM in `accession`.
4. **Re-validate.** `standl validate <dir>` → check `audit.md`. Common fails:
   - A `sample.files[*]` no longer matches a manifest entry because you
     renamed `sample_id` — update the `files` paths to match the new id.
   - `condition` perfectly confounded with `batch` / `donor_id` — emit a
     warning to the user; don't try to hide it.
   - Contrast references a factor/level that doesn't exist — typo or missing
     level declaration.
5. **Optional: cross-check against an existing h5ad.**
   `standl meta-check <dir> --h5ad processed.h5ad` surfaces disagreements
   between your `design.yaml` and `obs` in the h5ad.

## Paper-first flow (DOI only, accessions unknown)

If the user hands you a DOI with no GEO accession in the paper:

1. Read the paper first. Look in Methods and Data Availability for the
   accession (`GSE*`, `PRJNA*`, `E-MTAB-*`, CxG collection id, …).
2. Tell the user which accession you found; confirm before starting downloads.
3. Then run the flow above with the confirmed accession.

Papers that deposit only to controlled-access repositories (dbGaP, EGA) are
**out of scope** for standl — flag to the user.

## Rescue flow: `data_layout` failure (pooled series-level data)

When `audit.md` reports

    FAIL — extractor_partial_failure
    - 'geo-soft' could not extract 'data_layout': no sample-level supplementary
      files; data is pooled at Series_supplementary_file (N file(s))...

the dataset's processed data lives *at the series level* as a single matrix
covering all samples, split by cell-barcode suffix (GEO writes
`!Sample_supplementary_file_1 = NONE` in each ^SAMPLE block). `standl run`
won't auto-split — you rescue it:

1. **Find the series URLs.** Read `<dir>/design.yaml` — `notes` ends with
   `series_supplementary_files: <url1>; <url2>; ...`. These are the pooled
   files (typical 10x set: matrix.mtx.gz + barcodes.tsv.gz + features.tsv.gz).
2. **Download them to `<dir>/paper/`** via `standl.fetch.download`:
   ```python
   from pathlib import Path
   from standl.fetch import download
   for url in SERIES_URLS:
       download(url, Path("<dir>/paper") / url.rsplit("/", 1)[-1])
   ```
   Idempotent — re-running short-circuits on sha256.
3. **Load the pooled matrix** (scanpy / anndata) from `<dir>/paper/`.
4. **Split by barcode suffix.** GEO's convention is that cells from sample
   N end in `-N` (check the paper's Data Processing for the mapping —
   sometimes explicit "Sample 1 : AAAC...-1 ~ TTTGT...-1" in the SOFT's
   `Sample_data_processing` field). Extract the mapping and slice the
   AnnData per sample.
5. **Write per-sample h5ad (or mtx triples) under `<dir>/raw/<sample_id>/`**.
   Prefer h5ad — one file per sample matches the downstream `stanobj` flow.
6. **Rewrite `<dir>/design.yaml`**: set each sample's `files` to the newly
   written relative paths, and record the pool-split provenance in
   `notes` (`"split from <pooled URL> by barcode suffix"`).
7. **Update `<dir>/manifest.json`**: one entry per written file with
   `status: ok`, real size, and sha256 (compute via `hashlib`).
8. **Re-run `standl validate <dir>`** — audit.md should now be all `ok`.

Do not silently shim around `data_layout`. If the user asked for
`standl run GSE…` and you produced a split dataset via a rescue, say so in
the final message so they know the data isn't straight from GEO.

## Cross-validation checklist

When producing / updating `audit.md`, these nine checks are what `validate`
enforces. Your hand-edits to `design.yaml` should leave them all `ok`:

1. Every `sample.files[*]` resolves to an `ok` entry in `manifest.json`.
2. Manifest-listed files exist on disk (size match; sha256 in `--deep`).
3. No orphan files in `raw/` not referenced by any sample.
4. `sample_id` values unique and filesystem-safe (`[A-Za-z0-9_.-]`, no `..`).
5. Contrast-referenced factors / levels are declared.
6. `condition` not perfectly confounded with `batch` / `donor_id` (warn).
7. Ontology terms (CL / UBERON / EFO / MONDO) match `^PREFIX:\d+$`.
8. If h5ad given: `obs['sample']` unique values == design sample_ids.
9. If h5ad given: cell count within tolerance of `expected_cell_count`.

## When to stop and ask

- The paper's sample count doesn't match GEO's sample count → tell the user,
  don't paper over it.
- You can't resolve the paper (no PMC, no bioRxiv, paywalled PDF that
  WebFetch bounces) → ask the user to drop the PDF locally, then Read it.
- Ambiguous condition labels ("treated" vs "stimulated" vs "activated" used
  interchangeably) → propose a canonical label, confirm with the user.

## Extractors are pluggable

The core never says "if GSE, use GEO parser". Each extractor implements
`can_handle(source) -> float` and self-rates. The merger runs *every*
extractor above threshold and reconciles per-field, so GEO format drift
degrades one extractor's confidence instead of breaking the pipeline.

Registered extractors (as of v0.2.0):

| name | dispatch signal | scope |
|---|---|---|
| `geo-soft` | GSE/GSM/GPL/GDS accession, or `ncbi.nlm.nih.gov/geo` URL | GEO SOFT family file; deterministic sample metadata + per-sample supplementary URLs |
| `cellxgene-api` | `cellxgene.cziscience.com/e/<uuid>.cxg/` explorer URL, or UUID + `repositories=["CELLxGENE"]` | CZI CELLxGENE Discover curation API — one standardized h5ad per dataset |
| `hca-dcp` | `data.humancellatlas.org/explore/projects/<uuid>` URL, or UUID + `repositories=["HCA"]` | HCA Azul API; contributor-generated matrix (CGM) files, async `/fetch/repository/files/` URLs |
| `biostudies` | `E-MTAB-*` / `E-GEOD-*` / `S-BIAD*` / etc. accession, or `ebi.ac.uk/biostudies` URL | EBI BioStudies + legacy ArrayExpress; filters SRA-level raw formats |
| `zenodo` | `10.5281/zenodo.<id>` DOI, `zenodo.org/records/<id>` URL, or numeric id + `repositories=["Zenodo"]` | Generic DOI-backed data repo; metadata is free-form |
| `figshare` | `10.6084/m9.figshare.<id>[.v<N>]` DOI, `figshare.com/articles/.../<id>` URL, or numeric id + `repositories=["Figshare"]` | Same shape as Zenodo |
| `h5ad-observed` | `Source.local_h5ad` set | Treats a processed h5ad's `obs`/`uns` as the ground-truth design for ``meta-check`` |

All deterministic-source extractors (everything except `h5ad-observed`)
share the same one-sample-per-dataset pattern: the PartialSample's
``sample_id`` is the dataset id (GSM for GEO, UUID for CxG/HCA, record id
for Zenodo/Figshare, accession for BioStudies). Per-donor splits are a
skill rescue step when needed (see the pooled-series section above).

Don't re-litigate which extractor wins on conflicts — the merger's
priority map already settles that: ``manual`` (hand-edited design.yaml)
> `geo-soft` > `h5ad-observed` > `cellxgene-api` / `hca-dcp` >
`biostudies`; confidence wins first, priority breaks ties. Deterministic
repos beat free-form ones (Zenodo / Figshare) because the latter have no
structured biological vocab.

Add a new source: drop a module under `src/standl/extractors/` with a
class satisfying the `DesignExtractor` protocol, call `register(...)`.
Import it from `extractors/__init__.py`. No core changes needed.
