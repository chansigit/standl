"""Tests for the scp-broad extractor.

All tests run offline by monkeypatching ``_fetch_study`` — the real
extractor hits SCP's ``/api/v1/search?type=study&terms=<acc>`` endpoint.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from standl.schema import Source


FAKE_STUDY = {
    "studies": [
        {
            "accession": "SCP3622",
            "name": "test title",
            "description": "First sentence. Second sentence.",
            "cell_count": 3365,
            "gene_count": 33538,
            "public": True,
            "detached": False,
            "study_url": "/single_cell/study/SCP3622/test",
            "metadata": {
                "disease": ["prostate cancer"],
                "organ": ["prostate gland"],
                "species": ["Homo sapiens"],
                "sex": ["male"],
                "library_preparation_protocol": ["10x 3' v3"],
            },
        }
    ]
}


def _ex():
    from standl.extractors.scp_broad import ScpBroadExtractor
    return ScpBroadExtractor()


# -------- can_handle --------

def test_can_handle_study_url():
    src = Source(paper_url="https://singlecell.broadinstitute.org/single_cell/study/SCP3622")
    assert _ex().can_handle(src) == pytest.approx(0.95)


def test_can_handle_repo_plus_accession():
    src = Source(accessions=["SCP3622"], repositories=["SCP"])
    assert _ex().can_handle(src) == pytest.approx(0.8)


def test_can_handle_bare_accession():
    src = Source(accessions=["SCP3622"])
    assert _ex().can_handle(src) == pytest.approx(0.7)


def test_can_handle_unrelated():
    assert _ex().can_handle(Source(accessions=["GSE123456"])) == 0.0
    assert _ex().can_handle(Source(paper_url="https://example.com")) == 0.0


# -------- extract: happy path --------

def test_extract_happy_path(monkeypatch, tmp_path: Path):
    from standl.extractors import scp_broad as mod
    monkeypatch.setattr(mod, "_fetch_study", lambda acc, cache_dir: FAKE_STUDY)

    partial = _ex().extract(Source(accessions=["SCP3622"]), cache_dir=tmp_path)

    assert partial.extractor == "scp-broad"
    assert partial.dataset_id == "SCP3622"
    assert partial.organism is not None and partial.organism.value == "Homo sapiens"
    assert partial.assay is not None and partial.assay.value == "10x 3' v3"

    assert len(partial.samples) == 1
    s = partial.samples[0]
    assert s.sample_id == "SCP3622"
    assert s.accession is not None and s.accession.value == "SCP3622"
    assert s.tissue is not None and s.tissue.value == "prostate gland"
    assert s.extra["disease"].value == "prostate cancer"
    assert s.extra["sex"].value == "male"
    assert s.extra["cell_count"].value == "3365"
    assert s.extra["library_prep"].value == "10x 3' v3"
    assert s.extra["study_url"].value.startswith("https://singlecell.broadinstitute.org")

    # files endpoint is auth-gated → failures["files"] always set on success path
    assert "files" in partial.failures


# -------- extract: failure paths --------

def test_extract_detached_or_private(monkeypatch, tmp_path: Path):
    from standl.extractors import scp_broad as mod

    detached = {"studies": [{"accession": "SCP3622", "detached": True, "public": True}]}
    monkeypatch.setattr(mod, "_fetch_study", lambda acc, cache_dir: detached)
    partial = _ex().extract(Source(accessions=["SCP3622"]), cache_dir=tmp_path)
    assert "study" in partial.failures
    assert "detached" in partial.failures["study"]


def test_extract_not_found(monkeypatch, tmp_path: Path):
    from standl.extractors import scp_broad as mod
    monkeypatch.setattr(mod, "_fetch_study", lambda acc, cache_dir: {"studies": []})
    partial = _ex().extract(Source(accessions=["SCP9999"]), cache_dir=tmp_path)
    assert "study" in partial.failures
    assert "not found" in partial.failures["study"]


def test_extract_files_failure_always_set(monkeypatch, tmp_path: Path):
    """SCP file listing requires auth; the happy-path extraction MUST still
    record a failures['files'] entry so the rescue flow surfaces it."""
    from standl.extractors import scp_broad as mod
    monkeypatch.setattr(mod, "_fetch_study", lambda acc, cache_dir: FAKE_STUDY)

    partial = _ex().extract(
        Source(paper_url="https://singlecell.broadinstitute.org/single_cell/study/SCP3622"),
        cache_dir=tmp_path,
    )
    assert "files" in partial.failures
    assert "bearer token" in partial.failures["files"]


def test_extract_no_accession_extractable(tmp_path: Path):
    partial = _ex().extract(Source(paper_url="https://example.com/nothing"), cache_dir=tmp_path)
    assert "accession" in partial.failures


def test_extract_api_error_recorded(monkeypatch, tmp_path: Path):
    from standl.extractors import scp_broad as mod

    def boom(acc, cache_dir):
        raise RuntimeError("network down")
    monkeypatch.setattr(mod, "_fetch_study", boom)

    partial = _ex().extract(Source(accessions=["SCP3622"]), cache_dir=tmp_path)
    assert "api" in partial.failures
    assert "RuntimeError" in partial.failures["api"]


# -------- extract: cache --------

def test_extract_cache_hit(monkeypatch, tmp_path: Path):
    """Second call should hit the per-accession cache file and not re-invoke
    the (unpatched) network path."""
    import json
    from standl.extractors import scp_broad as mod

    # Seed the cache directly.
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "scp_SCP3622.json").write_text(json.dumps(FAKE_STUDY))

    # Use the REAL _fetch_study (no monkeypatch of it) — if it hits network
    # we'll blow up on requests import / HTTP. But cache hit should short-circuit.
    # Still, guard by monkeypatching requests.get to explode if called.
    import sys
    import types
    fake_requests = types.ModuleType("requests")
    def _blow(*a, **kw):
        raise AssertionError("cache miss: network was hit")
    fake_requests.get = _blow  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    partial = _ex().extract(Source(accessions=["SCP3622"]), cache_dir=tmp_path)
    assert partial.dataset_id == "SCP3622"
    assert partial.organism.value == "Homo sapiens"
