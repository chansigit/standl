#!/usr/bin/env python3
"""standl → stanobj pipeline glue.

Walks a standl dataset dir (``design.yaml`` + ``manifest.json`` + ``raw/``),
runs ``stanobj`` per sample, stamps each output's ``obs`` with the
design-level labels (``sample``, ``condition``, ``batch``, ``donor_id``,
``tissue``), concatenates into a single ``combined.h5ad``, and finally
invokes ``standl validate --h5ad`` so the loop closes.

This is orchestration code, not part of standl's core API. It lives under
``scripts/`` because the contract is "call two CLIs in the right order";
once the downstream stan* family grows more integration points a proper
python package could subsume it. For now: a thin, transparent script.

Usage:
    python scripts/standl_to_stanobj.py <dataset_dir> \
        [--stanobj /path/to/stanobj.py] \
        [--out <dataset_dir>/standardized/]

Requires: ``anndata``, ``standl`` (the tool itself), and ``stanobj``'s
``stanobj.py`` CLI on disk. Picks up the first file listed in each sample's
``sample.files`` — if a sample has multiple files (e.g. a 10x mtx triplet),
``stanobj`` should be pointed at the directory instead; for that case,
pass ``--primary-by-suffix .h5`` or ``.mtx`` to pick the primary.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml


def _pick_primary_file(files: list[str], suffix_pref: list[str]) -> str | None:
    """Pick the representative file from a sample.files list.

    Preference order comes from the caller (e.g. ['.h5', '.h5ad', '.mtx']);
    falls back to the first file if none match.
    """
    for suffix in suffix_pref:
        for f in files:
            if f.endswith(suffix):
                return f
    return files[0] if files else None


def _run_stanobj(stanobj_py: Path, input_path: Path, out_h5ad: Path) -> int:
    """Invoke ``stanobj <input> -o <output>`` as a subprocess."""
    out_h5ad.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(stanobj_py), str(input_path), "-o", str(out_h5ad)]
    rc = subprocess.call(cmd)
    return rc


def _stamp_obs(h5ad_path: Path, design_sample: dict) -> None:
    """Overlay design-level metadata onto the stanobj h5ad's obs.

    ``sample`` is always set (standl's ``validate --h5ad`` check 8 requires
    it). Other columns (``condition``, ``batch``, ``donor_id``, ``tissue``,
    ``disease``) are stamped when the design sample sets them.
    """
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


def _concat(per_sample_h5ads: list[Path], out_path: Path) -> None:
    import anndata as ad

    objs = [ad.read_h5ad(p) for p in per_sample_h5ads]
    combined = ad.concat(objs, axis=0, join="outer", index_unique=None)
    combined.write_h5ad(out_path, compression="gzip")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dataset_dir", type=Path)
    ap.add_argument("--stanobj", type=Path,
                    default=Path("/scratch/users/chensj16/projects/stanobj/scripts/stanobj.py"),
                    help="Path to stanobj.py CLI")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output dir (default: <dataset_dir>/standardized)")
    ap.add_argument("--primary-suffix", nargs="+",
                    default=[".h5", ".h5ad", ".mtx", ".mtx.gz", ".rds"],
                    help="Suffix preference for picking the per-sample primary file")
    ap.add_argument("--skip-concat", action="store_true",
                    help="Skip the combined.h5ad step (useful when per-sample files are huge)")
    args = ap.parse_args()

    design_path = args.dataset_dir / "design.yaml"
    if not design_path.exists():
        print(f"[pipeline] no design.yaml at {design_path}", file=sys.stderr)
        return 1
    design = yaml.safe_load(design_path.read_text())

    if not args.stanobj.exists():
        print(f"[pipeline] stanobj.py not found at {args.stanobj}", file=sys.stderr)
        return 1

    out_dir = args.out or (args.dataset_dir / "standardized")
    out_dir.mkdir(parents=True, exist_ok=True)

    raw_dir = args.dataset_dir / "raw"
    per_sample: list[Path] = []

    for s in design.get("samples", []):
        sid = s["sample_id"]
        primary = _pick_primary_file(s.get("files") or [], args.primary_suffix)
        if primary is None:
            print(f"[pipeline] {sid}: no files listed in design.yaml; skipping",
                  file=sys.stderr)
            continue
        src = raw_dir / primary
        if not src.exists():
            print(f"[pipeline] {sid}: {src} missing on disk; skipping", file=sys.stderr)
            continue

        dest = out_dir / f"{sid}.h5ad"
        print(f"[pipeline] {sid}: stanobj {src.name} → {dest.name}", file=sys.stderr)
        rc = _run_stanobj(args.stanobj, src, dest)
        if rc != 0:
            print(f"[pipeline] {sid}: stanobj exit {rc}; skipping", file=sys.stderr)
            continue

        _stamp_obs(dest, s)
        per_sample.append(dest)

    if not per_sample:
        print("[pipeline] no samples successfully standardized", file=sys.stderr)
        return 1

    summary = {
        "dataset_id": design["dataset_id"],
        "per_sample": [str(p) for p in per_sample],
        "combined": None,
    }

    if not args.skip_concat and len(per_sample) > 1:
        combined = out_dir / "combined.h5ad"
        print(f"[pipeline] concatenating {len(per_sample)} → {combined.name}",
              file=sys.stderr)
        _concat(per_sample, combined)
        summary["combined"] = str(combined)

        # Close the loop: standl validate --h5ad.
        print(f"[pipeline] standl validate --h5ad {combined.name}", file=sys.stderr)
        subprocess.call([
            sys.executable, "-m", "standl.cli", "validate",
            str(args.dataset_dir), "--h5ad", str(combined),
        ])

    (out_dir / "pipeline_summary.json").write_text(json.dumps(summary, indent=2))
    print(f"[pipeline] done → {out_dir}/pipeline_summary.json", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
