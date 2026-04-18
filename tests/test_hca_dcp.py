"""Tests for the hca-dcp extractor.

All tests run offline by monkeypatching ``_fetch_project``. The fake record
mirrors Azul's ``/index/projects/{uuid}`` shape — arrays of plain strings
for organism / disease / organ (unlike CxG's array-of-label-dicts), and a
deeply-nested ``contributedAnalyses`` dict for matrix files.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from standl.schema import Source

UUID = "74b6d569-3b11-42ef-b6b1-a0454522b4a0"
FILE_UUID = "6e63e10e-7a5f-52b8-9242-df9d169b802a"
FILE_VERSION = "2021-02-10T16:56:40.419579Z"

FAKE_PROJECT = {
    "entryId": UUID,
    "projects": [
        {
            "projectId": UUID,
            "projectTitle": "1.3 Million Brain Cells from E18 Mice",
            "projectShortname": "1M Neurons",
            "projectDescription": "...",
            "estimatedCellCount": 1330000,
            "publications": [
                {"doi": "10.1038/ncomms14049"},
            ],
            "accessions": [
                {"namespace": "geo_series", "accession": "GSE93421"},
            ],
            "contributedAnalyses": {
                "genusSpecies": {
                    "Mus musculus": {
                        "developmentStage": {
                            "mouse embryo stage": {
                                "organ": {
                                    "brain": {
                                        "libraryConstructionApproach": {
                                            "10X v2 sequencing": [
                                                {
                                                    "uuid": FILE_UUID,
                                                    "version": FILE_VERSION,
                                                    "name": "1M_neurons.h5",
                                                    "format": "h5",
                                                    "size": 4216018749,
                                                    "sha256": "deadbeef",
                                                    "contentDescription": ["Matrix"],
                                                    "isIntermediate": False,
                                                    "fileSource": "Contributor",
                                                },
                                            ],
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    ],
    "donorOrganisms": [
        {
            "donorCount": 2,
            "genusSpecies": ["Mus musculus"],
            "biologicalSex": ["unknown"],
            "disease": ["normal"],
            "developmentStage": ["mouse embryo stage"],
        },
    ],
    "samples": [
        {"organ": ["brain"], "organPart": ["cortex"], "disease": ["normal"]},
    ],
    "specimens": [
        {"organ": ["brain"], "disease": ["normal"]},
    ],
    "protocols": [
        {"libraryConstructionApproach": ["10x 3' v2"]},
        {"instrumentManufacturerModel": ["Illumina HiSeq 4000"]},
    ],
}


def _ex():
    from standl.extractors.hca_dcp import HCADCPExtractor
    return HCADCPExtractor()


# -------- can_handle --------

def test_can_handle_explore_url():
    src = Source(paper_url=f"https://data.humancellatlas.org/explore/projects/{UUID}")
    assert _ex().can_handle(src) >= 0.8


def test_can_handle_uuid_with_hca_repository():
    src = Source(accessions=[UUID], repositories=["HCA"])
    assert _ex().can_handle(src) >= 0.8


def test_can_handle_generic_hca_url_without_uuid():
    """A data.humancellatlas.org URL that doesn't point to a specific project
    — fire weakly so extract() can surface a clear failure."""
    src = Source(paper_url="https://data.humancellatlas.org/explore")
    s = _ex().can_handle(src)
    assert 0.0 < s < 0.8


def test_can_handle_zero_for_bare_uuid():
    """Ambiguous with CxG — require a repository signal."""
    assert _ex().can_handle(Source(accessions=[UUID])) == 0.0


def test_can_handle_zero_for_geo():
    assert _ex().can_handle(Source(accessions=["GSE123456"])) == 0.0


# -------- extract --------

def test_extract_builds_partial(monkeypatch, tmp_path: Path):
    from standl.extractors import hca_dcp as h
    monkeypatch.setattr(h, "_fetch_project", lambda uuid, cache_dir, catalog=None: FAKE_PROJECT)

    partial = _ex().extract(
        Source(paper_url=f"https://data.humancellatlas.org/explore/projects/{UUID}"),
        cache_dir=tmp_path,
    )

    assert partial.extractor == "hca-dcp"
    assert partial.dataset_id == UUID
    assert partial.organism is not None and partial.organism.value == "Mus musculus"
    assert partial.assay is not None and partial.assay.value == "10x 3' v2"
    assert partial.source.paper_doi == "10.1038/ncomms14049"
    # Project accessions from publications are carried over into source.
    assert "GSE93421" in partial.source.accessions


def test_extract_tissue_from_samples_organ(monkeypatch, tmp_path: Path):
    from standl.extractors import hca_dcp as h
    monkeypatch.setattr(h, "_fetch_project", lambda uuid, cache_dir, catalog=None: FAKE_PROJECT)

    partial = _ex().extract(
        Source(accessions=[UUID], repositories=["HCA"]), cache_dir=tmp_path,
    )
    s = partial.samples[0]
    assert s.tissue is not None and s.tissue.value == "brain"
    # No ontology term in the Azul response shape — tissue_ontology stays None.
    assert s.tissue_ontology is None


def test_extract_extra_fields(monkeypatch, tmp_path: Path):
    from standl.extractors import hca_dcp as h
    monkeypatch.setattr(h, "_fetch_project", lambda uuid, cache_dir, catalog=None: FAKE_PROJECT)

    partial = _ex().extract(
        Source(accessions=[UUID], repositories=["HCA"]), cache_dir=tmp_path,
    )
    extra = partial.samples[0].extra
    assert extra["biologicalSex"].value == "unknown"
    assert extra["disease"].value == "normal"
    assert extra["developmentStage"].value == "mouse embryo stage"
    assert extra["donor_count"].value == "2"
    assert extra["cell_count"].value == "1330000"
    assert extra["title"].value.startswith("1.3 Million")
    assert extra["short_name"].value == "1M Neurons"


def test_extract_walks_contributed_analyses_for_matrix(monkeypatch, tmp_path: Path):
    """Matrix files are buried under contributedAnalyses[genusSpecies][...][...].
    The extractor must flatten and pull out leaf Matrix entries."""
    from standl.extractors import hca_dcp as h
    monkeypatch.setattr(h, "_fetch_project", lambda uuid, cache_dir, catalog=None: FAKE_PROJECT)

    partial = _ex().extract(
        Source(accessions=[UUID], repositories=["HCA"]), cache_dir=tmp_path,
    )
    urls = partial.url_map.get(UUID)
    assert urls and len(urls) == 1
    # Azul fetch URL carries file uuid + version.
    assert FILE_UUID in urls[0]
    assert "version=" in urls[0]
    assert "catalog=" in urls[0]
    # sample.files holds the relative path.
    rel = partial.samples[0].files.value
    assert rel[0].startswith(f"{UUID}/") and rel[0].endswith(".h5")


def test_extract_skips_intermediate_matrices(monkeypatch, tmp_path: Path):
    """contentDescription != Matrix OR isIntermediate=True → don't promote."""
    from standl.extractors import hca_dcp as h
    fake = {
        **FAKE_PROJECT,
        "projects": [
            {
                **FAKE_PROJECT["projects"][0],
                "contributedAnalyses": {
                    "x": [
                        {
                            "uuid": FILE_UUID,
                            "version": FILE_VERSION,
                            "name": "intermediate.h5",
                            "format": "h5",
                            "contentDescription": ["Matrix"],
                            "isIntermediate": True,
                            "fileSource": "Contributor",
                        },
                    ],
                },
            },
        ],
    }
    monkeypatch.setattr(h, "_fetch_project", lambda uuid, cache_dir, catalog=None: fake)

    partial = _ex().extract(Source(accessions=[UUID], repositories=["HCA"]), cache_dir=tmp_path)
    assert partial.url_map == {}
    assert "matrices" in partial.failures


