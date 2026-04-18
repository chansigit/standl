"""Base contract + registry for design extractors.

Design principle: extractors self-rate via ``can_handle(source) -> float``
instead of the core dispatching by accession pattern. Multiple extractors
may fire for the same source (e.g. GEO SOFT parser + LLM paper reader for
a GSE accession with a linked PMC paper). The merger reconciles the outputs.

Extractors must be tolerant of source-format drift. If a field can't be
extracted, return ``None`` and record the reason in ``PartialDesign.failures``
rather than raising. Only raise for "this is not our kind of source at all".
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from ..schema import PartialDesign, ProvenancedValue, Source


def make_pv(
    source_name: str, default_confidence: float = 0.9,
) -> Callable[..., ProvenancedValue[Any]]:
    """Factory for an extractor-bound ``_pv(value, evidence, confidence=...)``
    helper. Every concrete extractor uses one of these; hoisting here
    eliminates the boilerplate re-definition in each module.

    >>> _pv = make_pv("geo-soft", default_confidence=0.95)
    >>> _pv("HN01", evidence="Sample_title").source
    'geo-soft'
    """
    def _pv(
        value: Any,
        evidence: str | None = None,
        confidence: float | None = None,
    ) -> ProvenancedValue[Any]:
        return ProvenancedValue(
            value=value,
            source=source_name,
            confidence=default_confidence if confidence is None else confidence,
            evidence=evidence,
        )
    return _pv


@runtime_checkable
class DesignExtractor(Protocol):
    """Contract every extractor must satisfy.

    Implementations should be stateless w.r.t. the caller — cache, network
    clients, and file handles are plumbed through ``cache_dir``.
    """

    name: str

    def can_handle(self, source: Source) -> float:
        """Return 0-1 self-rating. 0 = "not my kind of source". 1 = "authoritative"."""
        ...

    def extract(self, source: Source, cache_dir: Path) -> PartialDesign:
        """Do the extraction. Must not raise for partial failures — use ``failures`` field."""
        ...


# -------- registry --------

_REGISTRY: list[DesignExtractor] = []


def register(extractor: DesignExtractor) -> DesignExtractor:
    """Register an extractor instance. Usable as a decorator on a factory, or directly."""
    _REGISTRY.append(extractor)
    return extractor


def all_extractors() -> list[DesignExtractor]:
    return list(_REGISTRY)


def pick_extractors(source: Source, threshold: float = 0.3) -> list[tuple[DesignExtractor, float]]:
    """Return (extractor, score) pairs above threshold, sorted by score desc.

    Callers should run *all* returned extractors and feed the results into the
    merger; do not pick the top one only. Multiple weak sources often combine
    into a stronger picture than any single one.
    """
    scored = [(e, e.can_handle(source)) for e in _REGISTRY]
    return sorted(
        [(e, s) for e, s in scored if s >= threshold],
        key=lambda x: x[1],
        reverse=True,
    )
