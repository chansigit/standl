"""Tests for modes.validate + the audit record / markdown renderer.

Each of the 9 checks in ``modes.validate`` docstring gets at least one
positive assertion (passes on the good fixture) and one negative assertion
(fails when we deliberately break that check via a mutated tmp copy).

Fixture layout: see ``tests/fixtures/validate_good``. Tests copy it to
``tmp_path`` and mutate their private copy.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

FIXTURE = Path(__file__).parent / "fixtures" / "validate_good"


# -------- fixtures --------

@pytest.fixture
def good(tmp_path: Path) -> Path:
    dst = tmp_path / "ds"
    shutil.copytree(FIXTURE, dst)
    return dst


def _load_design(p: Path) -> dict:
    return yaml.safe_load((p / "design.yaml").read_text())


def _dump_design(p: Path, d: dict) -> None:
    (p / "design.yaml").write_text(yaml.safe_dump(d, sort_keys=False))


def _load_manifest(p: Path) -> dict:
    return json.loads((p / "manifest.json").read_text())


def _dump_manifest(p: Path, m: dict) -> None:
    (p / "manifest.json").write_text(json.dumps(m, indent=2) + "\n")


# -------- API + happy path --------

def test_validate_returns_report_and_writes_audit_md(good: Path):
    from standl.modes import validate
    report = validate(good)
    assert report.dataset_id == "GSE_FIXTURE_GOOD"
    assert (good / "audit.md").exists()


def test_good_fixture_has_no_failures(good: Path):
    from standl.modes import validate
    report = validate(good)
    failed = [r for r in report.records if r.status == "fail"]
    assert failed == [], f"unexpected failures on good fixture: {failed}"
    assert report.worst_severity() in ("ok", "warn")


def test_audit_markdown_mentions_dataset_id_and_checks(good: Path):
    from standl.modes import validate
    validate(good)
    md = (good / "audit.md").read_text()
    assert "GSE_FIXTURE_GOOD" in md
    # The renderer should list each check by name at least once.
    for name in (
        "files_in_manifest", "files_on_disk", "no_orphan_raw",
        "sample_id_valid", "contrasts_valid", "no_confound",
        "ontology_format",
    ):
        assert name in md, f"check {name!r} not rendered in audit.md"


# -------- check 1: files_in_manifest --------

def test_files_in_manifest_fails_when_entry_missing(good: Path):
    """Sample lists a file that has no manifest entry."""
    from standl.modes import validate
    m = _load_manifest(good)
    m["entries"] = [e for e in m["entries"] if e["path"] != "HN01_Tumor/matrix.mtx.gz"]
    _dump_manifest(good, m)

    report = validate(good)
    recs = [r for r in report.records if r.check == "files_in_manifest" and r.status == "fail"]
    assert recs, "expected fail on files_in_manifest when entry removed"
    assert any("HN01_Tumor/matrix.mtx.gz" in (r.message + str(r.evidence or "")) for r in recs)


def test_files_in_manifest_fails_when_status_not_ok(good: Path):
    """Manifest has an entry but its status is 'missing', not 'ok'."""
    from standl.modes import validate
    m = _load_manifest(good)
    for e in m["entries"]:
        if e["path"] == "HN01_Tumor/matrix.mtx.gz":
            e["status"] = "missing"
    _dump_manifest(good, m)

    report = validate(good)
    assert any(
        r.check == "files_in_manifest" and r.status == "fail"
        for r in report.records
    )


# -------- check 2: files_on_disk --------

def test_files_on_disk_fails_on_missing_file(good: Path):
    from standl.modes import validate
    (good / "raw" / "HN01_Tumor" / "matrix.mtx.gz").unlink()

    report = validate(good)
    assert any(
        r.check == "files_on_disk" and r.status == "fail"
        for r in report.records
    )


def test_files_on_disk_fails_on_size_mismatch(good: Path):
    from standl.modes import validate
    target = good / "raw" / "HN01_Tumor" / "matrix.mtx.gz"
    target.write_bytes(b"a different payload with a different length\n")

    report = validate(good)
    assert any(
        r.check == "files_on_disk" and r.status == "fail"
        for r in report.records
    )


def test_files_on_disk_deep_catches_sha256_drift(good: Path):
    """Same size, different bytes: shallow check passes, deep check fails."""
    from standl.modes import validate
    target = good / "raw" / "HN01_Tumor" / "matrix.mtx.gz"
    original = target.read_bytes()
    # Same length, different content.
    replacement = bytes(c ^ 0x01 for c in original)
    assert len(replacement) == len(original)
    target.write_bytes(replacement)

    shallow = validate(good, deep=False)
    assert all(
        r.check != "files_on_disk" or r.status == "ok"
        for r in shallow.records
    ), "shallow check should miss same-size byte drift"

    deep = validate(good, deep=True)
    assert any(
        r.check == "files_on_disk" and r.status == "fail"
        for r in deep.records
    ), "deep check should detect same-size byte drift"


# -------- check 3: no_orphan_raw --------

def test_no_orphan_raw_fails_on_untracked_file(good: Path):
    from standl.modes import validate
    (good / "raw" / "orphan.txt").write_text("nobody refers to me\n")

    report = validate(good)
    recs = [r for r in report.records if r.check == "no_orphan_raw" and r.status == "fail"]
    assert recs
    assert any("orphan.txt" in (r.message + str(r.evidence or "")) for r in recs)


# -------- check 4: sample_id_valid --------

def test_sample_id_valid_fails_on_duplicate(good: Path):
    from standl.modes import validate
    d = _load_design(good)
    d["samples"][1]["sample_id"] = d["samples"][0]["sample_id"]
    _dump_design(good, d)

    report = validate(good)
    assert any(
        r.check == "sample_id_valid" and r.status == "fail"
        for r in report.records
    )


def test_sample_id_valid_fails_on_unsafe_chars(good: Path):
    from standl.modes import validate
    d = _load_design(good)
    d["samples"][0]["sample_id"] = "bad/../id"
    _dump_design(good, d)

    report = validate(good)
    assert any(
        r.check == "sample_id_valid" and r.status == "fail"
        for r in report.records
    )


# -------- check 5: contrasts_valid --------

def test_contrasts_valid_fails_on_undeclared_factor(good: Path):
    from standl.modes import validate
    d = _load_design(good)
    d["contrasts"][0]["numerator"] = {"ghost_factor": "x"}
    _dump_design(good, d)

    report = validate(good)
    assert any(
        r.check == "contrasts_valid" and r.status == "fail"
        for r in report.records
    )


def test_contrasts_valid_fails_on_undeclared_level(good: Path):
    from standl.modes import validate
    d = _load_design(good)
    d["contrasts"][0]["numerator"] = {"condition": "not_a_level"}
    _dump_design(good, d)

    report = validate(good)
    assert any(
        r.check == "contrasts_valid" and r.status == "fail"
        for r in report.records
    )


# -------- check 6: no_confound (warn only) --------

def test_no_confound_warns_when_condition_perfectly_tracks_donor(good: Path):
    from standl.modes import validate
    d = _load_design(good)
    # Two samples, two donors, two conditions — perfect confound.
    d["samples"][0]["donor_id"] = "D1"
    d["samples"][0]["condition"] = "A"
    d["samples"][1]["donor_id"] = "D2"
    d["samples"][1]["condition"] = "B"
    _dump_design(good, d)

    report = validate(good)
    recs = [r for r in report.records if r.check == "no_confound"]
    assert recs
    # warn-only — must not fail.
    assert all(r.status in ("ok", "warn") for r in recs)
    assert any(r.status == "warn" for r in recs)


def test_no_confound_is_ok_when_condition_varies_within_batch(good: Path):
    """Good fixture already has two samples sharing donor HN01 with different conditions."""
    from standl.modes import validate
    report = validate(good)
    recs = [r for r in report.records if r.check == "no_confound"]
    assert recs
    assert all(r.status == "ok" for r in recs)


# -------- check 7: ontology_format --------

def test_ontology_format_ok_for_valid_uberon(good: Path):
    from standl.modes import validate
    report = validate(good)
    recs = [r for r in report.records if r.check == "ontology_format"]
    assert recs
    assert all(r.status == "ok" for r in recs)


def test_ontology_format_fails_on_bad_prefix(good: Path):
    from standl.modes import validate
    d = _load_design(good)
    d["samples"][0]["tissue_ontology"] = "UBERON_0002107"  # underscore, not colon
    _dump_design(good, d)

    report = validate(good)
    assert any(
        r.check == "ontology_format" and r.status == "fail"
        for r in report.records
    )


# -------- check 8: h5ad_samples_match --------

def test_h5ad_samples_match_ok(good: Path, tmp_path: Path, make_h5ad):
    pytest.importorskip("anndata")
    from standl.modes import validate
    h5ad = make_h5ad(tmp_path / "data.h5ad", ["HN01_Tumor", "HN01_PBL"])

    report = validate(good, h5ad=h5ad)
    recs = [r for r in report.records if r.check == "h5ad_samples_match"]
    assert recs
    assert all(r.status == "ok" for r in recs)


def test_h5ad_samples_match_fails_on_missing_sample(good: Path, tmp_path: Path, make_h5ad):
    pytest.importorskip("anndata")
    from standl.modes import validate
    # design has 2 samples, h5ad only sees 1 of them.
    h5ad = make_h5ad(tmp_path / "data.h5ad", ["HN01_Tumor"])

    report = validate(good, h5ad=h5ad)
    assert any(
        r.check == "h5ad_samples_match" and r.status == "fail"
        for r in report.records
    )


# -------- check 9: h5ad_cell_count --------

def test_h5ad_cell_count_skipped_without_expected(good: Path, tmp_path: Path, make_h5ad):
    """Without a paper-stated count, check should be 'ok' with a skip note."""
    pytest.importorskip("anndata")
    from standl.modes import validate
    h5ad = make_h5ad(tmp_path / "data.h5ad", ["HN01_Tumor", "HN01_PBL"])

    report = validate(good, h5ad=h5ad)
    recs = [r for r in report.records if r.check == "h5ad_cell_count"]
    assert recs
    assert all(r.status == "ok" for r in recs)


def test_h5ad_cell_count_fails_when_off(good: Path, tmp_path: Path, make_h5ad):
    """When caller supplies expected count, check should fire."""
    pytest.importorskip("anndata")
    from standl.modes import validate
    h5ad = make_h5ad(tmp_path / "data.h5ad", ["HN01_Tumor", "HN01_PBL"], cells_per_sample=5)
    # 10 cells total; expected 10_000 → way off.
    report = validate(good, h5ad=h5ad, expected_cell_count=10_000, cell_count_tolerance=0.1)
    assert any(
        r.check == "h5ad_cell_count" and r.status == "fail"
        for r in report.records
    )


def test_h5ad_cell_count_ok_within_tolerance(good: Path, tmp_path: Path, make_h5ad):
    pytest.importorskip("anndata")
    from standl.modes import validate
    h5ad = make_h5ad(tmp_path / "data.h5ad", ["HN01_Tumor", "HN01_PBL"], cells_per_sample=5)
    report = validate(good, h5ad=h5ad, expected_cell_count=11, cell_count_tolerance=0.2)
    recs = [r for r in report.records if r.check == "h5ad_cell_count"]
    assert recs
    assert all(r.status == "ok" for r in recs)
