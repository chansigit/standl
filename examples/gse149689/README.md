# GSE149689 — pooled-series rescue demo

Reference output for the rescue flow described in `skills/standl/SKILL.md`
(`Rescue flow: data_layout failure`). **Lee et al. 2020**,
*Immunophenotyping of COVID-19 and Influenza Underscores the Association of
Type I IFN Response in Severe COVID-19* (`10.1126/sciimmunol.abd1554`,
[Sci. Immunol.](https://www.science.org/doi/10.1126/sciimmunol.abd1554)).

20 COVID / flu / healthy PBMC samples, all deposited with
`!Sample_supplementary_file_1 = NONE` in their ^SAMPLE blocks — the
processed data sits at `Series_supplementary_file` as a single 490 MB
pooled 10x matrix covering 85,144 cells, with cell barcodes suffixed `-1`
through `-20` (one suffix per GSM, in `Series_sample_id` order).

`standl run` alone can't handle this pool — it surfaces a FAIL audit record
and points at the series URLs. The rescue script in
`../../scripts/demo_gse149689_rescue.py` carries out the 8-step flow from
SKILL.md and produces the clean dataset dir captured here.

## Artifacts

| file | produced by |
|---|---|
| `design.yaml` | `standl run` (geo-soft) + `rescue-split` (points `sample.files` at per-sample h5ad) |
| `manifest.json` | rescue script — `path: <GSM>/observed.h5ad`, real sha256 + size, `url: rescue://pooled-series-split\|<series URLs>` |
| `provenance.json` | merger sidecar (all `PV` source = `geo-soft`) |
| `audit.md` | final `standl validate` — worst severity `ok` after rescue |

`raw/` is **not committed** (~1.5 GB after split). Regenerate with the
rescue script.

## Reproduce

```bash
# 1) Reproduces the FAIL state (exits 1, audit flags data_layout).
standl run GSE149689 -o /tmp/gse149689

# 2) Walk the 8-step skill rescue: download series files, load pooled
#    matrix, split by barcode suffix, write 20× observed.h5ad, rewrite
#    design.yaml + manifest.json, re-validate. Needs anndata + scipy.
python scripts/demo_gse149689_rescue.py /tmp/gse149689
```

Total wall time: ~30 s (dominated by the 490 MB matrix download).

## Per-sample cell counts after split

Range 538 – 7,731 cells per GSM, total 85,144 — matches the pooled matrix
shape exactly. See the rescue script's stdout for the full table.

## What still needs a human

`design.yaml` still has no `factors`, `contrasts`, or `condition` — those
come from the paper. A follow-up hand-edit per SKILL.md would:

- Add a `condition` factor with levels `[healthy, flu, mild_covid, severe_covid]`
  (per the paper's Table 1).
- Map each `GSM` → condition via `subject_group` / `subject_status` in
  `Sample.extra` (already populated by `geo-soft`).
- Declare the contrasts the paper tests (severe vs healthy, severe vs mild,
  etc.).

That hand-edit isn't in this demo — it's downstream of the rescue.
