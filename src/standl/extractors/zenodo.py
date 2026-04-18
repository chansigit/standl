"""zenodo — generic Zenodo record extractor.

Zenodo is a domain-agnostic DOI-backed data repository; single-cell data
often lives there as supplementary material to a bioRxiv / journal paper.
Each record has a numeric id and a DOI of the form
``10.5281/zenodo.<record_id>``.

API: ``GET https://zenodo.org/api/records/<record_id>`` → JSON with
``files[]`` (each carries ``key`` = filename, ``size``, ``checksum`` =
``md5:<hex>``, ``links.self`` = direct HTTPS download URL).

No structured single-cell vocab (no organism / assay / tissue ontologies)
— Zenodo metadata is free-form. Extracted fields land mostly in
``Sample.extra`` (title, keywords, license, description snippet). The
human-in-the-loop skill flow fills in the biological slots.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..schema import PartialDesign, PartialSample, ProvenancedValue, Source
from .base import make_pv, register

_pv = make_pv("zenodo", default_confidence=0.9)

API_BASE = "https://zenodo.org/api/records"
_ZENODO_DOI_RE = re.compile(r"^10\.5281/zenodo\.(\d+)$", re.IGNORECASE)
_ZENODO_URL_RE = re.compile(
    r"zenodo\.org/(?:record|records)/(\d+)",
    re.IGNORECASE,
)


def _extract_record_id(source: Source) -> str | None:
    if source.paper_doi:
        m = _ZENODO_DOI_RE.match(source.paper_doi.strip())
        if m:
            return m.group(1)
    if source.paper_url:
        m = _ZENODO_URL_RE.search(source.paper_url)
        if m:
            return m.group(1)
    repos = {r.lower() for r in source.repositories}
    if "zenodo" in repos:
        for acc in source.accessions:
            if acc.isdigit():
                return acc
    return None


def _fetch_record(record_id: str, cache_dir: Path | None) -> dict[str, Any]:
    """GET /api/records/<record_id>. Caches to ``cache_dir`` per record."""
    if cache_dir is not None:
        cached = cache_dir / f"zenodo_{record_id}.json"
        if cached.is_file():
            return json.loads(cached.read_text())

    import requests

    r = requests.get(f"{API_BASE}/{record_id}", timeout=30)
    r.raise_for_status()
    data = r.json()
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"zenodo_{record_id}.json").write_text(json.dumps(data))
    return data


class ZenodoExtractor:
    name = "zenodo"

    def can_handle(self, source: Source) -> float:
        if source.paper_doi and _ZENODO_DOI_RE.match(source.paper_doi.strip()):
            return 0.9
        if source.paper_url and _ZENODO_URL_RE.search(source.paper_url):
            return 0.9
        repos = {r.lower() for r in source.repositories}
        if "zenodo" in repos and any(a.isdigit() for a in source.accessions):
            return 0.9
        if "zenodo" in repos:
            return 0.5
        if source.paper_url and "zenodo.org" in source.paper_url:
            return 0.3
        return 0.0

    def extract(self, source: Source, cache_dir: Path) -> PartialDesign:
        record_id = _extract_record_id(source)
        if record_id is None:
            return PartialDesign(
                extractor=self.name,
                failures={
                    "record_id": (
                        "no Zenodo record id; pass paper_doi=10.5281/zenodo.<id>, "
                        "paper_url=https://zenodo.org/records/<id>, or "
                        "accessions=[<id>] with repositories=['Zenodo']"
                    ),
                },
            )

        cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            record = _fetch_record(record_id, cache_dir)
        except Exception as e:  # noqa: BLE001
            return PartialDesign(
                extractor=self.name,
                dataset_id=record_id,
                source=Source(
                    accessions=[record_id],
                    repositories=["Zenodo"],
                    paper_doi=f"10.5281/zenodo.{record_id}",
                ),
                failures={"api": f"{type(e).__name__}: {e}"},
            )

        return _build_partial(record, record_id)


def _build_partial(record: dict[str, Any], record_id: str) -> PartialDesign:
    failures: dict[str, str] = {}
    metadata = record.get("metadata") or {}

    sample = PartialSample(sample_id=record_id)
    sample.accession = _pv(record_id, "zenodo record id", confidence=1.0)

    if title := metadata.get("title"):
        sample.extra["title"] = _pv(str(title), "metadata.title")
    if keywords := metadata.get("keywords"):
        if isinstance(keywords, list):
            sample.extra["keywords"] = _pv("; ".join(str(k) for k in keywords), "metadata.keywords")
    if (lic := metadata.get("license")) and isinstance(lic, dict) and lic.get("id"):
        sample.extra["license"] = _pv(str(lic["id"]), "metadata.license.id")

    files = record.get("files") or []
    url_map: dict[str, list[str]] = {}
    if files:
        rel_paths: list[str] = []
        urls: list[str] = []
        for f in files:
            if not isinstance(f, dict):
                continue
            name = f.get("key") or f.get("filename")
            link = (f.get("links") or {}).get("self")
            if not (name and link):
                continue
            rel_paths.append(f"{record_id}/{name}")
            urls.append(str(link))
        if rel_paths:
            sample.files = ProvenancedValue(
                value=rel_paths,
                source="zenodo",
                confidence=0.95,
                evidence="files[*].links.self",
            )
            url_map[record_id] = urls
        else:
            failures["files"] = "zenodo record has no resolvable file URLs"
    else:
        failures["files"] = "zenodo record has no files (embargoed, restricted, or empty)"

    notes = str(title) if (title := metadata.get("title")) else None

    return PartialDesign(
        extractor="zenodo",
        dataset_id=record_id,
        source=Source(
            accessions=[record_id],
            repositories=["Zenodo"],
            paper_doi=f"10.5281/zenodo.{record_id}",
        ),
        samples=[sample],
        url_map=url_map,
        failures=failures,
        notes=notes,
    )


register(ZenodoExtractor())
