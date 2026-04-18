"""CLI tests: exit codes, --deep plumbing, post-run summary output.

We invoke ``standl.cli.main(argv)`` directly rather than spawning a
subprocess — same code path, ~100× faster, and ``capsys`` gives us stdout /
stderr without pipes.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "validate_good"


@pytest.fixture
def good(tmp_path: Path) -> Path:
    dst = tmp_path / "ds"
    shutil.copytree(FIXTURE, dst)
    return dst


# -------- exit codes --------

def test_validate_exits_zero_on_ok(good: Path):
    from standl.cli import main
    assert main(["validate", str(good)]) == 0


def test_validate_exits_one_on_fail(good: Path):
    """Drop a manifest entry → files_in_manifest fails → exit 1."""
    from standl.cli import main
    m = json.loads((good / "manifest.json").read_text())
    m["entries"] = [e for e in m["entries"] if "HN01_Tumor/matrix" not in e["path"]]
    (good / "manifest.json").write_text(json.dumps(m, indent=2) + "\n")

    assert main(["validate", str(good)]) == 1


def test_validate_warn_still_exits_zero(good: Path):
    """WARN severity (e.g. no_confound) is informational, not a CI blocker."""
    import yaml
    from standl.cli import main
    d = yaml.safe_load((good / "design.yaml").read_text())
    # Collapse to a 2-sample perfect confound.
    d["samples"][0]["donor_id"] = "D1"
    d["samples"][0]["condition"] = "A"
    d["samples"][1]["donor_id"] = "D2"
    d["samples"][1]["condition"] = "B"
    (good / "design.yaml").write_text(yaml.safe_dump(d, sort_keys=False))

    assert main(["validate", str(good)]) == 0


# -------- --deep --------

def test_validate_deep_flag_catches_size_preserving_corruption(good: Path):
    """Shallow (size-only) misses byte-level drift; --deep catches it."""
    from standl.cli import main
    target = good / "raw" / "HN01_Tumor" / "matrix.mtx.gz"
    original = target.read_bytes()
    # Same length, different content.
    target.write_bytes(bytes(c ^ 0x01 for c in original))

    # Shallow: size matches → exit 0.
    assert main(["validate", str(good)]) == 0
    # Deep: sha256 drifts → exit 1.
    assert main(["validate", str(good), "--deep"]) == 1


# -------- post-run summary --------

def test_validate_prints_audit_summary_to_stderr(good: Path, capsys):
    from standl.cli import main
    main(["validate", str(good)])
    err = capsys.readouterr().err
    assert "audit.md" in err
    assert "worst severity" in err.lower()
    assert "ok" in err


def test_run_warns_when_design_lacks_factors(http_server, tmp_path: Path, capsys, monkeypatch):
    """Fresh `standl run` produces a design with no factors/contrasts (those
    are human-filled via the skill). The CLI must print a hint pointing at
    the skill so users aren't stranded with an incomplete design.
    """
    import re
    from standl.cli import main

    FIXTURE_SOFT = Path(__file__).parent / "fixtures" / "geo" / "GSE999001_family.soft"
    soft_text = re.sub(
        r"ftp://ftp\.ncbi\.nlm\.nih\.gov/geo/samples/[^/]+/[^/]+/suppl/",
        f"{http_server.url}/",
        FIXTURE_SOFT.read_text(),
    )

    for name in [
        "GSM999001_HN01_Tumor_matrix.mtx.gz",
        "GSM999001_HN01_Tumor_barcodes.tsv.gz",
        "GSM999001_HN01_Tumor_features.tsv.gz",
        "GSM999002_HN01_PBL_matrix.mtx.gz",
        "GSM999002_HN01_PBL_barcodes.tsv.gz",
        "GSM999002_HN01_PBL_features.tsv.gz",
    ]:
        (http_server.root / name).write_bytes(b"payload\n")

    out_dir = tmp_path / "ds"
    (out_dir / "paper").mkdir(parents=True, exist_ok=True)
    (out_dir / "paper" / "GSE999001_family.soft").write_text(soft_text)

    code = main(["run", "GSE999001", "-o", str(out_dir)])
    err = capsys.readouterr().err
    assert code == 0  # happy path
    assert "factors" in err.lower() or "condition" in err.lower()
    assert "SKILL" in err or "skill" in err
