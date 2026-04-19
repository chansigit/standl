"""Tests for the gsa-cncb extractor."""
from __future__ import annotations

from pathlib import Path

import pytest

from standl.schema import Source

ACC = "CRA000126"

# Trimmed-down real browse HTML: panel header + two experiment rows.
FAKE_BROWSE_HTML = '''
<html><body>
<div class="panel-heading">CRA000126 基本信息</div>
<div class="panel-body">
  <b>标题:</b> </span><span>Single-cell analysis of hepatocellular carcinoma</span>
</div>
<table>
  <tr class="experiment">
    <td class="experiments"><a href="browse/CRA000126/CRX007544">CRX007544</a></td>
    <td>T4144</td>
    <td><a href="https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?id=9606">Homo sapiens</a></td>
  </tr>
  <tr class="experiment">
    <td class="experiments"><a href="browse/CRA000126/CRX007543">CRX007543</a></td>
    <td>T4067</td>
    <td><a href="https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?id=9606">Homo sapiens</a></td>
  </tr>
</table>
</body></html>
'''


def _ex():
    from standl.extractors.gsa_cncb import GSACNCBExtractor
    return GSACNCBExtractor()


# -------- can_handle --------

def test_can_handle_cra_accession():
    assert _ex().can_handle(Source(accessions=["CRA000126"])) >= 0.8


def test_can_handle_hra_accession():
    assert _ex().can_handle(Source(accessions=["HRA009872"])) >= 0.8


def test_can_handle_omix_and_prjca():
    assert _ex().can_handle(Source(accessions=["OMIX123456"])) >= 0.8
    assert _ex().can_handle(Source(accessions=["PRJCA000007"])) >= 0.8


def test_can_handle_browse_url():
    url = "https://ngdc.cncb.ac.cn/gsa/browse/CRA016814"
    assert _ex().can_handle(Source(paper_url=url)) >= 0.8


def test_can_handle_zero_for_geo():
    assert _ex().can_handle(Source(accessions=["GSE139324"])) == 0.0


def test_can_handle_zero_for_unrelated():
    assert _ex().can_handle(Source(paper_doi="10.1038/xyz")) == 0.0


# -------- extract: CRA path --------

def test_extract_cra_scrapes_browse_and_emits_data_format_failure(monkeypatch, tmp_path: Path):
    """CRA is fastq-only; extractor must emit structured metadata AND a
    ``data_format`` failure so modes.run elevates to FAIL."""
    from standl.extractors import gsa_cncb as g
    monkeypatch.setattr(g, "_fetch_browse_html", lambda acc, cache_dir: FAKE_BROWSE_HTML)

    partial = _ex().extract(Source(accessions=[ACC]), cache_dir=tmp_path)

    assert partial.extractor == "gsa-cncb"
    assert partial.dataset_id == ACC
    assert "data_format" in partial.failures
    assert "fastq" in partial.failures["data_format"].lower()

    # Organism resolved from taxon id.
    assert partial.organism is not None
    assert partial.organism.value == "Homo sapiens"

    # Two experiments parsed; each becomes a PartialSample keyed by CRX.
    ids = sorted(s.sample_id for s in partial.samples)
    assert ids == ["CRX007543", "CRX007544"]

    by_id = {s.sample_id: s for s in partial.samples}
    assert by_id["CRX007544"].extra["sample_label"].value == "T4144"
    assert by_id["CRX007544"].extra["parent_study"].value == ACC
    assert by_id["CRX007544"].extra["taxon_id"].value == "9606"


def test_extract_cra_title_in_notes(monkeypatch, tmp_path: Path):
    from standl.extractors import gsa_cncb as g
    monkeypatch.setattr(g, "_fetch_browse_html", lambda acc, cache_dir: FAKE_BROWSE_HTML)

    partial = _ex().extract(Source(accessions=[ACC]), cache_dir=tmp_path)
    assert partial.notes is not None
    assert "hepatocellular" in partial.notes.lower()


def test_extract_cra_when_browse_fetch_fails(monkeypatch, tmp_path: Path):
    from standl.extractors import gsa_cncb as g
    def boom(acc, cache_dir):
        raise RuntimeError("network down")
    monkeypatch.setattr(g, "_fetch_browse_html", boom)

    partial = _ex().extract(Source(accessions=[ACC]), cache_dir=tmp_path)
    assert "browse_fetch" in partial.failures


def test_extract_cra_parses_with_no_experiments(monkeypatch, tmp_path: Path):
    """Empty-experiment HTML (rare, page drift etc.) — still emit a
    study-level PartialSample with the study_data_format failure."""
    from standl.extractors import gsa_cncb as g
    minimal = '<html><body>CRA000126 has no experiments</body></html>'
    monkeypatch.setattr(g, "_fetch_browse_html", lambda acc, cache_dir: minimal)

    partial = _ex().extract(Source(accessions=[ACC]), cache_dir=tmp_path)
    assert len(partial.samples) == 1
    assert partial.samples[0].sample_id == ACC
    assert "data_format" in partial.failures


# -------- extract: HRA path (no fetch, controlled-access failure) --------

def test_extract_hra_emits_data_access_failure_without_fetch(tmp_path: Path):
    """HRA is DAC-gated; the extractor must NOT even try the browse URL
    (would be misleading UX) and emit a clear data_access failure with
    the application landing URL."""
    partial = _ex().extract(Source(accessions=["HRA009872"]), cache_dir=tmp_path)
    assert "data_access" in partial.failures
    assert "DAC" in partial.failures["data_access"] or "controlled" in partial.failures["data_access"].lower()
    # landing URL recorded so the skill layer can link users to the application.
    assert partial.samples[0].extra["landing_url"].value.startswith("https://ngdc.cncb.ac.cn/gsa-human/browse/HRA009872")


# -------- OMIX unsupported for now --------

def test_extract_omix_is_unsupported(tmp_path: Path):
    partial = _ex().extract(Source(accessions=["OMIX001234"]), cache_dir=tmp_path)
    assert "unsupported_accession" in partial.failures


# -------- no accession --------

def test_extract_records_failure_when_no_accession(tmp_path: Path):
    partial = _ex().extract(Source(paper_url="https://ngdc.cncb.ac.cn/"), cache_dir=tmp_path)
    assert "accession" in partial.failures
