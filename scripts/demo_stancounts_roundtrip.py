#!/usr/bin/env python3
"""stancounts round-trip demo on a standl-stan pipeline output.

Premise: standl → stanobj → stangene all produce AnnData with integer
``X`` (or ``layers['counts']``) — stancounts has nothing to reverse on
that. To actually exercise stancounts as a downstream step, we:

1. Take a stangene-harmonized h5ad (integer counts).
2. Subsample to a reasonable cell count (the raw-feature CellRanger
   outputs have >300k mostly-empty barcodes; stancounts' scale-factor
   inference needs cells with real signal).
3. Synthesize a log1p-normalized copy with a standard scanpy-style
   workflow: ``x_norm = log1p(x / lib * target_sum)`` with
   ``target_sum=1e4``.
4. Feed the synthesized h5ad into ``stancounts.reverse_log1p_anndata``.
5. Compare the recovered counts to the originals, entry-by-entry.

A perfect round-trip (0 mismatches across all non-zero entries) is the
expected outcome. If stancounts drifts by ±1 on some entries, that's
rounding noise — still OK.

Usage:
    python scripts/demo_stancounts_roundtrip.py <harmonized.h5ad> [--n-cells 2000]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import anndata as ad
import numpy as np
import scipy.sparse as sp


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("src", type=Path, help="Input h5ad with integer counts in X")
    ap.add_argument("--n-cells", type=int, default=2000,
                    help="Subsample to this many real cells (lib_size > --min-lib)")
    ap.add_argument("--min-lib", type=int, default=500,
                    help="Skip cells with library size ≤ this (mostly-empty barcodes)")
    args = ap.parse_args()

    a = ad.read_h5ad(args.src)
    X = a.X.tocsr() if sp.issparse(a.X) else sp.csr_matrix(a.X)

    # Skip mostly-empty barcodes so scale-factor inference has signal.
    lib_all = np.asarray(X.sum(axis=1)).flatten()
    kept = np.where(lib_all > args.min_lib)[0][:args.n_cells]
    if len(kept) == 0:
        print(f"[stancounts] no cells with library > {args.min_lib}", file=sys.stderr)
        return 1
    a_small = a[kept].copy()
    X_counts = a_small.X.tocsr().astype(np.float32)
    print(f"[stancounts] subset: {a_small.shape}, nnz={X_counts.nnz}",
          file=sys.stderr)

    # Synthesize log1p normalization.
    lib = np.asarray(X_counts.sum(axis=1)).flatten()
    target = 1e4
    scaled = X_counts.multiply((target / lib)[:, None])
    X_norm = sp.csr_matrix(scaled)
    X_norm.data = np.log1p(X_norm.data)
    a_norm = ad.AnnData(X=X_norm, obs=a_small.obs.copy(), var=a_small.var.copy())
    print(f"[stancounts] log1p max: {X_norm.data.max():.3f}", file=sys.stderr)

    # Reverse.
    from stancounts import reverse_log1p_anndata
    reverse_log1p_anndata(a_norm, source="X", target_layer="counts_recovered", base="e")

    rec = sp.csr_matrix(a_norm.layers["counts_recovered"]).astype(np.int64)
    orig = X_counts.astype(np.int64)

    orig_sum = int(orig.sum())
    rec_sum = int(rec.sum())
    diff = orig - rec
    mismatches = int((diff.data != 0).sum()) if diff.nnz else 0
    worst_abs = int(np.abs(diff.data).max()) if diff.nnz else 0

    print(f"[stancounts] original sum: {orig_sum}  recovered sum: {rec_sum}")
    print(f"[stancounts] nonzero entries (orig / rec): {orig.nnz} / {rec.nnz}")
    print(f"[stancounts] entry-level mismatches: {mismatches}  worst |Δ|: {worst_abs}")
    if mismatches == 0 and orig_sum == rec_sum:
        print("[stancounts] EXACT round-trip across all non-zero entries")
        return 0
    # Off-by-one is rounding noise, not a failure.
    within_one = int((np.abs(diff.data) <= 1).sum()) if diff.nnz else 0
    coverage = (within_one + (orig.nnz - diff.nnz)) / max(orig.nnz, 1)
    print(f"[stancounts] within ±1: {coverage*100:.2f}% of nonzero entries")
    return 0 if coverage > 0.99 else 1


if __name__ == "__main__":
    sys.exit(main())
