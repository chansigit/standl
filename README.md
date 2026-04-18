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

## Status

Skeleton. Schema-first: the `design.yaml` contract is the stable API; paper
extraction and downloader backends are pluggable behind it.

See `docs/roadmap.md` for the implementation plan and `skills/standl/SKILL.md`
for the skill contract.
