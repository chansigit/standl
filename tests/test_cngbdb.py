"""Tests for the cngbdb extractor."""
from __future__ import annotations

from pathlib import Path

import pytest

from standl.schema import Source

ACC = "CNP0001543"
PAPER_URL = f"https://db.cngb.org/search/project/{ACC}/"

FAKE_HTML = f"""<!doctype html>
<html>
<head>
<title>Spatiotemporal transcriptomic atlas of mouse organogenesis using DNA nanoball patterned arrays - Project - Data resources - CNGBdb</title>
<meta name="description" content="We profiled mouse embryos across multiple stages using Stereo-seq. Datasets include CNS0456789 and CNS0456790 among others.">
</head>
<body>
<p>Samples: CNS0456789, CNS0456790, CNS0456791.</p>
</body>
</html>
"""

FAKE_HTML_NO_TITLE = """<!doctype html>
<html><head>
<meta name="description" content="Some description here.">
</head><body></body></html>
"""

FAKE_DATACITE = {
    "id": "https://doi.org/10.1234/cngbdb.demo",
    "attributes": {
        "doi": "10.1234/cngbdb.demo",
        "publicationYear": 2022,
        "subjects": [
            {"subject": "Mus musculus"},
            {"subject": "single-cell"},
        ],
    },
}


def _ex():
    from standl.extractors.cngbdb import CNGBdbExtractor
    return CNGBdbExtractor()


# -------- can_handle --------

def test_can_handle_paper_url():
    assert _ex().can_handle(Source(paper_url=PAPER_URL)) >= 0.9


def test_can_handle_repo_plus_accession():
    src = Source(accessions=[ACC], repositories=["CNGBdb"])
    assert _ex().can_handle(src) >= 0.8


def test_can_handle_repo_cnsa_with_sample_accession():
    src = Source(accessions=["CNS0456789"], repositories=["CNSA"])
    assert _ex().can_handle(src) >= 0.8


def test_can_handle_bare_cnp_accession():
    score = _ex().can_handle(Source(accessions=[ACC]))
    assert 0.5 <= score < 0.85


def test_can_handle_zero_for_unrelated():
    assert _ex().can_handle(Source(paper_doi="10.1038/xyz")) == 0.0
    assert _ex().can_handle(Source(accessions=["GSE139324"])) == 0.0


def test_can_handle_zero_for_wrong_prefix_cra():
    # CRA belongs to gsa-cncb, not CNGBdb.
    assert _ex().can_handle(Source(accessions=["CRA001234"])) == 0.0


# -------- extract: happy path --------

def test_extract_happy_path(monkeypatch, tmp_path: Path):
    from standl.extractors import cngbdb as c
    monkeypatch.setattr(c, "_fetch_project_html", lambda acc, cd: FAKE_HTML)
    monkeypatch.setattr(
        c, "_fetch_datacite", lambda acc, cd: FAKE_DATACITE,
    )

    partial = _ex().extract(Source(paper_url=PAPER_URL), cache_dir=tmp_path)

    assert partial.extractor == "cngbdb"
    assert partial.dataset_id == ACC
    assert "CNGBdb" in partial.source.repositories
    assert partial.source.paper_doi == "10.1234/cngbdb.demo"
    # Title becomes notes, minus the CNGBdb boilerplate suffix.
    assert partial.notes is not None
    assert "Spatiotemporal" in partial.notes
    assert "CNGBdb" not in partial.notes

    sample = partial.samples[0]
    assert sample.sample_id == ACC
    assert "description" in sample.extra
    assert "Stereo-seq" in sample.extra["description"].value
    # organism inferred from datacite subjects → "Mus musculus"
    assert partial.organism is not None
    assert partial.organism.value == "Mus musculus"
    assert sample.extra["publication_year"].value == "2022"
    assert "ftp_hint" in sample.extra


# -------- HTML has no title --------

def test_extract_without_title_records_failure(monkeypatch, tmp_path: Path):
    from standl.extractors import cngbdb as c
    monkeypatch.setattr(c, "_fetch_project_html", lambda acc, cd: FAKE_HTML_NO_TITLE)
    monkeypatch.setattr(c, "_fetch_datacite", lambda acc, cd: None)

    partial = _ex().extract(Source(accessions=[ACC]), cache_dir=tmp_path)
    assert "title" in partial.failures
    # Still emits a sample.
    assert len(partial.samples) == 1
    assert partial.samples[0].sample_id == ACC
    # Description from meta still made it through.
    assert "description" in partial.samples[0].extra


# -------- DataCite miss --------

def test_extract_datacite_miss(monkeypatch, tmp_path: Path):
    from standl.extractors import cngbdb as c
    monkeypatch.setattr(c, "_fetch_project_html", lambda acc, cd: FAKE_HTML)
    monkeypatch.setattr(c, "_fetch_datacite", lambda acc, cd: None)

    partial = _ex().extract(Source(accessions=[ACC]), cache_dir=tmp_path)
    assert partial.dataset_id == ACC
    assert partial.source.paper_doi is None
    # Still has HTML-derived metadata.
    assert partial.notes is not None
    assert "Spatiotemporal" in partial.notes
    # No organism since no subjects.
    assert partial.organism is None
    # files failure still recorded (manual shard resolution step).
    assert "files" in partial.failures


# -------- CNS accessions in HTML body --------

def test_extract_captures_cns_accessions_in_body(monkeypatch, tmp_path: Path):
    from standl.extractors import cngbdb as c
    monkeypatch.setattr(c, "_fetch_project_html", lambda acc, cd: FAKE_HTML)
    monkeypatch.setattr(c, "_fetch_datacite", lambda acc, cd: None)

    partial = _ex().extract(Source(accessions=[ACC]), cache_dir=tmp_path)
    extras = partial.samples[0].extra
    assert "samples_in_html" in extras
    s = extras["samples_in_html"].value
    assert "CNS0456789" in s
    assert "CNS0456790" in s
    assert "CNS0456791" in s


# -------- cache hit --------

def test_cache_hit_skips_second_fetch(monkeypatch, tmp_path: Path):
    from standl.extractors import cngbdb as c

    # Seed the cache files directly.
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / f"cngbdb_{ACC}.html").write_text(FAKE_HTML, encoding="utf-8")
    import json as _json
    (tmp_path / f"cngbdb_datacite_{ACC}.json").write_text(_json.dumps(FAKE_DATACITE))

    calls: dict[str, int] = {"html": 0, "datacite": 0}

    def _boom_requests_get(*args, **kwargs):
        calls["html"] += 1
        raise AssertionError("should not hit network when cache present")

    # Patch requests.get inside the module; cache hits must short-circuit it.
    import requests
    monkeypatch.setattr(requests, "get", _boom_requests_get)

    partial = _ex().extract(Source(accessions=[ACC]), cache_dir=tmp_path)
    assert partial.dataset_id == ACC
    assert partial.notes is not None and "Spatiotemporal" in partial.notes
    assert calls["html"] == 0


# -------- no accession --------

def test_extract_without_accession_is_graceful(tmp_path: Path):
    partial = _ex().extract(Source(paper_url="https://db.cngb.org/"), cache_dir=tmp_path)
    assert "accession" in partial.failures
