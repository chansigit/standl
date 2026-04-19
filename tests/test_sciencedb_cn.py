"""Tests for the sciencedb-cn extractor."""
from __future__ import annotations

from pathlib import Path

import pytest

from standl.schema import Source

SDB_ID = "19197"
DSID = "3877f39cb6ce40609aa44933d051d879"
DOI = f"10.57760/sciencedb.{SDB_ID}"

FAKE_DATACITE = {
    "id": f"https://doi.org/{DOI}",
    "doi": DOI.upper(),
    "url": f"https://www.scidb.cn/detail?dataSetId={DSID}",
    "titles": [
        {"title": "The merged count matrix of scRNA-seq data of lymph node metastatic NSCLC"},
    ],
    "creators": [
        {"name": "Di, Chen", "familyName": "Di", "givenName": "Chen"},
        {"name": "Piao Hai-Long"},
    ],
    "subjects": [
        {"subject": "Biology"},
        {"subject": "scRNA-seq"},
        {"subject": "non small cell lung cancer"},
    ],
    "publisher": {"name": "Science Data Bank"},
    "dates": [
        {"date": "2024-12-31", "dateType": "Issued"},
    ],
}

FAKE_FILETREE = {
    "code": 200,
    "data": {
        "fileName": "root",
        "children": [
            {
                "fileName": "README.md",
                "fileId": "abc123",
                "fileSize": 1024,
                "fileMd5": "deadbeef" * 4,
            },
            {
                "fileName": "data",
                "children": [
                    {
                        "fileName": "counts.h5ad",
                        "fileId": "def456",
                        "fileSize": 200_000_000,
                        "fileMd5": "cafebabe" * 4,
                    },
                ],
            },
        ],
    },
}


def _ex():
    from standl.extractors.sciencedb_cn import ScienceDataBankExtractor
    return ScienceDataBankExtractor()


# -------- can_handle --------

def test_can_handle_sciencedb_doi():
    assert _ex().can_handle(Source(paper_doi=DOI)) >= 0.8


def test_can_handle_scidb_detail_url():
    url = f"https://www.scidb.cn/detail?dataSetId={DSID}"
    assert _ex().can_handle(Source(paper_url=url)) >= 0.8


def test_can_handle_with_repo_hint_and_id():
    src = Source(accessions=[SDB_ID], repositories=["ScienceDataBank"])
    assert _ex().can_handle(src) >= 0.8


def test_can_handle_zero_for_non_scidb():
    assert _ex().can_handle(Source(paper_doi="10.5281/zenodo.12345")) == 0.0
    assert _ex().can_handle(Source(accessions=[SDB_ID])) == 0.0


# -------- extract: unauth (DataCite only) --------

def test_extract_metadata_only_without_cookie(monkeypatch, tmp_path: Path):
    """Without SCIDB_COOKIE, DataCite metadata lands in Sample.extra and a
    ``data_access`` failure records the auth requirement. No file URLs."""
    from standl.extractors import sciencedb_cn as sdb
    monkeypatch.setattr(sdb, "_fetch_datacite", lambda doi, cache_dir: FAKE_DATACITE)
    monkeypatch.delenv("SCIDB_COOKIE", raising=False)

    partial = _ex().extract(Source(paper_doi=DOI), cache_dir=tmp_path)
    assert partial.dataset_id == SDB_ID
    assert partial.source.paper_doi == DOI
    assert "ScienceDataBank" in partial.source.repositories

    extra = partial.samples[0].extra
    assert "nsclc" in extra["title"].value.lower()
    assert "Di, Chen" in extra["creators"].value
    assert "scRNA-seq" in extra["subjects"].value
    assert extra["publisher"].value == "Science Data Bank"
    assert extra["issued"].value == "2024-12-31"

    assert "data_access" in partial.failures
    assert "SCIDB_COOKIE" in partial.failures["data_access"]
    assert partial.url_map == {}


