"""Shared pytest fixtures.

Helpers exposed as fixtures (rather than importable functions) because the
active venv already owns a top-level ``tests`` package — a direct
``from tests.conftest import ...`` resolves against that, not ours.
"""
from __future__ import annotations

import http.server
import socketserver
import threading
from dataclasses import dataclass
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


# -------- tiny local HTTP server for fetch / run tests --------

class _SilentHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, directory: str, **kwargs: Any) -> None:
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return  # suppress pytest noise


@dataclass
class LocalServer:
    url: str
    root: Path
    _server: socketserver.TCPServer

    def shutdown(self) -> None:
        self._server.shutdown()


@pytest.fixture
def http_server(tmp_path: Path):
    """Spin up ``http.server`` on a random port serving a tmp subdirectory.

    Use ``server.root`` to drop files and ``server.url`` to fetch them.
    """
    root = tmp_path / "serve"
    root.mkdir()

    def factory(*args: Any, **kwargs: Any) -> _SilentHandler:
        return _SilentHandler(*args, directory=str(root), **kwargs)

    srv = socketserver.TCPServer(("127.0.0.1", 0), factory)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    local = LocalServer(url=f"http://127.0.0.1:{port}", root=root, _server=srv)
    try:
        yield local
    finally:
        local.shutdown()
