#!/usr/bin/env python3
"""standl → stanobj → stangene pipeline glue.

Extends ``scripts/standl_to_stanobj.py`` with a per-sample stangene
harmonization step. Produces per-sample harmonized h5ads, concatenates them
with design-level obs columns stamped in, and hands the combined AnnData
back to ``standl validate --h5ad`` so the loop closes on a fully-processed
object.

Usage:
    python scripts/standl_stan_chain.py <dataset_dir> [--species human] \
        [--stanobj /path/to/stanobj.py]

Prerequisite: the dataset dir already has ``design.yaml`` + ``manifest.json``
+ ``raw/`` populated (either via ``standl run`` or a rescue script). Raw files
are expected to be single-file-per-sample (CellRanger .h5 works; .mtx
triplets or .rds also work if the filename matches the primary suffix
preference list).

stancounts is NOT invoked here because stanobj's outputs already carry
integer counts in ``layers['counts']`` — nothing to reverse. See
``scripts/demo_stancounts_roundtrip.py`` for a stancounts round-trip
demonstration on a synthetic log1p-normalized copy.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml


def _pick_primary(files: list[str], suffix_pref: list[str]) -> str | None:
    for suffix in suffix_pref:
        for f in files:
            if f.endswith(suffix):
                return f
    return files[0] if files else None


def _stanobj_convert(stanobj_py: Path, src: Path, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.call([sys.executable, str(stanobj_py), str(src), "-o", str(dest)])


def _stangene_harmonize(
    src: Path, species: str, out_dir: Path, dataset_name: str,
) -> Path | None:
    """Invoke ``python -m stangene harmonize``; returns the enriched h5ad
    path on success or None on failure."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rc = subprocess.call([
        sys.executable, "-m", "stangene", "harmonize",
        "--input", str(src), "--species", species,
        "--output-dir", str(out_dir),
        "--dataset-name", dataset_name,
    ])
    if rc != 0:
        return None
    # stangene writes ``<dataset_name>_harmonized.h5ad`` alongside report.md.
    harmonized = out_dir / f"{dataset_name}_harmonized.h5ad"
    return harmonized if harmonized.exists() else None


def _stamp_design_obs(h5ad_path: Path, design_sample: dict) -> None:
    import anndata as ad

    a = ad.read_h5ad(h5ad_path)
    sid = design_sample["sample_id"]
    a.obs["sample"] = sid
    for key in ("condition", "batch", "donor_id", "tissue", "disease"):
        v = design_sample.get(key)
        if v is not None:
            a.obs[key] = str(v)
    a.obs["accession"] = design_sample.get("accession") or sid
    a.write_h5ad(h5ad_path, compression="gzip")


def _concat(per_sample: list[Path], out: Path) -> None:
    import anndata as ad

    objs = [ad.read_h5ad(p) for p in per_sample]
    combined = ad.concat(objs, axis=0, join="outer", index_unique=None)
    combined.write_h5ad(out, compression="gzip")


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("dataset_dir", type=Path)
    ap.add_argument("--species", default="human",
                    help="Organism for stangene lookup (default: human)")
    ap.add_argument("--stanobj", type=Path,
                    default=Path("/scratch/users/chensj16/projects/stanobj/scripts/stanobj.py"))
    ap.add_argument("--out", type=Path, default=None,
                    help="Output dir (default: <dataset_dir>/standardized_chain)")
    ap.add_argument("--primary-suffix", nargs="+",
                    default=[".h5", ".h5ad", ".mtx", ".mtx.gz", ".rds"])
    args = ap.parse_args()

    design = yaml.safe_load((args.dataset_dir / "design.yaml").read_text())
    if not args.stanobj.exists():
        print(f"[chain] stanobj.py not found at {args.stanobj}", file=sys.stderr)
        return 1

    out_dir = args.out or (args.dataset_dir / "standardized_chain")
    out_dir.mkdir(parents=True, exist_ok=True)
    stanobj_dir = out_dir / "stanobj"
    stangene_dir = out_dir / "stangene"
    stanobj_dir.mkdir(parents=True, exist_ok=True)
    stangene_dir.mkdir(parents=True, exist_ok=True)

    raw_dir = args.dataset_dir / "raw"
    harmonized: list[Path] = []

    for s in design.get("samples", []):
        sid = s["sample_id"]
        primary = _pick_primary(s.get("files") or [], args.primary_suffix)
        if primary is None:
            print(f"[chain] {sid}: no files in design.yaml; skipping", file=sys.stderr)
            continue
        src = raw_dir / primary
        if not src.exists():
            print(f"[chain] {sid}: {src} missing; skipping", file=sys.stderr)
            continue

        # Step 1: stanobj
        obj = stanobj_dir / f"{sid}.h5ad"
        print(f"[chain] {sid}: stanobj → {obj.name}", file=sys.stderr)
        if _stanobj_convert(args.stanobj, src, obj) != 0:
            print(f"[chain] {sid}: stanobj failed; skipping", file=sys.stderr)
            continue

        # Step 2: stangene
        print(f"[chain] {sid}: stangene harmonize ({args.species})", file=sys.stderr)
        harmonized_path = _stangene_harmonize(obj, args.species, stangene_dir, sid)
        if harmonized_path is None:
            print(f"[chain] {sid}: stangene failed; skipping", file=sys.stderr)
            continue

        # Stamp design-level obs so modes.validate --h5ad check 8 passes.
        _stamp_design_obs(harmonized_path, s)
        harmonized.append(harmonized_path)

    if not harmonized:
        print("[chain] no samples made it through the chain", file=sys.stderr)
        return 1

    combined = out_dir / "combined.h5ad"
    print(f"[chain] concat {len(harmonized)} → {combined.name}", file=sys.stderr)
    _concat(harmonized, combined)

    # Close the loop with standl validate.
    print(f"[chain] standl validate --h5ad {combined.name}", file=sys.stderr)
    subprocess.call([
        sys.executable, "-m", "standl.cli", "validate",
        str(args.dataset_dir), "--h5ad", str(combined),
    ])

    (out_dir / "pipeline_summary.json").write_text(json.dumps({
        "dataset_id": design["dataset_id"],
        "chain": ["standl-run", "stanobj", "stangene", "concat", "standl-validate"],
        "per_sample_harmonized": [str(p) for p in harmonized],
        "combined": str(combined),
    }, indent=2) + "\n")
    print(f"[chain] done → {out_dir}/pipeline_summary.json", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