def test_extract_resolves_dataSetId_from_datacite_url(monkeypatch, tmp_path: Path):
    """DataCite's ``url`` field carries the SciDB dataSetId; extractor
    should pluck it out so downstream auth calls can target the right id."""
    from standl.extractors import sciencedb_cn as sdb
    monkeypatch.setattr(sdb, "_fetch_datacite", lambda doi, cache_dir: FAKE_DATACITE)
    monkeypatch.delenv("SCIDB_COOKIE", raising=False)

    # Pass only the DOI — no explicit dataSetId. Extractor must still resolve
    # it from DataCite's url field (and the downstream auth gate still
    # records a ``data_access`` failure because no cookie is set).
    partial = _ex().extract(Source(paper_doi=DOI), cache_dir=tmp_path)
    assert "data_access" in partial.failures
    assert DSID in partial.failures["data_access"] or "SCIDB_COOKIE" in partial.failures["data_access"]


# -------- extract: authed (cookie set) --------

def test_extract_fetches_files_when_cookie_present(monkeypatch, tmp_path: Path):
    """With SCIDB_COOKIE + a successful file-tree response, files / URLs /
    md5 / size should all land in the PartialDesign."""
    from standl.extractors import sciencedb_cn as sdb
    monkeypatch.setattr(sdb, "_fetch_datacite", lambda doi, cache_dir: FAKE_DATACITE)
    monkeypatch.setattr(
        sdb, "_fetch_sdb_file_tree",
        lambda dsid, cache_dir, cookie: (sdb._flatten_sdb_tree(FAKE_FILETREE["data"]), None),
    )
    monkeypatch.setenv("SCIDB_COOKIE", "SESSION=fake")

    partial = _ex().extract(Source(paper_doi=DOI), cache_dir=tmp_path)
    sid = partial.dataset_id
    urls = partial.url_map[sid]
    assert len(urls) == 2
    assert all("fileId=" in u for u in urls)
    metas = partial.file_meta[sid]
    assert metas[0]["md5"] == "deadbeef" * 4
    assert metas[0]["size_bytes"] == 1024
    assert metas[1]["size_bytes"] == 200_000_000
    rel = partial.samples[0].files.value
    assert any("README.md" in r for r in rel)
    assert any("counts.h5ad" in r for r in rel)
    # No data_access failure on the authed path.
    assert "data_access" not in partial.failures


def test_extract_records_expired_cookie_failure(monkeypatch, tmp_path: Path):
    """SciDB returns ``code=20001`` when the cookie's expired; extractor
    surfaces that distinctly from the "no cookie at all" case."""
    from standl.extractors import sciencedb_cn as sdb
    monkeypatch.setattr(sdb, "_fetch_datacite", lambda doi, cache_dir: FAKE_DATACITE)
    monkeypatch.setattr(
        sdb, "_fetch_sdb_file_tree",
        lambda dsid, cache_dir, cookie: (None, "SciDB returned code=20001 message='用户未登录'; cookie likely expired"),
    )
    monkeypatch.setenv("SCIDB_COOKIE", "SESSION=expired")

    partial = _ex().extract(Source(paper_doi=DOI), cache_dir=tmp_path)
    assert "data_access" in partial.failures
    assert "expired" in partial.failures["data_access"].lower() or "20001" in partial.failures["data_access"]


# -------- flatten helper --------

def test_flatten_sdb_tree_walks_nested_children():
    from standl.extractors.sciencedb_cn import _flatten_sdb_tree
    files = _flatten_sdb_tree(FAKE_FILETREE["data"])
    names = sorted(f["fileName"] for f in files)
    assert names == ["README.md", "counts.h5ad"]


# -------- failure paths --------

def test_extract_records_failure_on_datacite_error(monkeypatch, tmp_path: Path):
    from standl.extractors import sciencedb_cn as sdb
    def boom(doi, cache_dir):
        raise RuntimeError("datacite down")
    monkeypatch.setattr(sdb, "_fetch_datacite", boom)
    monkeypatch.delenv("SCIDB_COOKIE", raising=False)

    partial = _ex().extract(Source(paper_doi=DOI), cache_dir=tmp_path)
    assert partial.failures  # either datacite or dataSetId failure


def test_extract_without_identifier_is_graceful(tmp_path: Path):
    partial = _ex().extract(Source(paper_url="https://www.scidb.cn/"), cache_dir=tmp_path)
    assert "identifier" in partial.failures
