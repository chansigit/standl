"""hca-dcp — Human Cell Atlas Data Coordination Platform extractor.

Hits CZI's Azul service (``service.azul.data.humancellatlas.org``) to pull a
project's metadata + matrix file references. Each HCA "project" is a study
with a stable UUID — treated here as one logical dataset with one
``PartialSample`` keyed by the project UUID (per-donor splits are a skill
rescue step, same pattern as GEO/CxG pooled matrices).

Schema-peculiar bits handled here:

- ``donorOrganisms[0].genusSpecies`` / ``samples[0].organ`` / ``disease``
  etc. are arrays of plain strings, NOT ``{label, ontology_term_id}`` dicts
  like CxG. Ontology IDs would require a separate facets call we don't make.
- ``projects[0].contributedAnalyses`` is deeply nested
  (``genusSpecies → developmentStage → organ → libraryConstructionApproach``)
  with leaf lists of file records; we flatten and filter to
  ``contentDescription == Matrix`` and ``isIntermediate != True``.
- Matrix downloads go through Azul's ``/fetch/repository/files/{uuid}`` async
  indirection — returns JSON ``{Status, Location}``. ``standl.fetch.download``
  handles that shape transparently.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..schema import PartialDesign, PartialSample, ProvenancedValue, Source
from .base import make_pv, register

_pv = make_pv("hca-dcp", default_confidence=0.9)

API_BASE = "https://service.azul.data.humancellatlas.org"
DEFAULT_CATALOG = "dcp58"
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HCA_PROJECT_URL_RE = re.compile(
    r"data\.humancellatlas\.org/explore/projects/([0-9a-f-]{36})",
    re.IGNORECASE,
)


def _extract_project_uuid(source: Source) -> str | None:
    url = source.paper_url or ""
    m = _HCA_PROJECT_URL_RE.search(url)
    if m:
        return m.group(1).lower()
    repos = {r.lower() for r in source.repositories}
    if any(x in repos for x in ("hca", "humancellatlas", "hca-dcp")):
        for acc in source.accessions:
            if _UUID_RE.match(acc):
                return acc.lower()
    return None


def _fetch_project(uuid: str, cache_dir: Path | None, catalog: str | None = None) -> dict[str, Any]:
    """GET /index/projects/{uuid}?catalog=X. Caches per-uuid to ``cache_dir``
    so repeat extract()s within a run don't re-hit the API. Tests monkeypatch
    this function.
    """
    if cache_dir is not None:
        cached = cache_dir / f"hca_project_{uuid}.json"
        if cached.is_file():
            return json.loads(cached.read_text())

    import requests

    params = {"catalog": catalog or DEFAULT_CATALOG}
    r = requests.get(f"{API_BASE}/index/projects/{uuid}", params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"hca_project_{uuid}.json").write_text(json.dumps(data))
    return data


def _walk_matrix_files(tree: Any) -> list[dict[str, Any]]:
    """``contributedAnalyses`` has arbitrary-depth dict nesting with leaf
    lists of file records. Walk down, collect every list entry that has a
    ``uuid`` key."""
    out: list[dict[str, Any]] = []
    if isinstance(tree, list):
        for item in tree:
            if isinstance(item, dict) and "uuid" in item:
                out.append(item)
            else:
                out.extend(_walk_matrix_files(item))
    elif isinstance(tree, dict):
        for v in tree.values():
            out.extend(_walk_matrix_files(v))
    return out


def _first_str(items: Any) -> str | None:
    if isinstance(items, list):
        for it in items:
            if isinstance(it, str) and it:
                return it
            if isinstance(it, dict) and it.get("label"):
                return str(it["label"])
    return None


class HCADCPExtractor:
    name = "hca-dcp"

    def can_handle(self, source: Source) -> float:
        url = source.paper_url or ""
        if _HCA_PROJECT_URL_RE.search(url):
            return 0.9
        repos = {r.lower() for r in source.repositories}
        hca_repo = any(x in repos for x in ("hca", "humancellatlas", "hca-dcp"))
        if hca_repo and any(_UUID_RE.match(a) for a in source.accessions):
            return 0.9
        if hca_repo:
            return 0.5
        if "data.humancellatlas.org" in url:
            return 0.4
        return 0.0

    def extract(self, source: Source, cache_dir: Path) -> PartialDesign:
        uuid = _extract_project_uuid(source)
        if uuid is None:
            return PartialDesign(
                extractor=self.name,
                failures={
                    "project_id": (
                        "no HCA project UUID; pass an explore URL "
                        "(https://data.humancellatlas.org/explore/projects/<uuid>) "
                        "or accessions=[<uuid>] with repositories=['HCA']"
                    ),
                },
            )

        cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            record = _fetch_project(uuid, cache_dir)
        except Exception as e:  # noqa: BLE001
            return PartialDesign(
                extractor=self.name,
                dataset_id=uuid,
                source=Source(accessions=[uuid], repositories=["HCA"]),
                failures={"api": f"{type(e).__name__}: {e}"},
            )

        return _build_partial(record, uuid)


def _build_partial(record: dict[str, Any], uuid: str) -> PartialDesign:
    failures: dict[str, str] = {}

    projects = record.get("projects") or [{}]
    project = projects[0] if projects else {}
    donors = record.get("donorOrganisms") or [{}]
    donor = donors[0] if donors else {}
    protocols = record.get("protocols") or []
    samples = record.get("samples") or []
    sample0 = samples[0] if samples else {}

    # Top-level organism + assay.
    organism = _first_str(donor.get("genusSpecies"))
    organism_pv = _pv(organism, "donorOrganisms[0].genusSpecies[0]") if organism else None

    assay: str | None = None
    for proto in protocols:
        if isinstance(proto, dict):
            lca = _first_str(proto.get("libraryConstructionApproach"))
            if lca:
                assay = lca
                break
    assay_pv = _pv(assay, "protocols[*].libraryConstructionApproach") if assay else None

    # Sample (one per project).
    sample = PartialSample(sample_id=uuid)
    sample.accession = _pv(uuid, "entryId", confidence=1.0)
    if organism_pv:
        sample.organism = organism_pv

    if (organ := _first_str(sample0.get("organ"))):
        sample.tissue = _pv(organ, "samples[0].organ[0]")

    for donor_key, extra_key, evidence in (
        ("biologicalSex", "biologicalSex", "donorOrganisms[0].biologicalSex[0]"),
        ("disease", "disease", "donorOrganisms[0].disease[0]"),
        ("developmentStage", "developmentStage", "donorOrganisms[0].developmentStage[0]"),
    ):
        v = _first_str(donor.get(donor_key))
        if v:
            sample.extra[extra_key] = _pv(v, evidence)

    if (dc := donor.get("donorCount")) is not None:
        sample.extra["donor_count"] = _pv(str(dc), "donorOrganisms[0].donorCount")
    if (cc := project.get("estimatedCellCount")) is not None:
        sample.extra["cell_count"] = _pv(str(cc), "projects[0].estimatedCellCount")
    if (title := project.get("projectTitle")):
        sample.extra["title"] = _pv(str(title), "projects[0].projectTitle")
    if (short := project.get("projectShortname")):
        sample.extra["short_name"] = _pv(str(short), "projects[0].projectShortname")

    # Matrix files: walk contributedAnalyses, filter to non-intermediate Matrix.
    all_files = _walk_matrix_files(project.get("contributedAnalyses"))
    matrices = [
        f for f in all_files
        if not f.get("isIntermediate")
        and any("Matrix" in str(cd) for cd in (f.get("contentDescription") or []))
    ]

    url_map: dict[str, list[str]] = {}
    if matrices:
        rel_paths: list[str] = []
        urls: list[str] = []
        for m in matrices:
            file_uuid = m["uuid"]
            file_version = m.get("version", "")
            file_name = m.get("name") or f"{file_uuid}.h5"
            qs = f"catalog={DEFAULT_CATALOG}"
            if file_version:
                qs += f"&version={file_version}"
            rel_paths.append(f"{uuid}/{file_name}")
            urls.append(f"{API_BASE}/fetch/repository/files/{file_uuid}?{qs}")
        sample.files = ProvenancedValue(
            value=rel_paths,
            source="hca-dcp",
            confidence=0.9,
            evidence="contributedAnalyses.*.uuid (Matrix, !isIntermediate)",
        )
        url_map[uuid] = urls
    else:
        failures["matrices"] = "no non-intermediate Matrix entry under contributedAnalyses"

    # Project accessions (GEO/SRA/PRJNA cross-refs) → source.accessions.
    extra_accs: list[str] = []
    for pa in project.get("accessions") or []:
        if isinstance(pa, dict) and pa.get("accession"):
            extra_accs.append(str(pa["accession"]))

    paper_doi: str | None = None
    for pub in project.get("publications") or []:
        if isinstance(pub, dict) and pub.get("doi"):
            paper_doi = str(pub["doi"])
            break

    notes_parts: list[str] = []
    if title:
        notes_parts.append(str(title))

    return PartialDesign(
        extractor="hca-dcp",
        dataset_id=uuid,
        source=Source(
            accessions=[uuid] + extra_accs,
            repositories=["HCA"],
            paper_doi=paper_doi,
        ),
        organism=organism_pv,
        assay=assay_pv,
        samples=[sample],
        url_map=url_map,
        failures=failures,
        notes=" | ".join(notes_parts) or None,
    )


register(HCADCPExtractor())
