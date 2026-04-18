"""cellxgene-api — CELLxGENE Discover curation API extractor.

CZI's CELLxGENE Discover serves already-standardized h5ad files for ~2000
curated single-cell datasets. Each dataset is a self-contained study with
a stable UUID; per-cell metadata (donor_id, sex, disease, ...) is already
canonicalized inside the h5ad, so this extractor emits a single
``PartialSample`` with ``sample_id = dataset_id`` and leaves per-donor
splitting to the rescue flow (same pattern as pooled GEO series — see
``skills/standl/SKILL.md``).

API
---

Endpoint: ``https://api.cellxgene.cziscience.com/curation/v1/datasets`` —
full index (~2000 records, ~2 MB of JSON). We pull the whole thing once
per cache_dir, filter client-side. Per-dataset endpoints exist but require
the collection_id too, which the user typically doesn't have handy.

Response schema (relevant fields): ``dataset_id``, ``collection_id``,
``title``, ``citation``, ``assets`` (each ``{filetype, filesize, url}`` —
we prefer ``RAW_H5AD`` over ``H5AD`` when both are present), ``assay[]``,
``organism[]``, ``tissue[]``, ``cell_type[]``, ``disease[]``, ``sex[]``
(each ``{label, ontology_term_id}``), plus ``cell_count`` (int).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..schema import PartialDesign, PartialSample, ProvenancedValue, Source
from .base import make_pv, register

_pv = make_pv("cellxgene-api", default_confidence=0.9)

API_BASE = "https://api.cellxgene.cziscience.com"
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_EXPLORER_URL_RE = re.compile(
    r"cellxgene\.cziscience\.com/e/([0-9a-f-]{36})\.cxg",
    re.IGNORECASE,
)
_DATASET_URL_RE = re.compile(
    r"cellxgene\.cziscience\.com/datasets/([0-9a-f-]{36})",
    re.IGNORECASE,
)


def _extract_dataset_uuid(source: Source) -> str | None:
    """Try to pin down *which* CxG dataset the caller is asking about.

    Order: explorer URL > explicit dataset URL > UUID in accessions (only
    when accompanied by a CELLxGENE repository signal). A UUID by itself is
    ambiguous — HCA and Synapse also use UUIDs.
    """
    url = source.paper_url or ""
    m = _EXPLORER_URL_RE.search(url) or _DATASET_URL_RE.search(url)
    if m:
        return m.group(1).lower()

    repos = {r.lower() for r in source.repositories}
    if "cellxgene" in repos or "cxg" in repos:
        for acc in source.accessions:
            if _UUID_RE.match(acc):
                return acc.lower()
    return None


def _fetch_datasets_list(cache_dir: Path | None) -> list[dict[str, Any]]:
    """Pull the full CxG dataset index. Cache to ``cache_dir`` so repeated
    extract() calls during a single run don't re-hit the API.

    Tests monkeypatch this function.
    """
    if cache_dir is not None:
        cached = cache_dir / "cellxgene_datasets_index.json"
        if cached.is_file():
            return json.loads(cached.read_text())

    import requests

    r = requests.get(f"{API_BASE}/curation/v1/datasets", timeout=60)
    r.raise_for_status()
    data = r.json()
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "cellxgene_datasets_index.json").write_text(json.dumps(data))
    return data


def _pick_h5ad_asset(assets: list[dict[str, Any]]) -> str | None:
    """Prefer RAW_H5AD (integer counts) over H5AD (usually normalized).
    Filetype comes back uppercase in the current API; be tolerant anyway.
    """
    raw = next(
        (a for a in assets if a.get("filetype", "").upper() == "RAW_H5AD"),
        None,
    )
    if raw and raw.get("url"):
        return raw["url"]
    normal = next(
        (a for a in assets if a.get("filetype", "").upper() == "H5AD"),
        None,
    )
    if normal and normal.get("url"):
        return normal["url"]
    return None


def _labels(items: list[dict[str, Any]] | Any) -> list[str]:
    """Render a CxG controlled-vocab array into plain labels."""
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for it in items:
        if isinstance(it, dict) and "label" in it:
            out.append(str(it["label"]))
        elif it is not None:
            out.append(str(it))
    return out


class CellxGeneAPIExtractor:
    name = "cellxgene-api"

    def can_handle(self, source: Source) -> float:
        url = source.paper_url or ""
        if _EXPLORER_URL_RE.search(url) or _DATASET_URL_RE.search(url):
            return 0.9
        repos = {r.lower() for r in source.repositories}
        if "cellxgene" in repos or "cxg" in repos:
            if any(_UUID_RE.match(a) for a in source.accessions):
                return 0.9
            return 0.5
        if "cellxgene.cziscience.com" in url:
            # Collection URL or otherwise under the CxG host but without a
            # parseable dataset id — fire weakly so extract() can record a
            # clear failure instead of silently skipping.
            return 0.4
        return 0.0

    def extract(self, source: Source, cache_dir: Path) -> PartialDesign:
        dataset_id = _extract_dataset_uuid(source)
        if dataset_id is None:
            return PartialDesign(
                extractor=self.name,
                failures={
                    "dataset_id": (
                        "no CELLxGENE dataset UUID in source; pass an explorer URL "
                        "(https://cellxgene.cziscience.com/e/<uuid>.cxg/) or set "
                        "accessions=[<uuid>] with repositories=['CELLxGENE']"
                    )
                },
            )

        cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            index = _fetch_datasets_list(cache_dir)
        except Exception as e:  # noqa: BLE001
            return PartialDesign(
                extractor=self.name,
                dataset_id=dataset_id,
                source=Source(accessions=[dataset_id], repositories=["CELLxGENE"]),
                failures={"api": f"{type(e).__name__}: {e}"},
            )

        record = next(
            (r for r in index if str(r.get("dataset_id", "")).lower() == dataset_id),
            None,
        )
        if record is None:
            return PartialDesign(
                extractor=self.name,
                dataset_id=dataset_id,
                source=Source(accessions=[dataset_id], repositories=["CELLxGENE"]),
                failures={"dataset": f"{dataset_id} not found in CxG index (deprecated or private?)"},
            )

        return _build_partial(record, dataset_id)


def _build_partial(record: dict[str, Any], dataset_id: str) -> PartialDesign:
    failures: dict[str, str] = {}

    # Top-level fields from controlled-vocab arrays.
    organism_labels = _labels(record.get("organism", []))
    organism_pv = _pv(organism_labels[0], "organism[0].label") if organism_labels else None

    assay_labels = _labels(record.get("assay", []))
    assay_pv = _pv(assay_labels[0], "assay[0].label") if assay_labels else None

    # Single-sample: the whole dataset is one logical unit at extract time.
    # Per-donor splitting (if needed) is a skill rescue step.
    sample = PartialSample(sample_id=dataset_id)
    sample.accession = _pv(dataset_id, "dataset_id", confidence=1.0)

    if organism_pv:
        sample.organism = organism_pv

    tissues = record.get("tissue", [])
    if isinstance(tissues, list) and tissues:
        first = tissues[0] if isinstance(tissues[0], dict) else {}
        if first.get("label"):
            sample.tissue = _pv(str(first["label"]), "tissue[0].label")
        ot = first.get("ontology_term_id")
        if isinstance(ot, str) and ot.startswith("UBERON:"):
            sample.tissue_ontology = _pv(ot, "tissue[0].ontology_term_id")
        # CxG datasets from cell culture / organoid carry a tissue_type tag
        # that qualifies whether the tissue label is anatomical or cultured.
        if (tt := first.get("tissue_type")):
            sample.extra["tissue_type"] = _pv(str(tt), "tissue[0].tissue_type")

    for key in ("cell_type", "disease", "sex"):
        labels = _labels(record.get(key, []))
        if labels:
            sample.extra[key] = _pv("; ".join(labels), f"{key}[].label")

    if (cc := record.get("cell_count")) is not None:
        sample.extra["cell_count"] = _pv(str(cc), "cell_count", confidence=1.0)

    if (title := record.get("title")):
        sample.extra["title"] = _pv(str(title), "title")

    # Files + URL map.
    assets = record.get("assets", []) or []
    h5ad_url = _pick_h5ad_asset(assets)
    url_map: dict[str, list[str]] = {}
    if h5ad_url:
        basename = h5ad_url.rsplit("/", 1)[-1] or f"{dataset_id}.h5ad"
        sample.files = ProvenancedValue(
            value=[f"{dataset_id}/{basename}"],
            source="cellxgene-api",
            confidence=0.95,
            evidence="assets[*].url (H5AD/RAW_H5AD)",
        )
        url_map[dataset_id] = [h5ad_url]
    else:
        failures["assets"] = "no H5AD/RAW_H5AD asset in dataset record"

    notes_parts: list[str] = []
    if title:
        notes_parts.append(str(title))
    if citation := record.get("citation"):
        notes_parts.append(str(citation).split("\n", 1)[0])

    return PartialDesign(
        extractor="cellxgene-api",
        dataset_id=dataset_id,
        source=Source(
            accessions=[dataset_id],
            repositories=["CELLxGENE"],
            paper_doi=_doi_from_citation(record.get("citation")),
        ),
        organism=organism_pv,
        assay=assay_pv,
        samples=[sample],
        url_map=url_map,
        failures=failures,
        notes=" | ".join(notes_parts) or None,
    )


def _doi_from_citation(citation: Any) -> str | None:
    if not isinstance(citation, str):
        return None
    m = re.search(r"doi\.org/(10\.[^\s]+)", citation)
    return m.group(1) if m else None


register(CellxGeneAPIExtractor())
