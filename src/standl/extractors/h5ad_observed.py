"""h5ad-observed — "extract" design from a local pre-processed h5ad.

Unlike paper / repo extractors, this one operates on already-processed data
(``a.obs`` is the ground truth for cell-level metadata). It is registered in
the same registry so ``modes.meta_check`` can pick it up through the normal
``can_handle`` dispatch — and advertise via ``Source.local_h5ad``.

Policy: promote an ``obs`` column to a canonical ``PartialSample`` field only
when the column is *constant within* a single ``sample_id`` grouping. Columns
whose values vary cell-to-cell inside a sample aren't sample-level facts, so
they go nowhere (they belong to clustering / cell-type annotation, not design).
Columns with a constant value but unrecognized name land in ``extra`` verbatim.

Confidence is 0.9 for canonical-slot fields, 0.8 for ``extra`` fields, and 0.85
for top-level organism / assay sourced from ``uns``. Manual entry outranks all
of these, so conflicts during merge favor the hand-authored ``design.yaml``
while surfacing the disagreement to ``audit.md``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..schema import PartialDesign, PartialSample, ProvenancedValue, Source
from .base import register


# obs column aliases -> canonical PartialSample slot
_CANONICAL: dict[str, list[str]] = {
    "condition": ["condition", "treatment", "group"],
    "batch": ["batch", "batch_id"],
    "donor_id": ["donor_id", "donor", "subject", "subject_id",
                 "patient", "patient_id", "individual", "individual_id"],
    "tissue": ["tissue", "tissue_type"],
    "cell_type": ["cell_type", "celltype", "cellType"],
    "disease": ["disease", "disease_state"],
    "age": ["age"],
    "timepoint": ["timepoint", "time_point", "time", "day"],
    "replicate": ["replicate", "rep"],
    "tissue_ontology": ["tissue_ontology_term_id", "tissue_ontology"],
    "accession": ["accession", "gsm_id", "GSM_id"],
    "organism": ["organism", "species"],
}
_OBS_TO_SLOT: dict[str, str] = {
    alias.lower(): slot
    for slot, aliases in _CANONICAL.items()
    for alias in aliases
}

# obs columns the extractor uses to group cells into samples.
_SAMPLE_COLS = ["sample", "sample_id", "sampleID", "Sample"]

# uns keys checked for top-level organism / assay.
_UNS_ORGANISM = ["organism", "organism_ontology_term_id", "species"]
_UNS_ASSAY = ["assay", "assay_ontology_term_id", "technology"]


def _pv(value: str, evidence: str, confidence: float) -> ProvenancedValue[str]:
    return ProvenancedValue(
        value=value, source="h5ad-observed", confidence=confidence, evidence=evidence,
    )


class H5ADObservedExtractor:
    name = "h5ad-observed"

    def can_handle(self, source: Source) -> float:
        if source.local_h5ad is None:
            return 0.0
        if not Path(source.local_h5ad).exists():
            return 0.0
        return 0.95

    def extract(self, source: Source, cache_dir: Path) -> PartialDesign:
        try:
            import anndata as ad
        except ImportError:
            return PartialDesign(
                extractor=self.name,
                failures={"import": "anndata not installed; cannot read h5ad"},
            )

        p = source.local_h5ad
        if p is None or not Path(p).exists():
            return PartialDesign(
                extractor=self.name,
                failures={"source": "local_h5ad not set or file missing"},
            )

        a = ad.read_h5ad(p)

        sample_col = next((c for c in _SAMPLE_COLS if c in a.obs.columns), None)
        if sample_col is None:
            return PartialDesign(
                extractor=self.name,
                failures={"samples": f"no sample column among {_SAMPLE_COLS}"},
            )

        samples: list[PartialSample] = []
        for sid, sub in a.obs.groupby(sample_col, observed=True):
            sample = PartialSample(sample_id=str(sid))
            for col in sub.columns:
                if col == sample_col:
                    continue
                vals = sub[col].dropna().unique()
                if len(vals) != 1:
                    continue  # not sample-level constant → skip
                value = str(vals[0])
                slot = _OBS_TO_SLOT.get(col.lower())
                confidence = 0.9 if slot else 0.8
                pv = _pv(value, f"obs[{col!r}]", confidence)
                if slot:
                    setattr(sample, slot, pv)
                else:
                    sample.extra[col] = pv
            samples.append(sample)

        organism_pv = _first_uns(a.uns, _UNS_ORGANISM, confidence=0.85)
        assay_pv = _first_uns(a.uns, _UNS_ASSAY, confidence=0.85)

        return PartialDesign(
            extractor=self.name,
            samples=samples,
            organism=organism_pv,
            assay=assay_pv,
        )


def _first_uns(uns: Any, keys: list[str], confidence: float) -> ProvenancedValue[str] | None:
    for k in keys:
        if k in uns:
            return _pv(str(uns[k]), f"uns[{k!r}]", confidence)
    return None


register(H5ADObservedExtractor())
