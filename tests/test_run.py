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
    # New diagnostic: dedicated download_failed records per entry, kind="missing"
    # (404 / network), not conflated with sha256 mismatch.
    fails = [r for r in report.records if r.check == "download_failed"]
    assert fails, "expected download_failed audit records on 404"
    assert all(r.evidence["kind"] == "missing" for r in fails)


def test_run_download_sha256_mismatch_emits_corrupt_record(
    http_server, tmp_path: Path, monkeypatch,
):
    """sha256 mismatch is a different failure mode from 404 — it means the
    upstream gave us bytes but their content hash doesn't match the API's
    assertion. modes.run must tag the entry status=corrupt and emit a
    download_failed record with kind='corrupt'."""
    from standl.modes import run
    from standl.schema import Source, PartialDesign, PartialSample, ProvenancedValue
    from standl.extractors.geo_soft import GEOSoftExtractor

    _populate_server(http_server.root)  # server serves 20-byte payloads

    # Have geo-soft publish a wrong sha256 so fetch.download rejects the body.
    def fake_extract(self, source, cache_dir):
        sid = "GSM999001"
        rel = f"{sid}/GSM999001_HN01_Tumor_matrix.mtx.gz"
        url = f"{http_server.url}/GSM999001_HN01_Tumor_matrix.mtx.gz"
        return PartialDesign(
            extractor="geo-soft",
            dataset_id="GSE999001",
            source=Source(accessions=["GSE999001"], repositories=["GEO"]),
            samples=[PartialSample(
                sample_id=sid,
                files=ProvenancedValue(value=[rel], source="geo-soft", confidence=0.95),
            )],
            url_map={sid: [url]},
            file_meta={sid: [{"sha256": "0" * 64}]},  # wrong — server serves "payload\n"
        )
    monkeypatch.setattr(GEOSoftExtractor, "extract", fake_extract)

    out_dir = tmp_path / "ds"
    report = run(Source(accessions=["GSE999001"]), out_dir)

    manifest = json.loads((out_dir / "manifest.json").read_text())
    assert any(e["status"] == "corrupt" for e in manifest["entries"])

    fails = [r for r in report.records if r.check == "download_failed"]
    assert fails
    assert any(r.evidence["kind"] == "corrupt" for r in fails)


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

def test_run_elevates_data_layout_failure_to_fail(http_server, tmp_path: Path):
    """When a SOFT has all samples with Sample_supplementary_file = NONE and
    real data at Series_supplementary_file, geo-soft emits a ``data_layout``
    failure. modes.run must surface it as a FAIL audit record so downstream
    CI / callers don't mistake the empty raw/ dir for a successful run."""
    from standl.modes import run
    from standl.schema import Source

    # Start from the fixture and strip every sample-level URL to NONE,
    # then add a series-level pool so the parser triggers data_layout.
    soft_text = _rewrite_soft(http_server.url)
    soft_text = re.sub(
        r"(!Sample_supplementary_file_\d+ = )http://[^\n]+",
        r"\1NONE",
        soft_text,
    )
    soft_text = soft_text.replace(
        "!Series_sample_id = GSM999002\n",
        (
            "!Series_sample_id = GSM999002\n"
            "!Series_supplementary_file = ftp://example/pool_matrix.mtx.gz\n"
        ),
    )
    out_dir = tmp_path / "ds"
    _seed_paper_cache(out_dir, soft_text)

    report = run(Source(accessions=["GSE999001"]), out_dir)

    assert report.worst_severity() == "fail"
    data_layout_records = [
        r for r in report.records
        if r.check == "extractor_partial_failure" and "data_layout" in r.message
    ]
    assert data_layout_records
    assert data_layout_records[0].status == "fail"


def test_run_plumbs_file_meta_into_manifest_entries(http_server, tmp_path: Path, monkeypatch):
    """If an extractor publishes file_meta, ManifestEntry.md5 / size_bytes /
    sha256 should be stamped from it when pre-download. Fake it on geo-soft's
    partial via monkeypatching extract."""
    from standl.modes import run
    from standl.schema import Source, PartialDesign, PartialSample, ProvenancedValue
    from standl.extractors.geo_soft import GEOSoftExtractor

    _populate_server(http_server.root)

    def fake_extract(self, source, cache_dir):
        sid = "GSM999001"
        rel = f"{sid}/GSM999001_HN01_Tumor_matrix.mtx.gz"
        url = f"{http_server.url}/GSM999001_HN01_Tumor_matrix.mtx.gz"
        return PartialDesign(
            extractor="geo-soft",
            dataset_id="GSE999001",
            source=Source(accessions=["GSE999001"], repositories=["GEO"]),
            samples=[PartialSample(
                sample_id=sid,
                files=ProvenancedValue(value=[rel], source="geo-soft", confidence=0.95),
            )],
            url_map={sid: [url]},
            file_meta={sid: [{"md5": "cafebabe" * 4, "size_bytes": 20}]},
        )
    monkeypatch.setattr(GEOSoftExtractor, "extract", fake_extract)

    out_dir = tmp_path / "ds"
    run(Source(accessions=["GSE999001"]), out_dir)

    import json
    m = json.loads((out_dir / "manifest.json").read_text())
    entry = m["entries"][0]
    assert entry["md5"] == "cafebabe" * 4
    # size_bytes ends up as the ACTUAL downloaded size (post-download overwrite),
    # but pre-download it was the API's 20 — we can at least check it's non-None.
    assert entry["size_bytes"] is not None
    # sha256 is computed during download and overwrites the (unset) extractor value.
    assert entry["sha256"] is not None and len(entry["sha256"]) == 64


def test_run_refresh_wipes_extractor_caches(http_server, tmp_path: Path, monkeypatch):
    """With --refresh (i.e. refresh=True), pre-existing cached SOFT files
    under <out>/paper/ are removed before extraction runs."""
    from standl.modes import run
    from standl.schema import Source

    out_dir = tmp_path / "ds"
    paper_cache = out_dir / "paper"
    paper_cache.mkdir(parents=True, exist_ok=True)
    (paper_cache / "GSE999001_family.soft").write_text("stale marker\n")

    calls: list[str] = []

    def fake_fetch(accession, cache_dir):
        calls.append(accession)
        # Write a fresh valid SOFT so extraction succeeds.
        import shutil as _sh
        _sh.copy(FIXTURE_SOFT, cache_dir / "GSE999001_family.soft")
        return cache_dir / "GSE999001_family.soft"

    from standl.extractors import geo_soft as gs
    monkeypatch.setattr(gs, "_fetch_soft", fake_fetch)

    _populate_server(http_server.root)
    # Rewrite URLs in the SOFT once we have fresh bytes (fixture URLs point to NCBI).
    # Easiest: put the server-rewritten SOFT into the cache and assert --refresh
    # wipes it (then fake_fetch writes the un-rewritten one, downloads fail but
    # manifest is built — we check cache clearing, not download success).
    (paper_cache / "GSE999001_family.soft").write_text(_rewrite_soft(http_server.url))

    # Without refresh: cached file is used, fake_fetch not called.
    run(Source(accessions=["GSE999001"]), out_dir, refresh=False)
    assert calls == [], "cached SOFT should short-circuit fetch"

    # With refresh: cache is wiped, fake_fetch fires once.
    run(Source(accessions=["GSE999001"]), out_dir, refresh=True)
    assert calls == ["GSE999001"], f"expected one fetch after refresh, got {calls}"


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
