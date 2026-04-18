"""Unit tests for the h5ad-observed extractor.

h5ad-observed is the only extractor in the registry that "extracts" from
local data (not a paper / URL / accession). It advertises via
``Source.local_h5ad`` and must stay tolerant of missing columns — not raise.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from standl.schema import Source


def _ex():
    from standl.extractors.h5ad_observed import H5ADObservedExtractor
    return H5ADObservedExtractor()


# -------- can_handle --------

def test_can_handle_local_h5ad(tmp_path: Path, make_h5ad):
    pytest.importorskip("anndata")
    h5ad = make_h5ad(tmp_path / "x.h5ad", ["S1"])
    assert _ex().can_handle(Source(local_h5ad=h5ad)) > 0.5


def test_can_handle_zero_without_local_h5ad():
    assert _ex().can_handle(Source(paper_url="https://example.com/paper")) == 0.0
    assert _ex().can_handle(Source(accessions=["GSE123"])) == 0.0


def test_can_handle_zero_when_path_missing(tmp_path: Path):
    """A Source pointing at a non-existent file — don't fire."""
    missing = tmp_path / "does_not_exist.h5ad"
    assert _ex().can_handle(Source(local_h5ad=missing)) == 0.0


# -------- extract: sample grouping --------

def test_extract_produces_one_partial_sample_per_unique_sample(tmp_path: Path, make_h5ad):
    pytest.importorskip("anndata")
    h5ad = make_h5ad(tmp_path / "x.h5ad", ["S1", "S2"], cells_per_sample=4)
    partial = _ex().extract(Source(local_h5ad=h5ad), cache_dir=tmp_path)

    assert partial.extractor == "h5ad-observed"
    assert {s.sample_id for s in partial.samples} == {"S1", "S2"}


# -------- extract: canonical field promotion --------

def test_extract_promotes_condition_when_constant_per_sample(tmp_path: Path, make_h5ad):
    pytest.importorskip("anndata")
    h5ad = make_h5ad(
        tmp_path / "x.h5ad",
        ["S1", "S2"],
        cells_per_sample=2,
        obs_cols={"condition": ["tumor", "tumor", "pbl", "pbl"]},
    )
    partial = _ex().extract(Source(local_h5ad=h5ad), cache_dir=tmp_path)
    by_id = {s.sample_id: s for s in partial.samples}
    assert by_id["S1"].condition is not None
    assert by_id["S1"].condition.value == "tumor"
    assert by_id["S2"].condition is not None
    assert by_id["S2"].condition.value == "pbl"


def test_extract_promotes_donor_id_from_common_aliases(tmp_path: Path, make_h5ad):
    pytest.importorskip("anndata")
    h5ad = make_h5ad(
        tmp_path / "x.h5ad",
        ["S1", "S2"],
        cells_per_sample=2,
        obs_cols={"donor": ["D1", "D1", "D2", "D2"]},
    )
    partial = _ex().extract(Source(local_h5ad=h5ad), cache_dir=tmp_path)
    by_id = {s.sample_id: s for s in partial.samples}
    assert by_id["S1"].donor_id is not None
    assert by_id["S1"].donor_id.value == "D1"


def test_extract_does_not_promote_when_values_vary_within_sample(tmp_path: Path, make_h5ad):
    """If condition varies cell-to-cell within a single sample_id, we can't
    attribute a sample-level condition — skip promotion."""
    pytest.importorskip("anndata")
    h5ad = make_h5ad(
        tmp_path / "x.h5ad",
        ["S1"],
        cells_per_sample=4,
        obs_cols={"condition": ["tumor", "pbl", "tumor", "pbl"]},
    )
    partial = _ex().extract(Source(local_h5ad=h5ad), cache_dir=tmp_path)
    s1 = partial.samples[0]
    assert s1.condition is None


def test_extract_unknown_columns_land_in_extra(tmp_path: Path, make_h5ad):
    pytest.importorskip("anndata")
    h5ad = make_h5ad(
        tmp_path / "x.h5ad",
        ["S1"],
        cells_per_sample=2,
        obs_cols={"lab_internal_tag": ["abc", "abc"]},
    )
    partial = _ex().extract(Source(local_h5ad=h5ad), cache_dir=tmp_path)
    s1 = partial.samples[0]
    assert "lab_internal_tag" in s1.extra
    assert s1.extra["lab_internal_tag"].value == "abc"


# -------- extract: failure recording --------

def test_extract_records_failure_when_no_sample_column(tmp_path: Path):
    """h5ad has no ``obs['sample']`` or ``obs['sample_id']`` → record, don't raise."""
    pytest.importorskip("anndata")
    import anndata as ad
    import numpy as np
    import pandas as pd

    X = np.zeros((3, 2), dtype="float32")
    obs = pd.DataFrame({"unrelated": ["a", "b", "c"]})
    a = ad.AnnData(X=X, obs=obs)
    p = tmp_path / "no_sample_col.h5ad"
    a.write_h5ad(p)

    partial = _ex().extract(Source(local_h5ad=p), cache_dir=tmp_path)
    assert partial.samples == []
    assert "samples" in partial.failures


# -------- extract: organism / assay from uns --------

def test_extract_picks_up_organism_from_uns(tmp_path: Path, make_h5ad):
    pytest.importorskip("anndata")
    h5ad = make_h5ad(
        tmp_path / "x.h5ad",
        ["S1"],
        cells_per_sample=2,
        uns={"organism": "Homo sapiens"},
    )
    partial = _ex().extract(Source(local_h5ad=h5ad), cache_dir=tmp_path)
    assert partial.organism is not None
    assert partial.organism.value == "Homo sapiens"
