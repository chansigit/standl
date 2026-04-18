"""Tests for the geo-soft extractor.

Design: feed the extractor a ``cache_dir`` that already contains a hand-
authored ``{GSE}_family.soft`` (or .gz). That bypasses the network fetch path.
One test monkeypatches ``_fetch_soft`` to simulate the "no network, no cache"
case and verifies we record a failure instead of raising.

Per the wide-in / narrow-out policy, the extractor must:
  - put every ``Sample_characteristics_ch1 = k: v`` into ``Sample.extra[k]`` verbatim;
  - NOT promote characteristics keys into canonical fields (``condition``,
    ``batch``, ``donor_id``) — that is a human-in-the-loop step via the
    ``standl`` skill;
  - surface ``Sample_supplementary_file_*`` URLs into ``sample.files``;
  - fall back to ``failures`` when a field is missing, never raise.
"""
from __future__ import annotations

import gzip
import re
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


def test_extract_supplementary_files_become_relative_paths(cache_with_soft: Path):
    """sample.files holds local relative paths under raw/, not URLs."""
    partial = _ex().extract(Source(accessions=["GSE999001"]), cache_dir=cache_with_soft)
    by_id = {s.sample_id: s for s in partial.samples}
    files = by_id["GSM999001"].files
    assert files is not None
    vals = files.value
    assert len(vals) == 3
    for rel in vals:
        assert rel.startswith("GSM999001/"), f"files must be under <sample_id>/ ({rel})"
        assert "://" not in rel, f"files must be local paths, not URLs ({rel})"


def test_extract_populates_url_map_with_source_urls(cache_with_soft: Path):
    """partial.url_map[sample_id] carries the SOFT supplementary URLs that
    modes.run will download. Lines up 1:1 with sample.files order."""
    partial = _ex().extract(Source(accessions=["GSE999001"]), cache_dir=cache_with_soft)
    urls = partial.url_map.get("GSM999001")
    assert urls is not None
    assert len(urls) == 3
    for u in urls:
        assert u.startswith(("http://", "https://", "ftp://")), f"url_map must hold URLs ({u})"
    # Order and basename alignment.
    by_id = {s.sample_id: s for s in partial.samples}
    rel = by_id["GSM999001"].files.value
    for r, u in zip(rel, urls):
        assert u.endswith(r.split("/", 1)[1]), f"{u} does not end with basename of {r}"


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


# -------- regressions against real-world GEO quirks --------

def test_none_literal_is_stripped_from_supplementary_files(tmp_path):
    """GEO writes ``!Sample_supplementary_file_1 = NONE`` when a sample has
    no sample-level processed file (data is pooled at series level).
    Observed in GSE149689. ``NONE`` must not become a URL."""
    soft = tmp_path / "GSE999001_family.soft"
    text = FIXTURE_SOFT.read_text()
    # Rewrite all real URLs for GSM999001 to the sentinel.
    text = re.sub(
        r"(!Sample_supplementary_file_\d+ = )(ftp://[^\n]+HN01_Tumor[^\n]+)",
        r"\1NONE",
        text,
    )
    soft.write_text(text)

    partial = _ex().extract(Source(accessions=["GSE999001"]), cache_dir=tmp_path)
    by_id = {s.sample_id: s for s in partial.samples}
    # GSM999001 had all three URLs replaced with NONE → no files, no url_map entry.
    assert by_id["GSM999001"].files is None
    assert "GSM999001" not in partial.url_map
    # GSM999002 untouched → still has its three URLs.
    assert by_id["GSM999002"].files is not None
    assert len(by_id["GSM999002"].files.value) == 3


def test_series_supplementary_recorded_when_samples_have_none(tmp_path):
    """If every sample has supplementary=NONE but the series has pooled
    supplementary files, record them in notes + surface as a
    ``data_layout`` failure so the user knows to split by barcode suffix."""
    soft = tmp_path / "GSE999001_family.soft"
    text = FIXTURE_SOFT.read_text()
    # Strip every sample-level supplementary URL to NONE.
    text = re.sub(
        r"(!Sample_supplementary_file_\d+ = )ftp://[^\n]+",
        r"\1NONE",
        text,
    )
    # Splice series-level files right after the SERIES block's sample_ids.
    text = text.replace(
        "!Series_sample_id = GSM999002\n",
        (
            "!Series_sample_id = GSM999002\n"
            "!Series_supplementary_file = ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE999nnn/GSE999001/suppl/GSE999001_matrix.mtx.gz\n"
            "!Series_supplementary_file = ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE999nnn/GSE999001/suppl/GSE999001_barcodes.tsv.gz\n"
        ),
    )
    soft.write_text(text)

    partial = _ex().extract(Source(accessions=["GSE999001"]), cache_dir=tmp_path)
    assert all(s.files is None for s in partial.samples)
    assert partial.url_map == {}
    assert "data_layout" in partial.failures
    assert "Series_supplementary_file" in partial.failures["data_layout"]
    assert "series_supplementary_files" in (partial.notes or "")


