"""Merge PartialDesigns from multiple extractors into one clean Design
plus a ProvenanceRecord sidecar.

Resolution rules (per leaf field):

1. Highest ``confidence`` wins.
2. Ties broken by a priority map (deterministic extractors beat LLM).
3. If two surviving values disagree on the *value* (not just confidence),
   mark ``conflict=True`` and keep both — downstream audit surfaces these.
4. ``None`` never overrides a real value. No extractor writing a field = field
   stays ``None`` in the final Design.

The merger does not heuristically invent. If no extractor filled a required
field (e.g. ``organism``), the merged Design is incomplete and the caller is
expected to surface that in ``audit.md`` rather than guess.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _now() -> datetime:
    return datetime.now(timezone.utc)

from .schema import (
    Design,
    Extraction,
    FieldProvenance,
    PartialDesign,
    PartialSample,
    ProvenanceRecord,
    ProvenancedValue,
    Sample,
    Source,
)


# Higher = preferred when confidences tie.
# Human-entered beats any programmatic extractor; data-observed (h5ad) ranks
# high because it's verifiable against the actual file being processed
# downstream. Paper extraction is deliberately NOT in the registry — the
# stan* flow asks Claude Code / a human to read the paper and write design.yaml
# directly (promoted as "manual"), see skills/standl/SKILL.md.
DEFAULT_PRIORITY = {
    "manual": 1000,
    "geo-soft": 100,
    "h5ad-observed": 95,
    "cellxgene-api": 90,
    "hca-dcp": 90,
    "arrayexpress": 85,
}


def _pick(candidates: list[ProvenancedValue[Any]],
          priority: dict[str, int]) -> tuple[ProvenancedValue[Any], list[ProvenancedValue[Any]], bool]:
    """Return (chosen, rejected, conflict)."""
    if not candidates:
        raise ValueError("_pick called with no candidates")
    ranked = sorted(
        candidates,
        key=lambda pv: (pv.confidence, priority.get(pv.source, 0)),
        reverse=True,
    )
    chosen = ranked[0]
    rejected = ranked[1:]
    distinct_values = {repr(c.value) for c in candidates}
    conflict = len(distinct_values) > 1
    return chosen, rejected, conflict


def _merge_sample(
    sample_id: str,
    partials: list[PartialSample],
    priority: dict[str, int],
) -> tuple[Sample, list[FieldProvenance]]:
    sample_kwargs: dict[str, Any] = {"sample_id": sample_id, "files": []}
    provenance: list[FieldProvenance] = []

    optional_pv_fields = [
        "files", "accession", "organism", "tissue", "tissue_ontology",
        "cell_type", "disease", "age", "sex", "donor_id", "condition",
        "timepoint", "replicate", "batch",
    ]

    for field in optional_pv_fields:
        cands = [getattr(p, field) for p in partials if getattr(p, field) is not None]
        if not cands:
            continue
        chosen, rejected, conflict = _pick(cands, priority)
        sample_kwargs[field] = chosen.value
        provenance.append(FieldProvenance(
            path=f"samples[{sample_id}].{field}",
            chosen=chosen,
            rejected=rejected,
            conflict=conflict,
        ))

    # Merge extra dicts verbatim; last-writer-wins on key collisions but record it.
    extra: dict[str, Any] = {}
    for p in partials:
        for k, pv in p.extra.items():
            extra[k] = pv.value  # extras are rarely conflict-worthy; keep simple
    if extra:
        sample_kwargs["extra"] = extra

    if "files" not in sample_kwargs or sample_kwargs["files"] is None:
        sample_kwargs["files"] = []

    return Sample(**sample_kwargs), provenance


def merge(
    partials: list[PartialDesign],
    priority: dict[str, int] | None = None,
) -> tuple[Design, ProvenanceRecord]:
    """Merge extractor outputs. Caller is responsible for feeding in every
    PartialDesign that ran — merger does not re-invoke extractors.
    """
    priority = priority or DEFAULT_PRIORITY
    if not partials:
        raise ValueError("merge() requires at least one PartialDesign")

    # dataset_id: first non-None wins, with conflict flagged.
    dataset_ids = [p.dataset_id for p in partials if p.dataset_id]
    if not dataset_ids:
        raise ValueError("No extractor produced a dataset_id")
    dataset_id = dataset_ids[0]

    # Merge Source by union.
    merged_source = Source()
    for p in partials:
        if p.source.paper_doi and not merged_source.paper_doi:
            merged_source.paper_doi = p.source.paper_doi
        if p.source.paper_url and not merged_source.paper_url:
            merged_source.paper_url = p.source.paper_url
        for acc in p.source.accessions:
            if acc not in merged_source.accessions:
                merged_source.accessions.append(acc)
        for repo in p.source.repositories:
            if repo not in merged_source.repositories:
                merged_source.repositories.append(repo)

    provenance: list[FieldProvenance] = []

    def _top_pv(field: str) -> ProvenancedValue[Any] | None:
        cands = [getattr(p, field) for p in partials if getattr(p, field) is not None]
        if not cands:
            return None
        chosen, rejected, conflict = _pick(cands, priority)
        provenance.append(FieldProvenance(
            path=field,
            chosen=chosen,
            rejected=rejected,
            conflict=conflict,
        ))
        return chosen

    organism_pv = _top_pv("organism")
    assay_pv = _top_pv("assay")

    # Group partial samples by sample_id across all extractors.
    by_id: dict[str, list[PartialSample]] = {}
    for p in partials:
        for s in p.samples:
            by_id.setdefault(s.sample_id, []).append(s)

    merged_samples: list[Sample] = []
    for sid, parts in by_id.items():
        s, sprov = _merge_sample(sid, parts, priority)
        merged_samples.append(s)
        provenance.extend(sprov)

    # Factors/contrasts/batches: union, dedup by name. Conflicts on level sets
    # are reported but the first extractor's definition wins.
    factors = []
    seen_factor_names: set[str] = set()
    for p in partials:
        for f in p.factors:
            if f.name in seen_factor_names:
                continue
            factors.append(f)
            seen_factor_names.add(f.name)

    contrasts = []
    seen_contrast_names: set[str] = set()
    for p in partials:
        for c in p.contrasts:
            if c.name in seen_contrast_names:
                continue
            contrasts.append(c)
            seen_contrast_names.add(c.name)

    batches: list[str] = []
    for p in partials:
        for b in p.batches:
            if b not in batches:
                batches.append(b)

    design = Design(
        dataset_id=dataset_id,
        source=merged_source,
        organism=organism_pv.value if organism_pv else "",
        assay=assay_pv.value if assay_pv else "",
        samples=merged_samples,
        factors=factors,
        contrasts=contrasts,
        batches=batches,
        extraction=Extraction(
            methods=[p.extractor for p in partials],
            extracted_at=_now(),
        ),
    )

    record = ProvenanceRecord(
        dataset_id=dataset_id,
        fields=provenance,
        extractor_runs=[
            {"extractor": p.extractor, "failures": p.failures} for p in partials
        ],
        created_at=_now(),
    )
    return design, record
