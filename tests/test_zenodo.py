"""Tests for the zenodo extractor."""
from __future__ import annotations

from pathlib import Path

import pytest

from standl.schema import Source

RECORD_ID = "19638700"

FAKE_RECORD = {
    "id": int(RECORD_ID),
    "doi": f"10.5281/zenodo.{RECORD_ID}",
    "metadata": {
        "title": "Single-cell atlas of tumor-infiltrating lymphocytes",
        "keywords": ["single-cell", "RNA-seq", "tumor"],
        "license": {"id": "cc-by-4.0"},
        "description": "<p>Raw 10x matrices from N patients...</p>",
    },
    "files": [
        {
            "key": "P01_matrix.mtx.gz",
            "size": 115_000_000,
            "checksum": "md5:9f5ca452848f82bb02e813f6f5abcdef",
            "links": {"self": "https://zenodo.org/records/19638700/files/P01_matrix.mtx.gz/content"},
        },
        {
            "key": "P01_barcodes.tsv.gz",
            "size": 2_000_000,
            "checksum": "md5:abcd1234",
            "links": {"self": "https://zenodo.org/records/19638700/files/P01_barcodes.tsv.gz/content"},
        },
    ],
}


def _ex():
    from standl.extractors.zenodo import ZenodoExtractor
    return ZenodoExtractor()


# -------- can_handle --------

def test_can_handle_zenodo_doi():
    assert _ex().can_handle(Source(paper_doi=f"10.5281/zenodo.{RECORD_ID}")) >= 0.8


def test_can_handle_zenodo_record_url():
    assert _ex().can_handle(Source(paper_url=f"https://zenodo.org/records/{RECORD_ID}")) >= 0.8


def test_can_handle_zenodo_legacy_record_url():
    """Old-style `zenodo.org/record/<id>` (singular) still resolves."""
    assert _ex().can_handle(Source(paper_url=f"https://zenodo.org/record/{RECORD_ID}")) >= 0.8


def test_can_handle_id_with_zenodo_repository():
    src = Source(accessions=[RECORD_ID], repositories=["Zenodo"])
    assert _ex().can_handle(src) >= 0.8


def test_can_handle_zero_for_non_zenodo_doi():
    assert _ex().can_handle(Source(paper_doi="10.1038/xyz")) == 0.0


def test_can_handle_zero_for_bare_numeric_id():
    assert _ex().can_handle(Source(accessions=[RECORD_ID])) == 0.0


# -------- extract --------

def test_extract_builds_partial(monkeypatch, tmp_path: Path):
    from standl.extractors import zenodo as zen
    monkeypatch.setattr(zen, "_fetch_record", lambda record_id, cache_dir: FAKE_RECORD)

    partial = _ex().extract(Source(paper_doi=f"10.5281/zenodo.{RECORD_ID}"), cache_dir=tmp_path)

    assert partial.extractor == "zenodo"
    assert partial.dataset_id == RECORD_ID
    assert partial.source.paper_doi == f"10.5281/zenodo.{RECORD_ID}"
    # Record title lands in notes.
    assert "lymphocytes" in (partial.notes or "").lower()


def test_extract_populates_files_and_url_map(monkeypatch, tmp_path: Path):
    from standl.extractors import zenodo as zen
    monkeypatch.setattr(zen, "_fetch_record", lambda record_id, cache_dir: FAKE_RECORD)

    partial = _ex().extract(Source(paper_url=f"https://zenodo.org/records/{RECORD_ID}"), cache_dir=tmp_path)

    urls = partial.url_map[RECORD_ID]
    assert len(urls) == 2
    assert all(u.startswith("https://zenodo.org/") for u in urls)
    rel = partial.samples[0].files.value
    assert rel[0] == f"{RECORD_ID}/P01_matrix.mtx.gz"
    assert len(rel) == 2


def test_extract_records_keywords_in_extra(monkeypatch, tmp_path: Path):
    from standl.extractors import zenodo as zen
    monkeypatch.setattr(zen, "_fetch_record", lambda record_id, cache_dir: FAKE_RECORD)

    partial = _ex().extract(Source(accessions=[RECORD_ID], repositories=["Zenodo"]), cache_dir=tmp_path)

    extra = partial.samples[0].extra
    assert "keywords" in extra and "single-cell" in extra["keywords"].value
    assert "license" in extra and extra["license"].value == "cc-by-4.0"


def test_extract_records_failure_on_missing_record_id(tmp_path: Path):
    partial = _ex().extract(Source(paper_url="https://zenodo.org"), cache_dir=tmp_path)
    assert partial.failures


def test_extract_records_failure_on_network_error(monkeypatch, tmp_path: Path):
    from standl.extractors import zenodo as zen

    def boom(record_id, cache_dir):
        raise RuntimeError("zenodo down")
    monkeypatch.setattr(zen, "_fetch_record", boom)

    partial = _ex().extract(Source(paper_doi=f"10.5281/zenodo.{RECORD_ID}"), cache_dir=tmp_path)
    assert partial.failures


def test_extract_records_failure_on_empty_file_list(monkeypatch, tmp_path: Path):
    from standl.extractors import zenodo as zen
    empty = {**FAKE_RECORD, "files": []}
    monkeypatch.setattr(zen, "_fetch_record", lambda record_id, cache_dir: empty)

    partial = _ex().extract(Source(paper_doi=f"10.5281/zenodo.{RECORD_ID}"), cache_dir=tmp_path)
    assert partial.url_map == {}
    assert "files" in partial.failures
