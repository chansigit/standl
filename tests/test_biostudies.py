"""Tests for the biostudies extractor (EBI BioStudies / ArrayExpress)."""
from __future__ import annotations

from pathlib import Path

import pytest

from standl.schema import Source

ACC = "E-MTAB-10553"

FAKE_STUDY = {
    "accno": ACC,
    "type": "Submission",
    "attributes": [
        {"name": "Title", "value": "Single-cell RNA sequencing of human liver"},
        {"name": "AttachTo", "value": "ArrayExpress"},
    ],
    "section": {
        "accno": ACC,
        "type": "Study",
        "attributes": [
            {"name": "Title", "value": "Single-cell RNA sequencing of human liver"},
            {"name": "Study type", "value": "RNA-seq of coding RNA from single cells"},
            {"name": "Organism", "value": "Homo sapiens"},
            {"name": "Description", "value": "Liver single-cell atlas."},
        ],
        "subsections": [],
        "links": [],
    },
}

FAKE_FILES = {
    "draw": 1,
    "recordsTotal": 4,
    "recordsFiltered": 4,
    "data": [
        {"Name": "liver_processed.h5ad", "Size": "1500000000", "Section": "processed-data",
         "Samples": "Donor A", "Description": "Processed AnnData",
         "Type": "", "Format": "h5ad", "path": "liver_processed.h5ad",
         "type": "file", "size": 1_500_000_000},
        {"Name": "raw/Donor_A_R1.fastq.gz", "Size": "8000000000", "Section": "raw-data",
         "Samples": "Donor A", "Description": "Raw fastq",
         "Type": "", "Format": "fastq", "path": "raw/Donor_A_R1.fastq.gz",
         "type": "file", "size": 8_000_000_000},
        {"Name": "raw/Donor_A_R2.fastq.gz", "Size": "8000000000", "Section": "raw-data",
         "Samples": "Donor A", "Description": "Raw fastq",
         "Type": "", "Format": "fastq", "path": "raw/Donor_A_R2.fastq.gz",
         "type": "file", "size": 8_000_000_000},
        {"Name": "metadata/samples.tsv", "Size": "4096", "Section": "metadata",
         "Samples": "", "Description": "Sample sheet",
         "Type": "", "Format": "tsv", "path": "metadata/samples.tsv",
         "type": "file", "size": 4096},
    ],
}

FASTQ_ONLY_FILES = {
    "draw": 1,
    "recordsTotal": 2,
    "data": [
        {"Name": "a.fastq.gz", "Size": "1", "Section": "raw-data", "Samples": "",
         "Description": "", "Type": "", "Format": "fastq", "path": "a.fastq.gz",
         "type": "file", "size": 1},
        {"Name": "b.bam", "Size": "1", "Section": "raw-data", "Samples": "",
         "Description": "", "Type": "", "Format": "bam", "path": "b.bam",
         "type": "file", "size": 1},
    ],
}


def _ex():
    from standl.extractors.biostudies import BioStudiesExtractor
    return BioStudiesExtractor()


# -------- can_handle --------

def test_can_handle_e_mtab_accession():
    assert _ex().can_handle(Source(accessions=["E-MTAB-10553"])) >= 0.8


def test_can_handle_s_biad_accession():
    assert _ex().can_handle(Source(accessions=["S-BIAD944"])) >= 0.8


def test_can_handle_various_biostudies_prefixes():
    for acc in ["E-GEOD-1234", "E-CURD-88", "S-BSST100", "S-SCDT-FOO-1"]:
        s = _ex().can_handle(Source(accessions=[acc]))
        assert s >= 0.8, f"{acc} scored {s}"


def test_can_handle_biostudies_url():
    url = "https://www.ebi.ac.uk/biostudies/studies/E-MTAB-10553"
    assert _ex().can_handle(Source(paper_url=url)) >= 0.8


def test_can_handle_zero_for_geo():
    assert _ex().can_handle(Source(accessions=["GSE139324"])) == 0.0


def test_can_handle_zero_for_unrelated_doi():
    assert _ex().can_handle(Source(paper_doi="10.1038/xyz")) == 0.0


# -------- extract --------

