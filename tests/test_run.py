"""Integration tests for modes.run.

Strategy: pre-seed ``<out_dir>/paper/GSE999001_family.soft`` with the fixture
SOFT file whose supplementary URLs have been rewritten to point at a local
HTTP server fixture. geo-soft then extracts those URLs, fetch streams them
off the local server, and validate reconciles everything.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

FIXTURE_SOFT = Path(__file__).parent / "fixtures" / "geo" / "GSE999001_family.soft"
_SUPP_URL_PREFIX = re.compile(
    r"ftp://ftp\.ncbi\.nlm\.nih\.gov/geo/samples/[^/]+/[^/]+/suppl/"
)
_SUPP_BASENAMES = [
    "GSM999001_HN01_Tumor_matrix.mtx.gz",
    "GSM999001_HN01_Tumor_barcodes.tsv.gz",
    "GSM999001_HN01_Tumor_features.tsv.gz",
    "GSM999002_HN01_PBL_matrix.mtx.gz",
    "GSM999002_HN01_PBL_barcodes.tsv.gz",
    "GSM999002_HN01_PBL_features.tsv.gz",
]


def _rewrite_soft(base_url: str) -> str:
    return _SUPP_URL_PREFIX.sub(f"{base_url}/", FIXTURE_SOFT.read_text())


def _seed_paper_cache(out_dir: Path, rewritten_soft: str) -> None:
    paper = out_dir / "paper"
    paper.mkdir(parents=True, exist_ok=True)
    (paper / "GSE999001_family.soft").write_text(rewritten_soft)


def _populate_server(server_root: Path, payload: bytes = b"standl fake payload\n") -> None:
    for name in _SUPP_BASENAMES:
        (server_root / name).write_bytes(payload)


# -------- happy path --------

def test_run_produces_complete_dataset_dir(http_server, tmp_path: Path):
    from standl.modes import run
    from standl.schema import Source

    _populate_server(http_server.root)
    out_dir = tmp_path / "ds"
    _seed_paper_cache(out_dir, _rewrite_soft(http_server.url))

    report = run(Source(accessions=["GSE999001"]), out_dir)

    for name in ("design.yaml", "manifest.json", "provenance.json", "audit.md"):
        assert (out_dir / name).exists(), f"expected artifact missing: {name}"

    # Raw files land at raw/<sample_id>/<basename>.
    for sid, basename in [
        ("GSM999001", "GSM999001_HN01_Tumor_matrix.mtx.gz"),
        ("GSM999002", "GSM999002_HN01_PBL_features.tsv.gz"),
    ]:
        assert (out_dir / "raw" / sid / basename).exists()

    # No fail-severity records on the happy path.
    fails = [r for r in report.records if r.status == "fail"]
    assert not fails, f"unexpected fails: {fails}"


def test_run_manifest_entries_are_all_ok_on_happy_path(http_server, tmp_path: Path):
    from standl.modes import run
    from standl.schema import Source

    _populate_server(http_server.root)
    out_dir = tmp_path / "ds"
    _seed_paper_cache(out_dir, _rewrite_soft(http_server.url))

    run(Source(accessions=["GSE999001"]), out_dir)

    manifest = json.loads((out_dir / "manifest.json").read_text())
    statuses = {e["status"] for e in manifest["entries"]}
    assert statuses == {"ok"}
    # sha256 computed for every entry.
    for e in manifest["entries"]:
        assert e["sha256"] and len(e["sha256"]) == 64
        assert e["size_bytes"] is not None and e["size_bytes"] > 0


def test_run_idempotent_when_files_already_cached(http_server, tmp_path: Path):
    """Running twice shouldn't re-download — the second pass must still end
    up with status=ok entries and a non-fail audit."""
    from standl.modes import run
    from standl.schema import Source

    _populate_server(http_server.root)
    out_dir = tmp_path / "ds"
    _seed_paper_cache(out_dir, _rewrite_soft(http_server.url))

    run(Source(accessions=["GSE999001"]), out_dir)
    report2 = run(Source(accessions=["GSE999001"]), out_dir)

    fails = [r for r in report2.records if r.status == "fail"]
    assert not fails


# -------- missing files --------

def test_run_marks_entries_missing_when_downloads_404(http_server, tmp_path: Path):
    from standl.modes import run
    from standl.schema import Source

    # Intentionally do NOT populate the server → every fetch 404s.
    out_dir = tmp_path / "ds"
    _seed_paper_cache(out_dir, _rewrite_soft(http_server.url))

    report = run(Source(accessions=["GSE999001"]), out_dir)

    manifest = json.loads((out_dir / "manifest.json").read_text())
    statuses = {e["status"] for e in manifest["entries"]}
    assert "missing" in statuses

    # audit.md must surface the failure.
    assert report.worst_severity() == "fail"
    assert any(
        r.check == "files_on_disk" and r.status == "fail"
        for r in report.records
    )


# -------- no applicable extractor --------

def test_run_raises_when_no_extractor_matches(tmp_path: Path):
    """A DOI-only source (no GEO accession, no local h5ad) has nothing to
    extract from — run must raise, not produce a half-baked dataset dir."""
    from standl.modes import run
    from standl.schema import Source

    out_dir = tmp_path / "empty"
    with pytest.raises(ValueError, match="no extractors"):
        run(Source(paper_doi="10.0000/unresolvable"), out_dir)


# -------- extractor failure is recorded, not fatal --------

def test_run_records_extractor_failure_via_partial(monkeypatch, tmp_path: Path, http_server):
    """If an extractor raises mid-run, we collect it as a PartialDesign with
    failures (not crash). Downstream validate still runs; the dataset dir is
    produced even if empty."""
    from standl.modes import run
    from standl.schema import Source
    from standl.extractors.geo_soft import GEOSoftExtractor

    def boom(self, source, cache_dir):
        raise RuntimeError("synthetic extractor crash")
    monkeypatch.setattr(GEOSoftExtractor, "extract", boom)

    out_dir = tmp_path / "ds"
    # Provide a valid source so geo-soft fires; boom intercepts.
    report = run(Source(accessions=["GSE999001"]), out_dir)

    # The failure should appear somewhere in audit (either via merged design
    # failures, or via the resulting empty-samples validate checks surfacing).
    assert (out_dir / "design.yaml").exists()
    assert (out_dir / "audit.md").exists()
    # No samples merged → sample_id_valid is ok (vacuous) and manifest is empty.
    manifest = json.loads((out_dir / "manifest.json").read_text())
    assert manifest["entries"] == []
