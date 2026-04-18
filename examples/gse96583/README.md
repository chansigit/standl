# GSE96583 — end-to-end demo

Reference output of `standl run` + hand-edit + `standl validate` on
**Kang et al. 2018**, *Multiplexing droplet-based single cell RNA-sequencing
using natural genetic variation* (`10.1038/nbt.4042`).

Five 10x libraries, ~76 MB raw. The five artifacts below are what a full
pipeline run produces, minus `raw/` (regenerable from `manifest.json`).

| file | produced by |
|---|---|
| `design.yaml` | `geo-soft` auto-fill → hand-edit per `skills/standl/SKILL.md` (added factors, contrasts, promoted `stimulation` → `condition`, added donor/batch) |
| `manifest.json` | `modes.run` — one entry per supplementary file, sha256 computed during download |
| `provenance.json` | merger sidecar — every `PV` shows source = `geo-soft`, confidence 0.9–0.95 |
| `audit.md` | `modes.validate` — worst severity `warn` |

## The warn is intentional

Look at `audit.md`:

    WARN — no_confound
      condition is perfectly confounded with 'donor_id'

At the library level (the unit `standl` tracks), every GSM in this study
maps to exactly one condition. The paper resolves this by *demultiplexing
cells* (SNP-based) and treating demuxed donor × stimulation as the unit of
analysis. That work belongs downstream of `standl` — the audit surfaces
the confound so it isn't forgotten.

## Reproduce

```bash
standl run GSE96583 -o /tmp/gse96583
# (runs geo-soft → downloads 10 files ~76 MB from NCBI → writes audit.md)

# Hand-edit: read the paper, add factors / contrasts / condition, save.
$EDITOR /tmp/gse96583/design.yaml

standl validate /tmp/gse96583
```

See `../../scripts/demo_gse96583.sh` for a scripted version.
