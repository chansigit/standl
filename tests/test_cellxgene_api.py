"""Tests for the cellxgene-api extractor.

All tests run offline by monkeypatching the ``_fetch_datasets_list`` hook —
the real extractor pulls ``/curation/v1/datasets`` (full index, ~2 MB of
JSON) and filters client-side.

Canonical dataset UUID used throughout: ``6cda3b13-7257-45b9-ac20-0a7e6697e4f2``
(Aging Mouse Brain Atlas / CZI). The fake record below mirrors the real
schema but is inlined so tests don't depend on network state.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from standl.schema import Source

UUID = "6cda3b13-7257-45b9-ac20-0a7e6697e4f2"

FAKE_DATASET = {
    "dataset_id": UUID,
    "dataset_version_id": "9349c6fb-758d-483d-a9ab-0f0563ba762a",
    "collection_id": "db468083-041c-41ca-8f6f-bf991a070adf",
    "title": "Aging vascular niche in umbilical vein",
    "citation": "Publication: https://doi.org/10.1038/s41467-020-18957-w",
    "assets": [
        {
            "filetype": "H5AD",
            "filesize": 421889692,
            "url": "https://datasets.cellxgene.cziscience.com/9349c6fb.h5ad",
        },
    ],
    "organism": [{"label": "Homo sapiens", "ontology_term_id": "NCBITaxon:9606"}],
    "assay": [{"label": "10x 3' v2", "ontology_term_id": "EFO:0009899"}],
    "tissue": [{"label": "umbilical vein", "ontology_term_id": "UBERON:0002067"}],
    "cell_type": [{"label": "endothelial cell", "ontology_term_id": "CL:0000115"}],
    "disease": [{"label": "normal", "ontology_term_id": "PATO:0000461"}],
    "sex": [{"label": "female", "ontology_term_id": "PATO:0000383"}],
    "cell_count": 59605,
}


def _ex():
    from standl.extractors.cellxgene_api import CellxGeneAPIExtractor
    return CellxGeneAPIExtractor()


# -------- can_handle --------

def test_can_handle_explorer_url():
    src = Source(paper_url=f"https://cellxgene.cziscience.com/e/{UUID}.cxg/")
    assert _ex().can_handle(src) >= 0.8


def test_can_handle_collection_url_without_dataset_id():
    """A collection URL alone — we can't resolve a single dataset from it,
    so weakly handle (> 0 so extract runs and records a descriptive failure)."""
    src = Source(paper_url="https://cellxgene.cziscience.com/collections/abc")
    assert _ex().can_handle(src) > 0.0
    assert _ex().can_handle(src) < 0.8  # weaker than an explicit dataset URL


def test_can_handle_uuid_with_cellxgene_repository():
    src = Source(accessions=[UUID], repositories=["CELLxGENE"])
    assert _ex().can_handle(src) >= 0.8


def test_can_handle_zero_for_uuid_alone():
    """A bare UUID is ambiguous (could be HCA/Synapse/anything). Don't fire."""
    assert _ex().can_handle(Source(accessions=[UUID])) == 0.0


def test_can_handle_zero_for_geo():
    assert _ex().can_handle(Source(accessions=["GSE123456"])) == 0.0


# -------- extract: happy path --------

def test_extract_builds_partial_from_mock(monkeypatch, tmp_path: Path):
    from standl.extractors import cellxgene_api as cxg
    monkeypatch.setattr(cxg, "_fetch_datasets_list", lambda cache_dir: [FAKE_DATASET])

    partial = _ex().extract(
        Source(paper_url=f"https://cellxgene.cziscience.com/e/{UUID}.cxg/"),
        cache_dir=tmp_path,
    )

    assert partial.extractor == "cellxgene-api"
    assert partial.dataset_id == UUID
    assert len(partial.samples) == 1
    s = partial.samples[0]
    assert s.sample_id == UUID
    assert s.accession is not None and s.accession.value == UUID

    assert partial.organism is not None and partial.organism.value == "Homo sapiens"
    assert partial.assay is not None and partial.assay.value == "10x 3' v2"