# -------- feature_type / library / companion heuristics --------

def test_library_stem_extracts_expected_prefixes():
    """Direct unit test for the helper — exercises the common GEO filename
    flavours (10x triplet, hash tagmtx, CellPlex multi, raw_feature_bc)."""
    from standl.extractors.geo_soft import _library_stem
    assert _library_stem("GSM8677708_exp1_spleen_l1_barcodes.tsv.gz") == "exp1_spleen_l1"
    assert _library_stem("GSM8677708_exp1_spleen_l1_features.tsv.gz") == "exp1_spleen_l1"
    assert _library_stem("GSM8677708_exp1_spleen_l1_matrix.mtx.gz") == "exp1_spleen_l1"
    assert _library_stem("GSM8677712_exp1_spleen_l1_hash_tagmtx.csv.gz") == "exp1_spleen_l1"
    assert _library_stem("GSM8677714_exp2_epor_pos_multi_tagmtx.csv.gz") == "exp2_epor_pos"
    # Real-world CellRanger h5: GSM prefix is the entire stem after the
    # canonical bc_matrix suffix strips.
    assert _library_stem("GSM4138110_sample_a_raw_feature_bc_matrix.h5") == "sample_a"
    assert _library_stem("nonsense.txt") is None


def test_feature_type_classifier():
    from standl.extractors.geo_soft import _looks_like_feature_barcoding
    assert _looks_like_feature_barcoding("Untreated spleen, HASH", []) is True
    assert _looks_like_feature_barcoding("Sample, MULTI", []) is True
    assert _looks_like_feature_barcoding("scRNA-seq", ["foo_hash_tagmtx.csv.gz"]) is True
    assert _looks_like_feature_barcoding("scRNA-seq", []) is False
    # "multiplexed" in title should NOT fire on the plain word.
    assert _looks_like_feature_barcoding("multiplexed donors, scRNA-seq", []) is False
    # Pure 10x triplet → no match.
    assert _looks_like_feature_barcoding("PBMC 10x", ["sample_barcodes.tsv.gz"]) is False
    # V(D)J immune-repertoire: BCR / TCR / VDJ are feature modalities too.
    assert _looks_like_feature_barcoding("C12_PBMC_10x_BCR", []) is True
    assert _looks_like_feature_barcoding("C12_PBMC_10x_TCR", []) is True
    assert _looks_like_feature_barcoding("sample VDJ enrichment", []) is True


def test_library_stem_falls_back_to_title_modality(tmp_path):
    """When filenames don't follow a canonical suffix (real-world
    submitter variation), Sample_title patterns like
    ``C12_R_10x_scRNA`` / ``C12_R_10x_BCR`` let us recover the library
    stem. Emulates GSE125527's naming."""
    from standl.extractors.geo_soft import _annotate_feature_types_and_libraries
    from standl.schema import PartialSample, ProvenancedValue

    def _pv(v, ev):
        return ProvenancedValue(value=v, source="geo-soft", confidence=0.9, evidence=ev)

    gex = PartialSample(sample_id="GSM1")
    gex.extra["title"] = _pv("C12_R_10x_scRNA", "Sample_title")
    bcr = PartialSample(sample_id="GSM2")
    bcr.extra["title"] = _pv("C12_R_10x_BCR", "Sample_title")
    other = PartialSample(sample_id="GSM3")
    other.extra["title"] = _pv("D5_PBMC_10x_scRNA", "Sample_title")

    url_map = {"GSM1": ["GSM1_weird-name.tsv.gz"], "GSM2": ["GSM2_weird-bcr.tsv.gz"],
               "GSM3": ["GSM3_weird-name.tsv.gz"]}

    _annotate_feature_types_and_libraries([gex, bcr, other], url_map)

    assert gex.extra["library"].value == "C12_R"
    assert bcr.extra["library"].value == "C12_R"
    assert other.extra["library"].value == "D5_PBMC"
    # BCR now classified as feature_barcoding.
    assert bcr.extra["feature_type"].value == "feature_barcoding"
    # GEX + BCR share stem → companion link wired up.
    assert gex.extra["companion_samples"].value == "GSM2"
    assert bcr.extra["companion_of"].value == "GSM1"