def test_extract_builds_partial_filters_raw(monkeypatch, tmp_path: Path):
    """Default filter excludes .fastq*/.bam/.sra (SRA-level raw data) per
    roadmap's processed-matrices-first policy. .h5ad and .tsv stay."""
    from standl.extractors import biostudies as bs
    monkeypatch.setattr(bs, "_fetch_study", lambda acc, cache_dir: FAKE_STUDY)
    monkeypatch.setattr(bs, "_fetch_files", lambda acc, cache_dir: FAKE_FILES)

    partial = _ex().extract(Source(accessions=[ACC]), cache_dir=tmp_path)

    assert partial.extractor == "biostudies"
    assert partial.dataset_id == ACC
    assert partial.organism is not None and partial.organism.value == "Homo sapiens"

    rel = partial.samples[0].files.value
    assert "liver_processed.h5ad" in rel[0] or "metadata/samples.tsv" in rel[0]
    # No fastq / bam in the kept set.
    assert not any(r.endswith(".fastq.gz") or r.endswith(".bam") for r in rel)
    # h5ad and tsv are kept.
    names = "\n".join(rel)
    assert "liver_processed.h5ad" in names
    assert "samples.tsv" in names


def test_extract_url_pattern(monkeypatch, tmp_path: Path):
    from standl.extractors import biostudies as bs
    monkeypatch.setattr(bs, "_fetch_study", lambda acc, cache_dir: FAKE_STUDY)
    monkeypatch.setattr(bs, "_fetch_files", lambda acc, cache_dir: FAKE_FILES)

    partial = _ex().extract(Source(accessions=[ACC]), cache_dir=tmp_path)
    urls = partial.url_map[ACC]
    assert all(u.startswith(f"https://www.ebi.ac.uk/biostudies/files/{ACC}/") for u in urls)


def test_extract_records_data_format_failure_when_only_raw(monkeypatch, tmp_path: Path):
    """Per-roadmap: SRA-level raw data is out of scope. When a study has
    nothing but fastq/bam, emit a data_format failure so downstream sees it."""
    from standl.extractors import biostudies as bs
    monkeypatch.setattr(bs, "_fetch_study", lambda acc, cache_dir: FAKE_STUDY)
    monkeypatch.setattr(bs, "_fetch_files", lambda acc, cache_dir: FASTQ_ONLY_FILES)

    partial = _ex().extract(Source(accessions=[ACC]), cache_dir=tmp_path)
    assert partial.url_map == {}
    assert "data_format" in partial.failures


def test_extract_records_metadata_attrs_in_extras(monkeypatch, tmp_path: Path):
    from standl.extractors import biostudies as bs
    monkeypatch.setattr(bs, "_fetch_study", lambda acc, cache_dir: FAKE_STUDY)
    monkeypatch.setattr(bs, "_fetch_files", lambda acc, cache_dir: FAKE_FILES)

    partial = _ex().extract(Source(accessions=[ACC]), cache_dir=tmp_path)
    extra = partial.samples[0].extra
    assert "title" in extra and "liver" in extra["title"].value.lower()
    assert "study_type" in extra


def test_extract_records_failure_on_missing_accession(tmp_path: Path):
    partial = _ex().extract(Source(paper_url="https://www.ebi.ac.uk/biostudies"), cache_dir=tmp_path)
    assert partial.failures


def test_extract_records_failure_on_network_error(monkeypatch, tmp_path: Path):
    from standl.extractors import biostudies as bs

    def boom(*a, **kw):
        raise RuntimeError("ebi down")
    monkeypatch.setattr(bs, "_fetch_study", boom)

    partial = _ex().extract(Source(accessions=[ACC]), cache_dir=tmp_path)
    assert partial.failures


def test_extract_records_failure_on_empty_files(monkeypatch, tmp_path: Path):
    from standl.extractors import biostudies as bs
    monkeypatch.setattr(bs, "_fetch_study", lambda acc, cache_dir: FAKE_STUDY)
    monkeypatch.setattr(bs, "_fetch_files", lambda acc, cache_dir: {"data": []})

    partial = _ex().extract(Source(accessions=[ACC]), cache_dir=tmp_path)
    assert partial.failures