def test_extract_populates_url_map_with_h5ad_url(monkeypatch, tmp_path: Path):
    from standl.extractors import cellxgene_api as cxg
    monkeypatch.setattr(cxg, "_fetch_datasets_list", lambda cache_dir: [FAKE_DATASET])

    partial = _ex().extract(
        Source(paper_url=f"https://cellxgene.cziscience.com/e/{UUID}.cxg/"),
        cache_dir=tmp_path,
    )

    urls = partial.url_map.get(UUID)
    assert urls and len(urls) == 1
    assert urls[0].endswith(".h5ad")

    assert partial.samples[0].files is not None
    rel = partial.samples[0].files.value
    assert rel[0].startswith(f"{UUID}/")
    assert rel[0].endswith(".h5ad")


def test_extract_picks_up_tissue_ontology_uberon(monkeypatch, tmp_path: Path):
    from standl.extractors import cellxgene_api as cxg
    monkeypatch.setattr(cxg, "_fetch_datasets_list", lambda cache_dir: [FAKE_DATASET])

    partial = _ex().extract(
        Source(accessions=[UUID], repositories=["CELLxGENE"]),
        cache_dir=tmp_path,
    )

    s = partial.samples[0]
    assert s.tissue_ontology is not None
    assert s.tissue_ontology.value == "UBERON:0002067"


def test_extract_lands_descriptive_fields_in_extra(monkeypatch, tmp_path: Path):
    from standl.extractors import cellxgene_api as cxg
    monkeypatch.setattr(cxg, "_fetch_datasets_list", lambda cache_dir: [FAKE_DATASET])

    partial = _ex().extract(
        Source(accessions=[UUID], repositories=["CELLxGENE"]),
        cache_dir=tmp_path,
    )
    extra = partial.samples[0].extra
    assert "cell_type" in extra and "endothelial cell" in extra["cell_type"].value
    assert "disease" in extra and "normal" in extra["disease"].value
    assert "sex" in extra and "female" in extra["sex"].value
    assert "cell_count" in extra and extra["cell_count"].value == "59605"


def test_extract_prefers_raw_h5ad_when_both_present(monkeypatch, tmp_path: Path):
    """If the API ever returns both H5AD and RAW_H5AD assets, prefer the raw
    version (integer counts, unambiguous for downstream count-based work)."""
    from standl.extractors import cellxgene_api as cxg
    fake = {
        **FAKE_DATASET,
        "assets": [
            {"filetype": "H5AD", "url": "https://x/normalized.h5ad"},
            {"filetype": "RAW_H5AD", "url": "https://x/raw.h5ad"},
        ],
    }
    monkeypatch.setattr(cxg, "_fetch_datasets_list", lambda cache_dir: [fake])

    partial = _ex().extract(Source(accessions=[UUID], repositories=["CELLxGENE"]), cache_dir=tmp_path)
    urls = partial.url_map[UUID]
    assert urls[0].endswith("raw.h5ad"), f"expected raw to win, got {urls}"


# -------- extract: failure paths --------

def test_extract_records_failure_when_dataset_not_in_index(monkeypatch, tmp_path: Path):
    from standl.extractors import cellxgene_api as cxg
    monkeypatch.setattr(cxg, "_fetch_datasets_list", lambda cache_dir: [])

    partial = _ex().extract(
        Source(paper_url=f"https://cellxgene.cziscience.com/e/{UUID}.cxg/"),
        cache_dir=tmp_path,
    )
    assert partial.samples == []
    assert partial.failures, "missing dataset must be recorded"


def test_extract_records_failure_on_network_error(monkeypatch, tmp_path: Path):
    from standl.extractors import cellxgene_api as cxg

    def boom(cache_dir):
        raise RuntimeError("network down")
    monkeypatch.setattr(cxg, "_fetch_datasets_list", boom)

    partial = _ex().extract(
        Source(paper_url=f"https://cellxgene.cziscience.com/e/{UUID}.cxg/"),
        cache_dir=tmp_path,
    )
    assert partial.failures
    assert any("RuntimeError" in v or "network" in v for v in partial.failures.values())


def test_extract_records_failure_when_no_dataset_id_extractable(tmp_path: Path):
    """A collection URL or source with no dataset identifier → can't pick one.
    extract() must surface this as a failure, not pick arbitrarily."""
    partial = _ex().extract(
        Source(paper_url="https://cellxgene.cziscience.com/collections/abc"),
        cache_dir=tmp_path,
    )
    assert partial.failures
