"""Tests for the figshare extractor."""
from __future__ import annotations

from pathlib import Path

import pytest

from standl.schema import Source

ARTICLE_ID = "29538791"

FAKE_ARTICLE = {
    "id": int(ARTICLE_ID),
    "doi": f"10.6084/m9.figshare.{ARTICLE_ID}.v3",
    "title": "Pan_Disease_Mouse_Neutrophil.h5ad",
    "tags": ["Neutrophils", "immune signatures"],
    "license": {"name": "CC BY 4.0"},
    "description": "<p>Curated pan-disease neutrophil atlas.</p>",
    "files": [
        {
            "name": "msNeu_minimal.h5ad",
            "size": 1_018_464_348,
            "computed_md5": "70a6069ea111124793033667a08298f9",
            "download_url": "https://ndownloader.figshare.com/files/56165831",
        },
        {
            "name": "common signatures.pdf",
            "size": 2_944_392,
            "computed_md5": "e660e2fd4a001c9b4120e94349ed57c7",
            "download_url": "https://ndownloader.figshare.com/files/56848697",
        },
    ],
}


def _ex():
    from standl.extractors.figshare import FigshareExtractor
    return FigshareExtractor()


# -------- can_handle --------

def test_can_handle_figshare_doi():
    assert _ex().can_handle(Source(paper_doi=f"10.6084/m9.figshare.{ARTICLE_ID}")) >= 0.8


def test_can_handle_figshare_doi_with_version():
    assert _ex().can_handle(Source(paper_doi=f"10.6084/m9.figshare.{ARTICLE_ID}.v3")) >= 0.8


def test_can_handle_figshare_article_url():
    url = f"https://figshare.com/articles/dataset/Pan_Disease/{ARTICLE_ID}"
    assert _ex().can_handle(Source(paper_url=url)) >= 0.8


def test_can_handle_id_with_figshare_repository():
    src = Source(accessions=[ARTICLE_ID], repositories=["Figshare"])
    assert _ex().can_handle(src) >= 0.8


def test_can_handle_zero_for_non_figshare_doi():
    assert _ex().can_handle(Source(paper_doi="10.1038/xyz")) == 0.0


def test_can_handle_zero_for_bare_numeric_id():
    assert _ex().can_handle(Source(accessions=[ARTICLE_ID])) == 0.0


# -------- extract --------

def test_extract_builds_partial(monkeypatch, tmp_path: Path):
    from standl.extractors import figshare as fs
    monkeypatch.setattr(fs, "_fetch_article", lambda article_id, cache_dir: FAKE_ARTICLE)

    partial = _ex().extract(
        Source(paper_doi=f"10.6084/m9.figshare.{ARTICLE_ID}"), cache_dir=tmp_path,
    )
    assert partial.extractor == "figshare"
    assert partial.dataset_id == ARTICLE_ID
    assert partial.source.paper_doi.startswith(f"10.6084/m9.figshare.{ARTICLE_ID}")


def test_extract_populates_files_and_url_map(monkeypatch, tmp_path: Path):
    from standl.extractors import figshare as fs
    monkeypatch.setattr(fs, "_fetch_article", lambda article_id, cache_dir: FAKE_ARTICLE)

    partial = _ex().extract(Source(accessions=[ARTICLE_ID], repositories=["Figshare"]), cache_dir=tmp_path)
    urls = partial.url_map[ARTICLE_ID]
    assert len(urls) == 2
    assert all("ndownloader.figshare.com" in u for u in urls)
    rel = partial.samples[0].files.value
    assert rel[0].startswith(f"{ARTICLE_ID}/")


def test_extract_records_tags_in_extra(monkeypatch, tmp_path: Path):
    from standl.extractors import figshare as fs
    monkeypatch.setattr(fs, "_fetch_article", lambda article_id, cache_dir: FAKE_ARTICLE)

    partial = _ex().extract(
        Source(paper_doi=f"10.6084/m9.figshare.{ARTICLE_ID}"), cache_dir=tmp_path,
    )
    extra = partial.samples[0].extra
    assert "tags" in extra and "Neutrophils" in extra["tags"].value
    assert "license" in extra


def test_extract_records_failure_on_missing_article_id(tmp_path: Path):
    partial = _ex().extract(Source(paper_url="https://figshare.com"), cache_dir=tmp_path)
    assert partial.failures


def test_extract_records_failure_on_network_error(monkeypatch, tmp_path: Path):
    from standl.extractors import figshare as fs

    def boom(article_id, cache_dir):
        raise RuntimeError("figshare down")
    monkeypatch.setattr(fs, "_fetch_article", boom)

    partial = _ex().extract(
        Source(paper_doi=f"10.6084/m9.figshare.{ARTICLE_ID}"), cache_dir=tmp_path,
    )
    assert partial.failures


def test_extract_records_failure_on_empty_files(monkeypatch, tmp_path: Path):
    from standl.extractors import figshare as fs
    empty = {**FAKE_ARTICLE, "files": []}
    monkeypatch.setattr(fs, "_fetch_article", lambda article_id, cache_dir: empty)

    partial = _ex().extract(
        Source(paper_doi=f"10.6084/m9.figshare.{ARTICLE_ID}"), cache_dir=tmp_path,
    )
    assert "files" in partial.failures
