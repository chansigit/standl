#!/usr/bin/env python3
"""Rescue flow for GSE149689 — pooled series matrix → per-sample h5ad.

Reference implementation of ``skills/standl/SKILL.md`` ``Rescue flow:
data_layout failure``. Lee et al. 2020 COVID PBMC (20 samples, one pooled
10x matrix at Series_supplementary_file level, barcodes suffixed ``-1``
through ``-20``).

Pre-req: ``standl run GSE149689 -o <dir>`` has already been called. That
produces a FAIL audit.md citing ``data_layout`` and stashes the series
URLs in ``design.yaml: notes``. This script picks up from there.

Usage:

    python scripts/demo_gse149689_rescue.py [OUT_DIR]

OUT_DIR defaults to /tmp/gse149689.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import anndata as ad
import pandas as pd
import scipy.io
import yaml

from standl.fetch import download
from standl.extractors.geo_soft import _locate_soft, _parse_soft


SERIES_URL_RE = re.compile(r"(ftp://[^\s;]+)")


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def step1_locate_urls(dataset_dir: Path) -> list[str]:
    """Read <dataset_dir>/design.yaml → notes → pull every ftp:// URL out."""
    d = yaml.safe_load((dataset_dir / "design.yaml").read_text())
    notes = d.get("notes") or ""
    urls = SERIES_URL_RE.findall(notes)
    if not urls:
        raise SystemExit(
            "No series URLs in design.yaml notes. Was `standl run` already called?"
        )
    return urls


def step2_download(urls: list[str], paper_dir: Path) -> dict[str, Path]:
    """Download each URL to <dataset_dir>/paper/. Idempotent via fetch.download."""
    paper_dir.mkdir(parents=True, exist_ok=True)
    local: dict[str, Path] = {}
    for url in urls:
        basename = url.rsplit("/", 1)[-1]
        dest = paper_dir / basename
        r = download(url, dest, timeout=120)
        print(f"  {'fetched' if r.fresh else 'cached '}  {dest.name}  ({r.size_bytes/1e6:.1f} MB)")
        local[basename] = dest
    return local


def step3_load_pooled(files: dict[str, Path]) -> ad.AnnData:
    """Reconstruct an AnnData from matrix.mtx.gz + barcodes.tsv.gz + features.tsv.gz."""
    matrix_path = next(p for n, p in files.items() if n.endswith("_matrix.mtx.gz"))
    barcodes_path = next(p for n, p in files.items() if n.endswith("_barcodes.tsv.gz"))
    features_path = next(p for n, p in files.items() if n.endswith("_features.tsv.gz"))

    with gzip.open(matrix_path, "rb") as fh:
        X = scipy.io.mmread(fh).tocsr()
    # CellRanger mtx is genes × cells; scanpy / anndata expect cells × genes.
    X = X.T.tocsr()

    barcodes = pd.read_csv(barcodes_path, header=None, sep="\t")[0].astype(str).values
    features = pd.read_csv(features_path, header=None, sep="\t")
    var_names = features[1].astype(str).values if features.shape[1] > 1 else features[0].astype(str).values

    adata = ad.AnnData(
        X=X,
        obs=pd.DataFrame(index=barcodes),
        var=pd.DataFrame(index=var_names),
    )
    print(f"  pooled AnnData: {adata.shape[0]} cells × {adata.shape[1]} genes")
    return adata


def step4_sample_mapping(paper_dir: Path) -> list[str]:
    """Recover GSM ids in barcode-suffix order (Sample N ↔ suffix -N).

    Cached SOFT file lives at <paper_dir>/GSE149689_family.soft.gz.
    """
    soft = _locate_soft("GSE149689", paper_dir)
    if soft is None:
        raise SystemExit("SOFT fixture not in paper/ — re-run `standl run` first.")
    parsed = _parse_soft(soft)
    ids = parsed.series.get("Series_sample_id", [])
    if not ids:
        raise SystemExit("SOFT has no Series_sample_id entries.")
    print(f"  {len(ids)} samples in SOFT-declared order; GSM[0]={ids[0]}")
    return ids


