"""HTTP(S) downloader with sha256 verification and idempotent re-runs.

``download(url, dest)`` is the single unit modes.run calls per manifest entry.
Idempotency is a hard requirement — re-running ``standl run`` on the same
dataset directory must not re-fetch files that already match the expected
checksum.

Two sugar behaviors on top of a plain streaming download:

- **FTP→HTTPS rewrite.** GEO emits FTP URLs (``ftp://ftp.ncbi.nlm.nih.gov/...``)
  which requests doesn't speak natively; the NCBI mirror serves the same tree
  over HTTPS, so we swap the scheme. Only applied for that exact host so we
  don't silently rewrite non-NCBI FTP URLs.
- **Cached short-circuit.** If ``dest`` exists and ``sha256`` (or, failing
  that, ``expected_size``) matches, we skip the download and return
  ``fresh=False``. Mismatches trigger a fresh fetch.

Any sha256 mismatch *after* a fresh download deletes the on-disk file and
raises — garbage on disk is worse than a clear error.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


_NCBI_FTP_PREFIX = "ftp://ftp.ncbi.nlm.nih.gov/"
_NCBI_HTTPS_PREFIX = "https://ftp.ncbi.nlm.nih.gov/"


@dataclass
class DownloadResult:
    path: Path
    size_bytes: int
    sha256: str
    fresh: bool  # True if we fetched, False if the cache already matched


def _sha256_file(p: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _normalize_url(url: str) -> str:
    if url.startswith(_NCBI_FTP_PREFIX):
        return _NCBI_HTTPS_PREFIX + url[len(_NCBI_FTP_PREFIX):]
    return url


def download(
    url: str,
    dest: Path,
    sha256: str | None = None,
    expected_size: int | None = None,
    *,
    timeout: float = 60.0,
    chunk_size: int = 1 << 16,
) -> DownloadResult:
    """Fetch ``url`` to ``dest``. Idempotent when the destination already matches.

    Short-circuit logic, in order:
      1. ``dest`` exists AND ``sha256`` given AND disk file matches → skip.
      2. ``dest`` exists AND ``expected_size`` given AND disk size matches
         (no sha256 available) → skip, but compute sha256 to fill the result.

    Fresh download:
      3. Stream ``url`` to ``dest``, hashing as we go.
      4. If ``sha256`` is given and differs, ``unlink`` ``dest`` and raise.
    """
    import requests  # imported here so standl is importable without requests

    if dest.exists():
        if sha256 and _sha256_file(dest) == sha256:
            return DownloadResult(
                path=dest,
                size_bytes=dest.stat().st_size,
                sha256=sha256,
                fresh=False,
            )
        if sha256 is None and expected_size is not None and dest.stat().st_size == expected_size:
            return DownloadResult(
                path=dest,
                size_bytes=expected_size,
                sha256=_sha256_file(dest),
                fresh=False,
            )

    dest.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    with requests.get(_normalize_url(url), stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in r.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                fh.write(chunk)
                h.update(chunk)

    got = h.hexdigest()
    if sha256 is not None and got != sha256:
        dest.unlink(missing_ok=True)
        raise IOError(
            f"sha256 mismatch for {url}: expected {sha256}, got {got}",
        )

    return DownloadResult(
        path=dest,
        size_bytes=dest.stat().st_size,
        sha256=got,
        fresh=True,
    )
