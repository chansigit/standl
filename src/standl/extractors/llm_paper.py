"""LLM-based paper extractor.

Reads paper PDF / PMC XML / supplementary tables and asks a model to produce
a structured ``PartialDesign``. Trades determinism for coverage: good at
condition / batch / contrast / donor_id — the stuff GEO's SOFT doesn't encode.

Principles:

- **Schema-constrained output only.** Prompt the model to emit exactly the
  ``PartialDesign`` schema. Reject/retry on validation failure.
- **Per-field evidence.** Each ``ProvenancedValue`` gets an ``evidence``
  pointer back to the paper ("Methods, paragraph 3" / "Table S2 row 4").
  Down-weight confidence when evidence is missing.
- **Don't fabricate sample_id.** If the paper uses "Donor 1 tumor" but GEO
  uses "GSM4138110", prefer the GEO id and put the paper label in extra.
  The merger re-keys by GEO accession when both are present.
- **Abstain is valid.** Lower confidence (<0.5) when the paper text is
  ambiguous. The merger then lets deterministic sources win.
"""
from __future__ import annotations

from pathlib import Path

from ..schema import PartialDesign, Source
from .base import register


class LLMPaperExtractor:
    name = "llm-paper"

    def can_handle(self, source: Source) -> float:
        if source.paper_doi or source.paper_url:
            return 0.8
        # Even with just an accession, a paper is often discoverable via GEO
        # series metadata — but that's the geo-soft extractor's job to surface
        # a paper_url. Don't fire speculatively here.
        return 0.0

    def extract(self, source: Source, cache_dir: Path) -> PartialDesign:
        # TODO(impl):
        # 1. Resolve paper -> download PDF / PMC XML / supplementary to cache_dir
        # 2. Chunk, run structured-output LLM call (tool-use w/ PartialDesign schema)
        # 3. Validate against schema, retry on fail
        # 4. Attach evidence pointers per field
        raise NotImplementedError("llm-paper extractor not yet implemented")


register(LLMPaperExtractor())
