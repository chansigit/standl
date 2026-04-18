"""Tests for the geo-soft extractor.

Design: feed the extractor a ``cache_dir`` that already contains a hand-
authored ``{GSE}_family.soft`` (or .gz). That bypasses the network fetch path.
One test monkeypatches ``_fetch_soft`` to simulate the "no network, no cache"
case and verifies we record a failure instead of raising.

Per the wide-in / narrow-out policy, the extractor must:
  - put every ``Sample_characteristics_ch1 = k: v`` into ``Sample.extra[k]`` verbatim;
  - NOT promote characteristics keys into canonical fields (``condition``,
    ``batch``, ``donor_id``) — that's llm-paper's job;
  - surface ``Sample_supplementary_file_*`` URLs into ``sample.files``;
  - fall back to ``failures`` when a field is missing, never raise.
"""
from __future__ import annotations

import gzip
import shutil
from pathlib import Path

import pytest

from standl.schema import Source

FIXTURE_SOFT = Path(__file__).parent / "fixtures" / "geo" / "GSE999001_family.soft"


def _ex():
    from standl.extractors.geo_soft import GEOSoftExtractor
    return GEOSoftExtractor()


@pytest.fixture
def cache_with_soft(tmp_path: Path) -> Path:
    """tmp_path pre-populated with a copy of the fixture SOFT file."""
    shutil.copy(FIXTURE_SOFT, tmp_path / "GSE999001_family.soft")
    return tmp_path


@pytest.fixture
def cache_with_soft_gz(tmp_path: Path) -> Path:
    with FIXTURE_SOFT.open("rb") as src, gzip.open(tmp_path / "GSE999001_family.soft.gz", "wb") as dst:
        shutil.copyfileobj(src, dst)
    return tmp_path


# -------- dispatch --------

def test_can_handle_gse_accession():
    assert _ex().can_handle(Source(accessions=["GSE999001"])) >= 0.8


def test_can_handle_zero_for_non_geo():
    assert _ex().can_handle(Source(paper_doi="10.1038/xyz")) == 0.0


# -------- SOFT parsing: series-level --------

def test_extract_produces_dataset_id_from_series_accession(cache_with_soft: Path):
    partial = _ex().extract(Source(accessions=["GSE999001"]), cache_dir=cache_with_soft)
    assert partial.extractor == "geo-soft"
    assert partial.dataset_id == "GSE999001"


def test_extract_populates_source_accessions_and_repository(cache_with_soft: Path):
    partial = _ex().extract(Source(accessions=["GSE999001"]), cache_dir=cache_with_soft)
    assert "GSE999001" in partial.source.accessions
    assert "GEO" in partial.source.repositories


def test_extract_picks_up_top_level_organism(cache_with_soft: Path):
    partial = _ex().extract(Source(accessions=["GSE999001"]), cache_dir=cache_with_soft)
    assert partial.organism is not None
    assert partial.organism.value == "Homo sapiens"


# -------- SOFT parsing: sample-level --------

def test_extract_emits_one_partial_sample_per_gsm(cache_with_soft: Path):
    partial = _ex().extract(Source(accessions=["GSE999001"]), cache_dir=cache_with_soft)
    ids = {s.sample_id for s in partial.samples}
    assert ids == {"GSM999001", "GSM999002"}


def test_extract_sample_accession_equals_gsm(cache_with_soft: Path):
    partial = _ex().extract(Source(accessions=["GSE999001"]), cache_dir=cache_with_soft)
    for s in partial.samples:
        assert s.accession is not None
        assert s.accession.value == s.sample_id


def test_extract_sample_organism_from_sample_organism_ch1(cache_with_soft: Path):
    partial = _ex().extract(Source(accessions=["GSE999001"]), cache_dir=cache_with_soft)
    by_id = {s.sample_id: s for s in partial.samples}
    s = by_id["GSM999001"]
    assert s.organism is not None
    assert s.organism.value == "Homo sapiens"


def test_extract_supplementary_files_go_into_sample_files(cache_with_soft: Path):
    partial = _ex().extract(Source(accessions=["GSE999001"]), cache_dir=cache_with_soft)
    by_id = {s.sample_id: s for s in partial.samples}
    files = by_id["GSM999001"].files
    assert files is not None
    vals = files.value
    assert any("GSM999001_HN01_Tumor_matrix.mtx.gz" in u for u in vals)
    assert any("GSM999001_HN01_Tumor_barcodes.tsv.gz" in u for u in vals)
    assert any("GSM999001_HN01_Tumor_features.tsv.gz" in u for u in vals)
    assert len(vals) == 3


# -------- characteristics → extra (verbatim, no canonical promotion) --------

def test_characteristics_land_in_extra_verbatim(cache_with_soft: Path):
    partial = _ex().extract(Source(accessions=["GSE999001"]), cache_dir=cache_with_soft)
    by_id = {s.sample_id: s for s in partial.samples}
    s = by_id["GSM999001"]
    assert "donor" in s.extra
    assert s.extra["donor"].value == "HN01"
    assert "tissue" in s.extra
    assert s.extra["tissue"].value == "tumor"
    assert "disease" in s.extra
    assert s.extra["disease"].value == "HNSCC"


def test_geo_soft_does_not_promote_condition_or_donor_id(cache_with_soft: Path):
    """Policy: only the LLM extractor infers ``condition`` / ``donor_id`` from
    characteristics free-text. geo-soft stays deterministic."""
    partial = _ex().extract(Source(accessions=["GSE999001"]), cache_dir=cache_with_soft)
    for s in partial.samples:
        assert s.condition is None, f"geo-soft must not populate condition ({s.sample_id})"
        assert s.donor_id is None, f"geo-soft must not populate donor_id ({s.sample_id})"
        assert s.batch is None, f"geo-soft must not populate batch ({s.sample_id})"


# -------- gzip support --------

def test_extract_reads_gzipped_soft(cache_with_soft_gz: Path):
    partial = _ex().extract(Source(accessions=["GSE999001"]), cache_dir=cache_with_soft_gz)
    assert partial.dataset_id == "GSE999001"
    assert len(partial.samples) == 2


# -------- failure paths (no raise) --------

def test_extract_records_failure_when_soft_missing_and_fetch_fails(tmp_path, monkeypatch):
    """Empty cache + a stubbed fetcher that returns None → PartialDesign with
    a failures entry, no exception."""
    from standl.extractors import geo_soft as gs
    monkeypatch.setattr(gs, "_fetch_soft", lambda accession, cache_dir: None)

    partial = _ex().extract(Source(accessions=["GSE999001"]), cache_dir=tmp_path)
    assert partial.samples == []
    assert partial.failures, "expected a failures entry recording the missing SOFT"


def test_extract_records_failure_when_no_gse_in_source(tmp_path):
    partial = _ex().extract(Source(accessions=["PRJNA123"]), cache_dir=tmp_path)
    assert partial.failures, "expected failure when no GSE/GDS accession present"


def test_extract_tolerant_of_unknown_attr_lines(tmp_path):
    """Inject a line with an unrecognized attribute — must not raise."""
    soft = tmp_path / "GSE999001_family.soft"
    content = FIXTURE_SOFT.read_text() + "!Some_future_attribute = ignore me\n"
    soft.write_text(content)

    partial = _ex().extract(Source(accessions=["GSE999001"]), cache_dir=tmp_path)
    assert partial.dataset_id == "GSE999001"
    assert len(partial.samples) == 2
