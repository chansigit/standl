"""Shared pytest fixtures.

Helpers exposed as fixtures (rather than importable functions) because the
active venv already owns a top-level ``tests`` package — a direct
``from tests.conftest import ...`` resolves against that, not ours.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest


def _make_h5ad(
    path: Path,
    sample_ids: list[str],
    cells_per_sample: int = 5,
    obs_cols: dict[str, list[Any]] | None = None,
    uns: dict[str, Any] | None = None,
) -> Path:
    """Build a tiny h5ad with ``obs['sample']`` populated.

    ``obs_cols`` columns must have length ``len(sample_ids) * cells_per_sample``.
    """
    import anndata as ad
    import numpy as np
    import pandas as pd

    n = len(sample_ids) * cells_per_sample
    X = np.zeros((n, 3), dtype="float32")
    data: dict[str, list[Any]] = {
        "sample": [sid for sid in sample_ids for _ in range(cells_per_sample)],
    }
    if obs_cols:
        for col, values in obs_cols.items():
            if len(values) != n:
                raise ValueError(f"obs_cols[{col!r}] len {len(values)} != {n}")
            data[col] = list(values)
    obs = pd.DataFrame(data)
    a = ad.AnnData(X=X, obs=obs)
    if uns:
        for k, v in uns.items():
            a.uns[k] = v
    a.write_h5ad(path)
    return path


@pytest.fixture
def make_h5ad() -> Callable[..., Path]:
    return _make_h5ad
