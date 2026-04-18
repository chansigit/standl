"""figshare — Figshare article extractor.

Figshare mirrors the Zenodo pattern: numeric article id + DOI
``10.6084/m9.figshare.<article_id>`` (optionally suffixed ``.v<N>`` for a
specific version). API ``GET https://api.figshare.com/v2/articles/<id>``
returns ``files[]`` (each ``{name, size, computed_md5, download_url}``).

Like Zenodo, the metadata schema is not single-cell-specific; biological
fields stay empty and the skill flow fills them in.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..schema import PartialDesign, PartialSample, ProvenancedValue, Source
from .base import make_pv, register

_pv = make_pv("figshare", default_confidence=0.9)

API_BASE = "https://api.figshare.com/v2/articles"
# Matches both "10.6084/m9.figshare.12345" and "10.6084/m9.figshare.12345.v3".
_FIGSHARE_DOI_RE = re.compile(
    r"^10\.6084/m9\.figshare\.(\d+)(?:\.v\d+)?$",
    re.IGNORECASE,
)
_FIGSHARE_URL_RE = re.compile(
    r"figshare\.com/articles/(?:[^/]+/)?(?:[^/]+/)?(\d+)",
    re.IGNORECASE,
)


def _extract_article_id(source: Source) -> str | None:
    if source.paper_doi:
        m = _FIGSHARE_DOI_RE.match(source.paper_doi.strip())
        if m:
            return m.group(1)
    if source.paper_url:
        m = _FIGSHARE_URL_RE.search(source.paper_url)
        if m:
            return m.group(1)
    repos = {r.lower() for r in source.repositories}
    if "figshare" in repos:
        for acc in source.accessions:
            if acc.isdigit():
                return acc
    return None


def _fetch_article(article_id: str, cache_dir: Path | None) -> dict[str, Any]:
    """GET /v2/articles/<article_id>. Caches to ``cache_dir``."""
    if cache_dir is not None:
        cached = cache_dir / f"figshare_{article_id}.json"
        if cached.is_file():
            return json.loads(cached.read_text())

    import requests

    r = requests.get(f"{API_BASE}/{article_id}", timeout=30)
    r.raise_for_status()
    data = r.json()
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"figshare_{article_id}.json").write_text(json.dumps(data))
    return data


class FigshareExtractor:
    name = "figshare"

    def can_handle(self, source: Source) -> float:
        if source.paper_doi and _FIGSHARE_DOI_RE.match(source.paper_doi.strip()):
            return 0.9
        if source.paper_url and _FIGSHARE_URL_RE.search(source.paper_url):
            return 0.9
        repos = {r.lower() for r in source.repositories}
        if "figshare" in repos and any(a.isdigit() for a in source.accessions):
            return 0.9
        if "figshare" in repos:
            return 0.5
        if source.paper_url and "figshare.com" in source.paper_url:
            return 0.3
        return 0.0

    def extract(self, source: Source, cache_dir: Path) -> PartialDesign:
        article_id = _extract_article_id(source)
        if article_id is None:
            return PartialDesign(
                extractor=self.name,
                failures={
                    "article_id": (
                        "no Figshare article id; pass "
                        "paper_doi=10.6084/m9.figshare.<id>[.v<N>], "
                        "paper_url=https://figshare.com/articles/.../<id>, or "
                        "accessions=[<id>] with repositories=['Figshare']"
                    ),
                },
            )

        cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            record = _fetch_article(article_id, cache_dir)
        except Exception as e:  # noqa: BLE001
            return PartialDesign(
                extractor=self.name,
                dataset_id=article_id,
                source=Source(
                    accessions=[article_id],
                    repositories=["Figshare"],
                    paper_doi=(source.paper_doi if source.paper_doi and _FIGSHARE_DOI_RE.match(source.paper_doi) else f"10.6084/m9.figshare.{article_id}"),
                ),
                failures={"api": f"{type(e).__name__}: {e}"},
            )

        return _build_partial(record, article_id, source.paper_doi)


def _build_partial(
    record: dict[str, Any], article_id: str, requested_doi: str | None,
) -> PartialDesign:
    failures: dict[str, str] = {}

    sample = PartialSample(sample_id=article_id)
    sample.accession = _pv(article_id, "figshare article id", confidence=1.0)

    if title := record.get("title"):
        sample.extra["title"] = _pv(str(title), "title")
    if tags := record.get("tags"):
        if isinstance(tags, list):
            sample.extra["tags"] = _pv("; ".join(str(t) for t in tags), "tags")
    if (lic := record.get("license")) and isinstance(lic, dict):
        name = lic.get("name") or lic.get("value")
        if name:
            sample.extra["license"] = _pv(str(name), "license.name")

    files = record.get("files") or []
    url_map: dict[str, list[str]] = {}
    if files:
        rel_paths: list[str] = []
        urls: list[str] = []
        for f in files:
            if not isinstance(f, dict):
                continue
            name = f.get("name")
            dlurl = f.get("download_url")
            if not (name and dlurl):
                continue
            rel_paths.append(f"{article_id}/{name}")
            urls.append(str(dlurl))
        if rel_paths:
            sample.files = ProvenancedValue(
                value=rel_paths,
                source="figshare",
                confidence=0.95,
                evidence="files[*].download_url",
            )
            url_map[article_id] = urls
        else:
            failures["files"] = "figshare article has no resolvable download URLs"
    else:
        failures["files"] = "figshare article has no files"

    # Prefer the API's canonical DOI (usually versioned) over what the caller
    # may have passed in stripped.
    doi = str(record.get("doi") or requested_doi or f"10.6084/m9.figshare.{article_id}")

    notes = str(title) if (title := record.get("title")) else None

    return PartialDesign(
        extractor="figshare",
        dataset_id=article_id,
        source=Source(
            accessions=[article_id],
            repositories=["Figshare"],
            paper_doi=doi,
        ),
        samples=[sample],
        url_map=url_map,
        failures=failures,
        notes=notes,
    )


register(FigshareExtractor())
