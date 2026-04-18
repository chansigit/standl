"""Tests for ``standl.fetch.download``."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def test_download_writes_content(http_server, tmp_path: Path):
    from standl.fetch import download

    payload = b"hello standl\n" * 64
    (http_server.root / "sample.bin").write_bytes(payload)

    dest = tmp_path / "out.bin"
    result = download(f"{http_server.url}/sample.bin", dest)

    assert dest.read_bytes() == payload
    assert result.fresh is True
    assert result.size_bytes == len(payload)
    assert result.sha256 == _sha256(payload)


def test_download_verifies_sha256_ok(http_server, tmp_path: Path):
    from standl.fetch import download

    payload = b"payload that hashes to a known value"
    (http_server.root / "x.bin").write_bytes(payload)

    dest = tmp_path / "x.bin"
    result = download(f"{http_server.url}/x.bin", dest, sha256=_sha256(payload))
    assert result.fresh is True


def test_download_rejects_sha256_mismatch(http_server, tmp_path: Path):
    from standl.fetch import download

    (http_server.root / "y.bin").write_bytes(b"actual bytes")
    dest = tmp_path / "y.bin"

    with pytest.raises(IOError, match="sha256 mismatch"):
        download(f"{http_server.url}/y.bin", dest, sha256="0" * 64)
    assert not dest.exists(), "failed downloads must not leave garbage on disk"


def test_download_short_circuits_when_cached_sha256_matches(
    http_server, tmp_path: Path, monkeypatch,
):
    """If dest already holds the expected bytes, we must not hit the network."""
    from standl.fetch import download
    import standl.fetch as fetch_mod

    payload = b"cached bytes"
    sha = _sha256(payload)

    dest = tmp_path / "cached.bin"
    dest.write_bytes(payload)

    # If requests.get is reached, blow up — proves we short-circuited.
    def boom(*a, **kw):
        raise AssertionError("fetch.download should have short-circuited; hit network")
    import requests
    monkeypatch.setattr(requests, "get", boom)

    result = download("http://ignored/should-not-be-used", dest, sha256=sha)
    assert result.fresh is False
    assert result.sha256 == sha


def test_download_redownloads_when_sha256_mismatches_existing(http_server, tmp_path: Path):
    """Stale cache (wrong content) gets replaced by the fresh copy."""
    from standl.fetch import download

    payload = b"v2 content"
    (http_server.root / "z.bin").write_bytes(payload)

    dest = tmp_path / "z.bin"
    dest.write_bytes(b"v1 stale content")  # same name, wrong content

    result = download(
        f"{http_server.url}/z.bin",
        dest,
        sha256=_sha256(payload),
    )
    assert dest.read_bytes() == payload
    assert result.fresh is True


def test_download_short_circuits_on_size_when_no_sha256(tmp_path: Path, monkeypatch):
    from standl.fetch import download

    payload = b"exact size here" * 10
    dest = tmp_path / "w.bin"
    dest.write_bytes(payload)

    def boom(*a, **kw):
        raise AssertionError("should have short-circuited on size match")
    import requests
    monkeypatch.setattr(requests, "get", boom)

    result = download("http://ignored/w.bin", dest, expected_size=len(payload))
    assert result.fresh is False
    assert result.size_bytes == len(payload)


def test_download_rewrites_ncbi_ftp_to_https(monkeypatch, tmp_path: Path):
    """ftp://ftp.ncbi.nlm.nih.gov/... -> https://ftp.ncbi.nlm.nih.gov/..."""
    from standl.fetch import download
    import requests

    calls: list[str] = []

    class FakeResp:
        status_code = 200
        headers = {"content-type": "application/octet-stream"}

        def raise_for_status(self): return None
        def iter_content(self, chunk_size): yield b"fake"
        def json(self): return {}
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_get(url, *args, **kwargs):
        calls.append(url)
        return FakeResp()

    monkeypatch.setattr(requests, "get", fake_get)

    download(
        "ftp://ftp.ncbi.nlm.nih.gov/geo/samples/x.txt",
        tmp_path / "out.txt",
    )
    # _resolve_json_indirection makes one HEAD-like call, then the streaming
    # download makes a second; both should carry the HTTPS-rewritten URL.
    assert all(c.startswith("https://ftp.ncbi.nlm.nih.gov/") for c in calls)
    assert len(calls) >= 1


def test_download_raises_on_404(http_server, tmp_path: Path):
    from standl.fetch import download
    import requests

    with pytest.raises(requests.HTTPError):
        download(f"{http_server.url}/does_not_exist.bin", tmp_path / "x")


# -------- JSON Status/Location indirection (Azul HCA) --------

def test_download_follows_json_location_indirection(http_server, tmp_path: Path):
    """Server returns JSON with {Status: 302, Location} — fetch follows once."""
    import json as _json
    from standl.fetch import download

    payload = b"real bytes behind the json indirection\n" * 32
    (http_server.root / "real.bin").write_bytes(payload)
    # Content-type for .json is application/json by default with SimpleHTTPRequestHandler.
    (http_server.root / "fetch_stub.json").write_text(_json.dumps({
        "Status": 302,
        "Location": f"{http_server.url}/real.bin",
    }))

    dest = tmp_path / "out.bin"
    result = download(f"{http_server.url}/fetch_stub.json", dest)
    assert dest.read_bytes() == payload
    assert result.fresh is True
    assert result.size_bytes == len(payload)


def test_download_raises_on_json_indirection_not_ready(http_server, tmp_path: Path):
    """Azul's async-prep returns Status=301 + Retry-After when the file
    isn't signed yet. We surface that as a clear IOError."""
    import json as _json
    import pytest as _pytest
    from standl.fetch import download

    (http_server.root / "not_ready.json").write_text(_json.dumps({
        "Status": 301,
        "Retry-After": 5,
    }))

    with _pytest.raises(IOError, match="not ready"):
        download(f"{http_server.url}/not_ready.json", tmp_path / "x.bin")


def test_download_indirection_probe_does_not_read_body(monkeypatch, tmp_path: Path):
    """Regression: _resolve_json_indirection used to issue a plain
    ``requests.get(url)`` which buffers the entire body into memory when
    the response is octet-stream (which is the common case). For a 4 GB
    file this OOMs the process before streaming download can start. The
    fix: use stream=True and avoid reading body for non-JSON responses.
    """
    from standl.fetch import _resolve_json_indirection
    import requests

    body_read_count = 0

    class SpyResp:
        status_code = 200
        # Binary content-type — the non-JSON path.
        headers = {"content-type": "application/octet-stream"}

        def raise_for_status(self): return None
        def __enter__(self): return self
        def __exit__(self, *a): return False

        @property
        def content(self):
            nonlocal body_read_count
            body_read_count += 1
            return b"x" * (10 << 20)  # pretend 10 MB

        def json(self):
            nonlocal body_read_count
            body_read_count += 1
            return {}

    def spy_get(url, *args, **kwargs):
        # The streaming fix must pass stream=True.
        assert kwargs.get("stream") is True, "indirection probe must use stream=True"
        return SpyResp()

    monkeypatch.setattr(requests, "get", spy_get)
    resolved = _resolve_json_indirection("http://example/big.bin", timeout=30)
    assert resolved == "http://example/big.bin"
    assert body_read_count == 0, (
        f"non-JSON response body was accessed ({body_read_count}×); "
        "indirection probe must not read the body"
    )
