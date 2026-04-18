# GSE234903 — paper skill demo

End-to-end walk of `skills/standl/SKILL.md`'s human-in-the-loop paper
extraction flow on a study Claude Code had not seen before. Unlike the
other two demos (`gse96583/` happy path, `gse149689/` rescue path), the
experimental design here comes from **reading the paper**, not from prior
knowledge.

**Paper:** Ito et al. 2025, *Neutrophil gene expression in COVID-19
patients with acute respiratory distress syndrome*, Frontiers in
Immunology. DOI: [10.3389/fimmu.2025.1620745][doi],
PMC: [PMC12631193][pmc].

[doi]: https://doi.org/10.3389/fimmu.2025.1620745
[pmc]: https://pmc.ncbi.nlm.nih.gov/articles/PMC12631193/

## The flow, as executed

1. **`standl run GSE234903 -o /tmp/gse234903`** — 5 samples, 5 CellRanger
   h5 files, ~65 MB total, ~9 s wall. Initial audit is `ok` because
   every structural check passes, but the CLI hints:

       hint: design.yaml has no factors / contrasts / condition —
             hand-edit per skills/standl/SKILL.md, then `standl validate`

2. **WebFetch the paper** — `PMC12631193`, the PMC mirror of the
   Frontiers HTML. Extracted:
   - 5 COVID ARDS patients (+ 6 healthy controls, but those are
     bulk-RNA-seq and live in a different accession).
   - Primary clinical contrast: 28-day ventilator weaning (**VW**) vs
     non-weaning (**NVW**). Paper states *"patient cases 1 and 2 [VW],
     patient cases 3, 4, 5 [NVW]"* — directly maps onto GSM7476348..52
     in accession order.
   - Assay: 10x Chromium Next GEM v2 (sequencer DNBSEQ-G400, but that's
     the platform not the library prep; `geo-soft` had mis-assigned the
     sequencer label to `assay`).
   - Sample source: neutrophil-enriched fraction from peripheral blood.
   - Total cells post-QC: ~35,759.
   - Samples collected May–October 2021 at a single institution → batch
     ≈ collection quarter.

3. **Hand-edit `design.yaml`** — filled in per-sample `donor_id`,
   `condition`, `batch`, `disease`, `tissue_ontology` (UBERON:0000178);
   added `factors` (condition, batch) and the `NVW_vs_VW` contrast with
   Fig 2 as evidence; overrode `assay` from "DNBSEQ-G400 (Homo sapiens)"
   to the correct 10x chemistry. Extraction notes record the
   paper-cross-reference provenance (including a mismatch the paper
   has: its Data Availability cites `GSE234904` but the scRNA samples
   live here at `GSE234903` — flagged, not papered over).

4. **`standl validate /tmp/gse234903`** — final audit:

       Worst severity: **warn** | Records: 8 (ok=6 warn=2 fail=0)

   The two WARNs are the real scientific issues, correctly flagged:
   - `condition ↔ donor_id` perfectly confounded — each patient is either
     VW or NVW by definition; n=5 scRNA libraries means any condition
     effect is indistinguishable from donor effect at the library level.
     The paper sidesteps this by treating single cells within a
     library as the replication unit — that's a downstream / stanobj
     concern.
   - `condition ↔ batch` perfectly confounded — the paper collected VW
     in spring 2021 and NVW in summer–autumn. Temporal confound is real
     and only somewhat mitigatable (paper does not, as far as I read, cite
     a batch variable explicitly).

   Neither is a `fail`. standl's job is to surface them, not judge.

## What I did NOT fabricate

- Patient-level age / sex / comorbidities are in the paper's **Table 2**
  but WebFetch returned only the header, not the body. I could WebFetch
  the PDF for those, or leave the slots empty — chose the latter to
  keep this demo about the flow, not exhaustive metadata enrichment.
- Specific date each sample was collected — paper gives a range, not
  per-patient dates. `batch` assigned by coarse quarter is a judgment
  call, noted in `extraction.notes`.

## Artifacts committed

| file | produced by |
|---|---|
| `design.yaml` | `standl run` + hand-edit (factors, contrasts, condition, donor, batch, tissue_ontology, assay override) |
| `manifest.json` | `standl run` — 5 files, ~65 MB, each with sha256 + size |
| `provenance.json` | merger sidecar; all `PV` source = `geo-soft` |
| `audit.md` | final `standl validate` — worst severity `warn` |

`raw/` not committed (~65 MB). Regenerable from `manifest.json` via
`standl run GSE234903 -o <dir>`.

## Reproduce

```bash
standl run GSE234903 -o /tmp/gse234903
# Edit design.yaml per the paper (see this README's commits for what to add).
cp examples/gse234903_skill_demo/design.yaml /tmp/gse234903/design.yaml
standl validate /tmp/gse234903
```
