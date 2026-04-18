"""Pydantic models for design.yaml, provenance.json, and manifest.json.

Two shapes of Design live in this codebase:

- ``Design`` — clean, flat, what downstream stanobj/stangene read.
- ``PartialDesign`` — what a single extractor produces: every leaf field is
  wrapped in ``ProvenancedValue`` so the merger can resolve conflicts and
  attribute each fact back to its source.

The merger reads a list of ``PartialDesign`` and writes one ``Design`` plus
a side-car ``ProvenanceRecord`` (-> provenance.json).

See docs/design-schema.md for the human-readable spec.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ProvenancedValue(BaseModel, Generic[T]):
    """A value carrying where it came from and how confident we are."""
    value: T
    source: str                # extractor name, e.g. "geo-soft", "llm-paper"
    confidence: float = 1.0    # 0-1
    evidence: str | None = None  # free-form pointer (e.g. "Figure 1c caption")


# ---------- Clean Design (consumer-facing) ----------

class Source(BaseModel):
    paper_doi: str | None = None
    paper_url: str | None = None
    accessions: list[str] = Field(default_factory=list)
    repositories: list[str] = Field(default_factory=list)
    # Local data file(s) treated as a design source — e.g. a pre-processed h5ad
    # whose ``obs`` is the ground truth for sample-level metadata. Picked up by
    # the ``h5ad-observed`` extractor; serialized as a POSIX path when present.
    local_h5ad: Path | None = None


class Sample(BaseModel):
    sample_id: str
    files: list[str]
    accession: str | None = None
    organism: str | None = None
    tissue: str | None = None
    tissue_ontology: str | None = None
    cell_type: str | None = None
    disease: str | None = None
    age: str | None = None
    sex: Literal["male", "female", "unknown"] | None = None
    donor_id: str | None = None
    condition: str | None = None
    timepoint: str | None = None
    replicate: str | None = None
    batch: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class Factor(BaseModel):
    name: str
    levels: list[str]
    reference: str | None = None


class Contrast(BaseModel):
    name: str
    numerator: dict[str, str]
    denominator: dict[str, str]
    source: str | None = None


class Extraction(BaseModel):
    """Top-level extraction summary. Per-field attribution lives in provenance.json."""
    methods: list[str]                  # e.g. ["geo-soft", "llm-paper"]
    extracted_at: datetime
    notes: str | None = None


class Design(BaseModel):
    dataset_id: str
    source: Source
    organism: str
    assay: str
    samples: list[Sample]
    factors: list[Factor] = Field(default_factory=list)
    contrasts: list[Contrast] = Field(default_factory=list)
    batches: list[str] = Field(default_factory=list)
    notes: str | None = None
    extraction: Extraction


# ---------- Partial Design (extractor output) ----------

PV = ProvenancedValue  # shorthand


class PartialSample(BaseModel):
    """Like Sample, but every optional field carries provenance. sample_id is required."""
    sample_id: str
    files: PV[list[str]] | None = None
    accession: PV[str] | None = None
    organism: PV[str] | None = None
    tissue: PV[str] | None = None
    tissue_ontology: PV[str] | None = None
    cell_type: PV[str] | None = None
    disease: PV[str] | None = None
    age: PV[str] | None = None
    sex: PV[str] | None = None
    donor_id: PV[str] | None = None
    condition: PV[str] | None = None
    timepoint: PV[str] | None = None
    replicate: PV[str] | None = None
    batch: PV[str] | None = None
    extra: dict[str, PV[Any]] = Field(default_factory=dict)


class PartialDesign(BaseModel):
    """One extractor's take. Missing knowledge = None; do not invent defaults."""
    extractor: str                       # e.g. "geo-soft"
    dataset_id: str | None = None
    source: Source = Field(default_factory=Source)
    organism: PV[str] | None = None
    assay: PV[str] | None = None
    samples: list[PartialSample] = Field(default_factory=list)
    factors: list[Factor] = Field(default_factory=list)
    contrasts: list[Contrast] = Field(default_factory=list)
    batches: list[str] = Field(default_factory=list)
    notes: str | None = None
    # Fields we tried but couldn't fill — "source changed format" etc.
    failures: dict[str, str] = Field(default_factory=dict)


# ---------- Provenance sidecar ----------

class FieldProvenance(BaseModel):
    path: str                  # e.g. "samples[HN01_Tumor].tissue"
    chosen: ProvenancedValue[Any]
    rejected: list[ProvenancedValue[Any]] = Field(default_factory=list)
    conflict: bool = False


class ProvenanceRecord(BaseModel):
    dataset_id: str
    fields: list[FieldProvenance]
    extractor_runs: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime


# ---------- Manifest (download-level) ----------

class ManifestEntry(BaseModel):
    path: str
    url: str
    size_bytes: int | None = None
    sha256: str | None = None
    md5: str | None = None
    status: Literal["ok", "missing", "corrupt", "pending"] = "pending"
    downloaded_at: datetime | None = None
    source_accession: str | None = None


class Manifest(BaseModel):
    dataset_id: str
    entries: list[ManifestEntry]
    created_at: datetime
