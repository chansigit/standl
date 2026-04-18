"""biostudies — EBI BioStudies (and legacy ArrayExpress) extractor.

BioStudies is the EBI deposit hub that absorbed ArrayExpress; studies
carry accessions like ``E-MTAB-<n>`` (historical ArrayExpress scRNA-seq),
``E-GEOD-<n>`` (auto-imports from GEO), ``E-CURD-<n>`` /
``E-HCAD-<n>`` / ``S-BIAD<n>`` / ``S-BSST<n>`` / ``S-SCDT-<...>`` (newer
BioStudies-native).

Two endpoints are needed per study:

- ``GET /api/v1/studies/<acc>`` — tree of attributes + subsections, carries
  Title / Organism / Study type etc. in ``section.attributes``.
- ``GET /api/v1/files/<acc>`` — flat file listing (``data[]``) with
  ``Name``, ``Section`` (raw-data / processed-data / metadata), ``Samples``,
  ``size``, and a ``path`` relative to the study's file root. Download URL
  is ``https://www.ebi.ac.uk/biostudies/files/<acc>/<path>``.

Per ``docs/roadmap.md`` ("Things intentionally deferred: SRA / fastq-level
downloads"), this extractor filters raw-sequencing formats (fastq, bam,
sra, cram, bai) out of ``sample.files`` / ``url_map`` — they're downstream
of the SRA/ENA pipeline, not standl. A study with nothing but raw files
emits a ``data_format`` failure, same shape as geo-soft's ``data_layout``
for pooled series.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..schema import PartialDesign, PartialSample, ProvenancedValue, Source
from .base import make_pv, register

_pv = make_pv("biostudies", default_confidence=0.9)

API_BASE = "https://www.ebi.ac.uk/biostudies/api/v1"
FILES_BASE = "https://www.ebi.ac.uk/biostudies/files"

# Accession prefix families we claim. BioStudies' own docs enumerate these —
# keep permissive to new prefixes (S-* and E-*).
_ACCESSION_RE = re.compile(r"^(E-[A-Z]+-[A-Z0-9]+(?:-\d+)?|S-[A-Z0-9]+(?:-[A-Z0-9]+)*)$")
_URL_RE = re.compile(
    r"ebi\.ac\.uk/(?:biostudies|arrayexpress)/(?:studies/)?([A-Z0-9\-]+)",
    re.IGNORECASE,
)

# SRA/ENA raw-sequencing formats. Anything else passes through.
_RAW_EXTENSIONS = {
    ".fastq", ".fastq.gz", ".fq", ".fq.gz",
    ".bam", ".bai", ".cram", ".crai", ".sra",
}


def _extract_accession(source: Source) -> str | None:
    for acc in source.accessions:
        if _ACCESSION_RE.match(acc):
            return acc
    if source.paper_url:
        m = _URL_RE.search(source.paper_url)
        if m and _ACCESSION_RE.match(m.group(1)):
            return m.group(1)
    return None


def _fetch_study(accession: str, cache_dir: Path | None) -> dict[str, Any]:
    """GET /api/v1/studies/<acc>. Caches to ``cache_dir``."""
    if cache_dir is not None:
        cached = cache_dir / f"biostudies_{accession}_study.json"
        if cached.is_file():
            return json.loads(cached.read_text())

    import requests

    r = requests.get(f"{API_BASE}/studies/{accession}", timeout=30)
    r.raise_for_status()
    data = r.json()
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"biostudies_{accession}_study.json").write_text(json.dumps(data))
    return data


def _fetch_files(accession: str, cache_dir: Path | None) -> dict[str, Any]:
    """GET /api/v1/files/<acc>. Pages with ``pageSize=10000`` to avoid
    pagination for any reasonably-sized study."""
    if cache_dir is not None:
        cached = cache_dir / f"biostudies_{accession}_files.json"
        if cached.is_file():
            return json.loads(cached.read_text())

    import requests

    r = requests.get(f"{API_BASE}/files/{accession}", params={"pageSize": 10000}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"biostudies_{accession}_files.json").write_text(json.dumps(data))
    return data


def _attrs_to_dict(attrs: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(attrs, list):
        return out
    for a in attrs:
        if isinstance(a, dict) and a.get("name"):
            v = a.get("value")
            if v is not None:
                out[str(a["name"]).strip().lower()] = str(v)
    return out


def _is_raw_only(name: str) -> bool:
    n = name.lower()
    # Check longest suffix first so `.fastq.gz` wins over `.gz`.
    for suffix in (".fastq.gz", ".fq.gz"):
        if n.endswith(suffix):
            return True
    for suffix in _RAW_EXTENSIONS:
        if n.endswith(suffix):
            return True
    return False


class BioStudiesExtractor:
    name = "biostudies"

    def can_handle(self, source: Source) -> float:
        if any(_ACCESSION_RE.match(a) for a in source.accessions):
            return 0.9
        if source.paper_url:
            if "ebi.ac.uk/biostudies" in source.paper_url or "ebi.ac.uk/arrayexpress" in source.paper_url:
                m = _URL_RE.search(source.paper_url)
                if m and _ACCESSION_RE.match(m.group(1)):
                    return 0.9
                return 0.4
        return 0.0

    def extract(self, source: Source, cache_dir: Path) -> PartialDesign:
        accession = _extract_accession(source)
        if accession is None:
            return PartialDesign(
                extractor=self.name,
                failures={
                    "accession": (
                        "no BioStudies/ArrayExpress accession; pass "
                        "accessions=[<E-MTAB-*|S-BIAD*|...>] or a "
                        "ebi.ac.uk/biostudies/studies/<accession> URL"
                    ),
                },
            )

        cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            study = _fetch_study(accession, cache_dir)
        except Exception as e:  # noqa: BLE001
            return PartialDesign(
                extractor=self.name,
                dataset_id=accession,
                source=Source(accessions=[accession], repositories=["BioStudies"]),
                failures={"api_study": f"{type(e).__name__}: {e}"},
            )
        try:
            files_resp = _fetch_files(accession, cache_dir)
        except Exception as e:  # noqa: BLE001
            return PartialDesign(
                extractor=self.name,
                dataset_id=accession,
                source=Source(accessions=[accession], repositories=["BioStudies"]),
                failures={"api_files": f"{type(e).__name__}: {e}"},
            )

        return _build_partial(study, files_resp, accession)


def _build_partial(
    study: dict[str, Any], files_resp: dict[str, Any], accession: str,
) -> PartialDesign:
    failures: dict[str, str] = {}

    section = study.get("section") or {}
    sec_attrs = _attrs_to_dict(section.get("attributes"))
    root_attrs = _attrs_to_dict(study.get("attributes"))
    merged = {**root_attrs, **sec_attrs}  # section wins on collisions

    organism_pv = None
    if (org := merged.get("organism")):
        organism_pv = _pv(org, "section.attributes.Organism")

    assay_pv = None
    if (study_type := merged.get("study type")):
        assay_pv = _pv(study_type, "section.attributes.'Study type'", confidence=0.6)

    sample = PartialSample(sample_id=accession)
    sample.accession = _pv(accession, "biostudies accession", confidence=1.0)
    if organism_pv:
        sample.organism = organism_pv

    for key_lc, extra_key in (
        ("title", "title"),
        ("study type", "study_type"),
        ("description", "description"),
        ("releasedate", "release_date"),
    ):
        v = merged.get(key_lc)
        if v:
            sample.extra[extra_key] = _pv(str(v)[:500], f"attributes.{extra_key}")

    all_data = files_resp.get("data") or []
    raw_count = 0
    processed_entries: list[dict[str, Any]] = []
    for f in all_data:
        if not isinstance(f, dict):
            continue
        name = f.get("path") or f.get("Name") or ""
        if not name:
            continue
        if _is_raw_only(name):
            raw_count += 1
            continue
        processed_entries.append(f)

    if raw_count:
        sample.extra["biostudies_raw_file_count"] = _pv(
            str(raw_count), "files.data (fastq/bam/sra excluded)",
        )

    url_map: dict[str, list[str]] = {}
    file_meta: dict[str, list[dict]] = {}
    if processed_entries:
        rel_paths: list[str] = []
        urls: list[str] = []
        metas: list[dict] = []
        for f in processed_entries:
            path = f.get("path") or f.get("Name")
            rel_paths.append(f"{accession}/{path}")
            urls.append(f"{FILES_BASE}/{accession}/{path}")
            # BioStudies exposes size but no checksum on /api/v1/files.
            meta: dict = {}
            if (sz := f.get("size")) is not None:
                try:
                    meta["size_bytes"] = int(sz)
                except (TypeError, ValueError):
                    pass
            metas.append(meta)
        sample.files = ProvenancedValue(
            value=rel_paths,
            source="biostudies",
            confidence=0.95,
            evidence="files.data[*].path (raw formats filtered)",
        )
        url_map[accession] = urls
        file_meta[accession] = metas
    elif all_data:
        failures["data_format"] = (
            f"only raw-sequencing formats available ({raw_count} fastq/bam/sra file(s)); "
            f"SRA/ENA processing is out of scope for standl. See ENA/SRA or a "
            f"processed-matrix deposit."
        )
    else:
        failures["files"] = "no files listed on BioStudies for this study"

    title = merged.get("title")

    return PartialDesign(
        extractor="biostudies",
        dataset_id=accession,
        source=Source(
            accessions=[accession],
            repositories=["BioStudies"],
        ),
        organism=organism_pv,
        assay=assay_pv,
        samples=[sample],
        url_map=url_map,
        file_meta=file_meta,
        failures=failures,
        notes=str(title) if title else None,
    )


register(BioStudiesExtractor())
