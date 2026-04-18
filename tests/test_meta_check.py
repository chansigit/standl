"""Tests for modes.meta_check — read-only metadata reconciliation.

meta_check builds a PartialDesign from an existing design.yaml (promoted as
"manual"), an h5ad (via the h5ad-observed extractor), and any paper source
extractors that fire. It merges them, then surfaces disagreement via
``sources_disagree`` records.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

FIXTURE = Path(__file__).parent / "fixtures" / "validate_good"


@pytest.fixture
def good(tmp_path: Path) -> Path:
    dst = tmp_path / "ds"
    shutil.copytree(FIXTURE, dst)
    return dst


def _load_design(p: Path) -> dict:
    return yaml.safe_load((p / "design.yaml").read_text())


def _dump_design(p: Path, d: dict) -> None:
    (p / "design.yaml").write_text(yaml.safe_dump(d, sort_keys=False))


# -------- API + smoke --------

def test_meta_check_returns_report_and_writes_audit_md(good: Path, tmp_path: Path, make_h5ad):
    pytest.importorskip("anndata")
    from standl.modes import meta_check
    h5ad = make_h5ad(tmp_path / "data.h5ad", ["HN01_Tumor", "HN01_PBL"])

    report = meta_check(good, h5ad=h5ad)
    assert report.dataset_id == "GSE_FIXTURE_GOOD"
    assert (good / "audit.md").exists()


# -------- the roadmap test: N-1 samples in h5ad --------

def test_meta_check_flags_missing_sample_in_h5ad(good: Path, tmp_path: Path, make_h5ad):
    """Design claims 2 samples; h5ad only has 1 → fail on h5ad_samples_match."""
    pytest.importorskip("anndata")
    from standl.modes import meta_check
    h5ad = make_h5ad(tmp_path / "data.h5ad", ["HN01_Tumor"])  # HN01_PBL missing

    report = meta_check(good, h5ad=h5ad)
    assert any(
        r.check == "h5ad_samples_match" and r.status == "fail"
        for r in report.records
    )


def test_meta_check_flags_extra_sample_in_h5ad(good: Path, tmp_path: Path, make_h5ad):
    """h5ad has an extra sample not declared in design."""
    pytest.importorskip("anndata")
    from standl.modes import meta_check
    h5ad = make_h5ad(
        tmp_path / "data.h5ad",
        ["HN01_Tumor", "HN01_PBL", "HN02_Tumor"],  # extra
    )

    report = meta_check(good, h5ad=h5ad)
    assert any(
        r.check == "h5ad_samples_match" and r.status == "fail"
        for r in report.records
    )


# -------- field-level disagreement --------

def test_meta_check_surfaces_field_disagreement(good: Path, tmp_path: Path, make_h5ad):
    """Design says HN01_Tumor.condition=tumor; h5ad obs says condition=control
    for the same sample_id → must emit a sources_disagree warn."""
    pytest.importorskip("anndata")
    from standl.modes import meta_check
    h5ad = make_h5ad(
        tmp_path / "data.h5ad",
        ["HN01_Tumor", "HN01_PBL"],
        cells_per_sample=2,
        obs_cols={
            # Bogus condition for HN01_Tumor (design says "tumor"), matching for PBL.
            "condition": ["control", "control", "pbl", "pbl"],
        },
    )

    report = meta_check(good, h5ad=h5ad)
    disagree = [r for r in report.records if r.check == "sources_disagree"]
    assert disagree, "expected sources_disagree record when observed condition != design"
    # The disagreement should mention HN01_Tumor + condition.
    assert any(
        "HN01_Tumor" in (r.message + str(r.evidence or "")) and
        "condition" in (r.message + str(r.evidence or ""))
        for r in disagree
    )


def test_meta_check_no_disagreement_when_observed_matches_design(
    good: Path, tmp_path: Path, make_h5ad,
):
    pytest.importorskip("anndata")
    from standl.modes import meta_check
    # Conditions match what design.yaml declares.
    h5ad = make_h5ad(
        tmp_path / "data.h5ad",
        ["HN01_Tumor", "HN01_PBL"],
        cells_per_sample=2,
        obs_cols={"condition": ["tumor", "tumor", "pbl", "pbl"]},
    )
    report = meta_check(good, h5ad=h5ad)
    # Either no sources_disagree records, or they're all ok.
    disagree_fails = [
        r for r in report.records
        if r.check == "sources_disagree" and r.status != "ok"
    ]
    assert not disagree_fails


# -------- read-only contract --------

def test_meta_check_does_not_overwrite_design_yaml_by_default(good: Path, tmp_path: Path, make_h5ad):
    pytest.importorskip("anndata")
    from standl.modes import meta_check
    original = (good / "design.yaml").read_text()
    h5ad = make_h5ad(tmp_path / "data.h5ad", ["HN01_Tumor", "HN01_PBL"])

    meta_check(good, h5ad=h5ad)

    assert (good / "design.yaml").read_text() == original, \
        "meta_check must be read-only w.r.t. design.yaml unless opted in"
    assert not (good / "provenance.json").exists()


def test_meta_check_writes_merged_design_when_opted_in(good: Path, tmp_path: Path, make_h5ad):
    pytest.importorskip("anndata")
    from standl.modes import meta_check
    h5ad = make_h5ad(tmp_path / "data.h5ad", ["HN01_Tumor", "HN01_PBL"])

    meta_check(good, h5ad=h5ad, write_design=True)

    assert (good / "provenance.json").exists()
    prov = json.loads((good / "provenance.json").read_text())
    assert prov["dataset_id"] == "GSE_FIXTURE_GOOD"


# -------- no existing design --------

def test_meta_check_runs_without_existing_design_yaml(tmp_path: Path, make_h5ad):
    """An empty dataset dir + an h5ad should still produce an audit report."""
    pytest.importorskip("anndata")
    from standl.modes import meta_check
    ds = tmp_path / "fresh"
    ds.mkdir()
    h5ad = make_h5ad(tmp_path / "data.h5ad", ["X", "Y"])

    report = meta_check(ds, h5ad=h5ad)
    assert report.records
    # dataset_id falls back to the dir name when no source provides one.
    assert report.dataset_id == "fresh"


# -------- paper extractor best-effort --------

def test_meta_check_records_unimplemented_paper_extractors(good: Path, tmp_path: Path, make_h5ad):
    """If paper_source triggers an extractor that's still a stub, we must
    record a warn and keep going — not abort the run."""
    pytest.importorskip("anndata")
    from standl.modes import meta_check
    from standl.schema import Source
    h5ad = make_h5ad(tmp_path / "data.h5ad", ["HN01_Tumor", "HN01_PBL"])

    # GEO accession makes geo-soft fire; geo-soft.extract raises NotImplementedError.
    report = meta_check(good, h5ad=h5ad, paper_source=Source(accessions=["GSE_FIXTURE_GOOD"]))
    assert any(
        r.check == "paper_extractor_skipped" and "geo-soft" in r.message
        for r in report.records
    )


# -------- nothing to work with --------

def test_meta_check_flags_when_no_sources_available(tmp_path: Path):
    """Empty dir, no h5ad, no paper_source → fail (nothing to reconcile)."""
    from standl.modes import meta_check
    ds = tmp_path / "empty"
    ds.mkdir()
    report = meta_check(ds)
    assert any(r.status == "fail" for r in report.records)