def test_extract_annotates_feature_type_and_companion_links(tmp_path):
    """Synthesize a SOFT where two GSMs share library stem ``exp1_l1``
    but one is GEX and the other HASH — companion cross-refs must land
    on Sample.extra for both."""
    soft = tmp_path / "GSE999001_family.soft"
    text = FIXTURE_SOFT.read_text()

    # Rewrite the 2 fixture samples: GSM999001 becomes GEX with a new
    # library stem; GSM999002 becomes its HASH companion.
    text = re.sub(
        r"(!Sample_title = )[^\n]+\n(!Sample_geo_accession = GSM999001)",
        r"\1Spleen exp1_l1, scRNA-seq\n\2", text,
    )
    text = re.sub(
        r"(!Sample_title = )[^\n]+\n(!Sample_geo_accession = GSM999002)",
        r"\1Spleen exp1_l1, HASH\n\2", text,
    )
    text = re.sub(
        r"ftp://ftp\.ncbi\.nlm\.nih\.gov/geo/samples/GSM999nnn/GSM999001/suppl/GSM999001_HN01_Tumor_(barcodes|features|matrix)\.(tsv|mtx)\.gz",
        r"ftp://ftp.ncbi.nlm.nih.gov/geo/samples/GSM999nnn/GSM999001/suppl/GSM999001_exp1_l1_\1.\2.gz",
        text,
    )
    # Replace all 3 of GSM999002's supplementary files with a single HASH CSV.
    text = re.sub(
        r"(!Sample_supplementary_file_1 = )ftp://[^\n]+GSM999002[^\n]+",
        r"\1ftp://ftp.ncbi.nlm.nih.gov/geo/samples/GSM999nnn/GSM999002/suppl/GSM999002_exp1_l1_hash_tagmtx.csv.gz",
        text,
    )
    text = re.sub(
        r"!Sample_supplementary_file_[23] = ftp://[^\n]+GSM999002[^\n]+\n",
        "",
        text,
    )
    soft.write_text(text)

    partial = _ex().extract(Source(accessions=["GSE999001"]), cache_dir=tmp_path)
    by_id = {s.sample_id: s for s in partial.samples}

    gex, hash_s = by_id["GSM999001"], by_id["GSM999002"]
    assert gex.extra["feature_type"].value == "gene_expression"
    assert hash_s.extra["feature_type"].value == "feature_barcoding"

    assert gex.extra["library"].value == "exp1_l1"
    assert hash_s.extra["library"].value == "exp1_l1"

    # Cross-refs populated.
    assert hash_s.extra["companion_of"].value == "GSM999001"
    assert gex.extra["companion_samples"].value == "GSM999002"


def test_extract_skips_companion_link_when_no_feature_pair(tmp_path):
    """If both samples in a library are GEX (no HASH) — no companion
    records, just the library/feature_type annotations."""
    partial = _ex().extract(
        Source(accessions=["GSE999001"]),
        cache_dir=Path(__file__).parent / "fixtures" / "geo",
    )
    for s in partial.samples:
        assert s.extra["feature_type"].value == "gene_expression"
        assert "companion_of" not in s.extra
        assert "companion_samples" not in s.extra


def test_sample_files_filtered_but_other_samples_keep_urls(tmp_path):
    """Mixed case: some samples NONE, some with real URLs — the non-NONE
    samples are unaffected."""
    soft = tmp_path / "GSE999001_family.soft"
    text = FIXTURE_SOFT.read_text()
    # Only GSM999001's supp lines become NONE.
    text = re.sub(
        r"(!Sample_supplementary_file_\d+ = )ftp://[^\n]+GSM999001[^\n]+",
        r"\1NONE",
        text,
    )
    soft.write_text(text)

    partial = _ex().extract(Source(accessions=["GSE999001"]), cache_dir=tmp_path)
    # GSM999001 empty, GSM999002 still intact — so samples overall DO have files,
    # which means the series-level ``data_layout`` branch should NOT fire.
    assert "data_layout" not in partial.failures