# -------- failure paths --------

def test_extract_records_failure_on_missing_uuid(tmp_path: Path):
    partial = _ex().extract(
        Source(paper_url="https://data.humancellatlas.org/explore"),
        cache_dir=tmp_path,
    )
    assert partial.failures


def test_extract_records_failure_on_network_error(monkeypatch, tmp_path: Path):
    from standl.extractors import hca_dcp as h

    def boom(uuid, cache_dir, catalog=None):
        raise RuntimeError("network down")
    monkeypatch.setattr(h, "_fetch_project", boom)

    partial = _ex().extract(
        Source(accessions=[UUID], repositories=["HCA"]), cache_dir=tmp_path,
    )
    assert partial.failures and "network" in next(iter(partial.failures.values())).lower()


def test_extract_populates_file_meta_sha256_and_size(monkeypatch, tmp_path: Path):
    """Azul gives sha256 + size on matrix files — not md5."""
    from standl.extractors import hca_dcp as h
    monkeypatch.setattr(h, "_fetch_project", lambda uuid, cache_dir, catalog=None: FAKE_PROJECT)

    # Extend the fixture's matrix entry with sha256 + size to exercise the plumbing.
    fake = dict(FAKE_PROJECT)
    fake_proj = dict(FAKE_PROJECT["projects"][0])
    fake_ca = {
        "x": [
            {
                "uuid": FILE_UUID,
                "version": FILE_VERSION,
                "name": "1M_neurons.h5",
                "format": "h5",
                "size": 4216018749,
                "sha256": "255a36ee92de25cb3568faa2c27d31fe6d0db30f285c5c977be8d6245de14044",
                "contentDescription": ["Matrix"],
                "isIntermediate": False,
                "fileSource": "Contributor",
            },
        ],
    }
    fake_proj["contributedAnalyses"] = fake_ca
    fake["projects"] = [fake_proj]
    monkeypatch.setattr(h, "_fetch_project", lambda uuid, cache_dir, catalog=None: fake)

    partial = _ex().extract(Source(accessions=[UUID], repositories=["HCA"]), cache_dir=tmp_path)
    metas = partial.file_meta[UUID]
    assert metas[0]["sha256"].startswith("255a36")
    assert metas[0]["size_bytes"] == 4216018749
