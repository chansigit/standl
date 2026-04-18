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

Given a GEO accession, `standl run` fetches the SOFT family file, extracts
a per-sample skeleton, downloads each sample's supplementary files, and
writes an audit:

```bash
standl run GSE96583 -o /tmp/gse96583
```

~76 MB, 10 files, ~6 s on a warm pipe. Produces `design.yaml` +
`manifest.json` + `provenance.json` + `raw/` + `audit.md` with every check
green.

`geo-soft` does not guess `condition` / `donor_id` / `contrasts` from
characteristics free-text — that's a human step. Hand-edit `design.yaml`
(see `skills/standl/SKILL.md`), then re-run `standl validate` to surface
issues like perfect confounds. Worked demo: `examples/gse96583/`.

## Status

MVP complete (roadmap steps 1–3 + 5; step 4 intentionally dropped in favor
of a Claude-Code skill — see `skills/standl/SKILL.md`). 79 tests passing.

See `docs/roadmap.md` for the implementation plan and `docs/design-schema.md`
for the authoritative `design.yaml` contract.
