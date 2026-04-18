"""GEO SOFT / MINiML / series-matrix extractor.

GEO's format is a moving target — supplementary naming, characteristics keys,
and superseries vs series layout have all drifted. This extractor follows a
**wide-in, narrow-out** policy:

- Try multiple sources in order: SOFT family file → MINiML XML → series matrix
  header → supplementary README. First hit wins per-field.
- Only emit fields from the canonical schema. Raw oddities go into
  ``Sample.extra`` verbatim — never invent new top-level fields.
- Don't guess condition/batch from characteristics free-text. That's the
  LLM extractor's job. This extractor's value is *deterministic, verifiable
  facts*: accessions, titles, characteristics key/value pairs as-given.

When a field is missing (source changed, key renamed), record ``(field, reason)``
in ``PartialDesign.failures`` and move on. Never raise for format drift.
"""
from __future__ import annotations

from pathlib import Path

from ..schema import PartialDesign, Source
from .base import register


class GEOSoftExtractor:
    name = "geo-soft"

    def can_handle(self, source: Source) -> float:
        score = 0.0
        for acc in source.accessions:
            if acc.startswith(("GSE", "GSM", "GPL", "GDS")):
                score = max(score, 0.9)
        if "GEO" in source.repositories:
            score = max(score, 0.8)
        if source.paper_url and "ncbi.nlm.nih.gov/geo" in source.paper_url:
            score = max(score, 0.7)
        return score

    def extract(self, source: Source, cache_dir: Path) -> PartialDesign:
        # TODO(impl): fetch SOFT family file to cache_dir, fall back to MINiML,
        # fall back to series matrix header. Populate PartialDesign with
        # accession-level facts (title, characteristics, platform).
        # Put anything non-canonical into Sample.extra.
        raise NotImplementedError("geo-soft extractor not yet implemented")


register(GEOSoftExtractor())