def step5_split_and_write(
    pooled: ad.AnnData, sample_ids: list[str], raw_dir: Path,
) -> list[Path]:
    """Slice by barcode suffix ``-i`` for i in 1..N; write one h5ad per GSM."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for i, gsm in enumerate(sample_ids, start=1):
        suffix = f"-{i}"
        mask = pd.Index(pooled.obs_names).str.endswith(suffix)
        sub = pooled[mask].copy()
        dest_dir = raw_dir / gsm
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "observed.h5ad"
        sub.write_h5ad(dest)
        print(f"  {gsm}  {sub.shape[0]:>6d} cells  → {dest.relative_to(raw_dir.parent)}")
        written.append(dest)
    return written


def step6_rewrite_design(dataset_dir: Path, sample_ids: list[str]) -> None:
    """Point each sample.files at the newly-written h5ad; record the split in extraction.methods."""
    path = dataset_dir / "design.yaml"
    d = yaml.safe_load(path.read_text())
    by_id = {s["sample_id"]: s for s in d["samples"]}
    for gsm in sample_ids:
        s = by_id[gsm]
        s["files"] = [f"{gsm}/observed.h5ad"]
    methods = set(d["extraction"].get("methods", []))
    methods.add("rescue-split")
    d["extraction"]["methods"] = sorted(methods)
    prev_notes = d.get("notes") or ""
    d["notes"] = (
        prev_notes
        + " | split: pooled Series_supplementary_file matrix by barcode suffix "
        "(-1..-N, Series_sample_id order); see scripts/demo_gse149689_rescue.py"
    )
    path.write_text(yaml.safe_dump(d, sort_keys=False))


def step7_rewrite_manifest(
    dataset_dir: Path, sample_ids: list[str], sources: list[str],
) -> None:
    """Build manifest entries for the per-sample h5ad files.

    ``url`` uses the ``rescue://`` pseudo-scheme plus a pipe-joined list of
    the real series URLs; the information lives in design.yaml ``notes`` too,
    but keeping it on the manifest entry means ``standl validate --deep``
    can still be trusted (sha256s are computed against the written h5ad).
    """
    raw = dataset_dir / "raw"
    created = datetime.now(timezone.utc).isoformat()
    entries = []
    for gsm in sample_ids:
        p = raw / gsm / "observed.h5ad"
        entries.append({
            "path": f"{gsm}/observed.h5ad",
            "url": "rescue://pooled-series-split|" + "|".join(sources),
            "size_bytes": p.stat().st_size,
            "sha256": _sha256(p),
            "md5": None,
            "status": "ok",
            "downloaded_at": created,
            "source_accession": gsm,
        })
    manifest = {
        "dataset_id": "GSE149689",
        "entries": entries,
        "created_at": created,
    }
    (dataset_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def step8_revalidate(dataset_dir: Path) -> int:
    """Hand off to `standl validate`. Expect worst severity `ok`."""
    print()
    rc = subprocess.call(
        [sys.executable, "-m", "standl.cli", "validate", str(dataset_dir)]
    )
    return rc


def main(argv: list[str]) -> int:
    out_dir = Path(argv[1]) if len(argv) > 1 else Path("/tmp/gse149689")

    if not (out_dir / "design.yaml").exists():
        raise SystemExit(
            f"{out_dir}/design.yaml not found. Run first:\n"
            f"    standl run GSE149689 -o {out_dir}"
        )

    print(f"[rescue] step 1 — locate series URLs in design.yaml notes")
    urls = step1_locate_urls(out_dir)

    print(f"[rescue] step 2 — download {len(urls)} series file(s) to paper/")
    files = step2_download(urls, out_dir / "paper")

    print(f"[rescue] step 3 — load pooled AnnData")
    pooled = step3_load_pooled(files)

    print(f"[rescue] step 4 — recover barcode-suffix ↔ GSM mapping from SOFT")
    sample_ids = step4_sample_mapping(out_dir / "paper")

    print(f"[rescue] step 5 — split by barcode suffix + write per-sample h5ad")
    # Nuke any existing raw/ from a previous rescue.
    raw_dir = out_dir / "raw"
    if raw_dir.exists():
        shutil.rmtree(raw_dir)
    step5_split_and_write(pooled, sample_ids, raw_dir)

    print(f"[rescue] step 6 — rewrite design.yaml sample.files")
    step6_rewrite_design(out_dir, sample_ids)

    print(f"[rescue] step 7 — rebuild manifest.json")
    step7_rewrite_manifest(out_dir, sample_ids, urls)

    print(f"[rescue] step 8 — revalidate")
    return step8_revalidate(out_dir)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
