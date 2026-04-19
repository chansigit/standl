"""scp-broad — Broad Institute Single Cell Portal (SCP).

The Single Cell Portal at <https://singlecell.broadinstitute.org/single_cell>
is a Broad-hosted registry of single-cell studies, each with a stable
``SCP<digits>`` accession. Its public API has an asymmetric auth policy:

- ``GET /api/v1/search?type=study&terms=<accession>`` — works **without**
  auth and returns a compact study record (title, description, cell_count,
  gene_count, study_url, and a ``metadata`` dict with controlled-vocab
  lists for species / organ / disease / sex / library_preparation_protocol).
- ``/api/v1/studies/<acc>``, ``.../file_info``, ``.../manifest``,
  ``/api/v1/bulk_download/*`` — all return **401** (or 500 for bulk_download)
  without a bearer token. SCP's token flow requires a Google-SSO login,
  which standl doesn't do.

Consequence: this extractor is **metadata-only**. It emits a
single-PartialSample PartialDesign populated from the ``/search`` endpoint
and records a ``files`` failure pointing the user at the SCP web UI or
Terra to pull the actual data. The pattern mirrors how ``gsa-cncb`` handles
HRA (metadata known, data gated).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..schema import PartialDesign, PartialSample, Source
from .base import make_pv, register

_pv = make_pv("scp-broad", default_confidence=0.9)

API_BASE = "https://singlecell.broadinstitute.org/single_cell/api/v1"
WEB_BASE = "https://singlecell.broadinstitute.org"

_ACCESSION_RE = re.compile(r"^SCP\d+$")
_STUDY_URL_RE = re.compile(
    r"singlecell\.broadinstitute\.org/single_cell/study/(SCP\d+)",
    re.IGNORECASE,
)
_GENERIC_URL_RE = re.compile(
    r"singlecell\.broadinstitute\.org/.*?(SCP\d+)",
    re.IGNORECASE,
)


def _extract_accession(source: Source) -> str | None:
    url = source.paper_url or ""
    m = _STUDY_URL_RE.search(url) or _GENERIC_URL_RE.search(url)
    if m:
        return m.group(1).upper()
    for acc in source.accessions:
        if _ACCESSION_RE.match(acc):
            return acc
    return None


def _fetch_study(accession: str, cache_dir: Path | None) -> dict[str, Any]:
    """Query SCP's public search endpoint for a specific accession.

    Returns the raw JSON response (shape: ``{studies: [...], ...}``).
    Cached per-accession to ``cache_dir/scp_<acc>.json``. Tests monkeypatch
    this function.
    """
    if cache_dir is not None:
        cached = cache_dir / f"scp_{accession}.json"
        if cached.is_file():
            return json.loads(cached.read_text())

    import requests

    r = requests.get(
        f"{API_BASE}/search",
        params={"type": "study", "terms": accession},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"scp_{accession}.json").write_text(json.dumps(data))
    return data


def _first(values: Any) -> str | None:
    if isinstance(values, list) and values:
        v = values[0]
        if v is not None:
            return str(v)
    return None


def _first_sentence(desc: Any) -> str | None:
    if not isinstance(desc, str) or not desc.strip():
        return None
    # Strip HTML tags defensively — SCP descriptions sometimes carry them.
    text = re.sub(r"<[^>]+>", "", desc).strip()
    m = re.search(r"[^.!?]+[.!?]", text)
    return (m.group(0) if m else text).strip()[:300]


class ScpBroadExtractor:
    name = "scp-broad"

    def can_handle(self, source: Source) -> float:
        url = source.paper_url or ""
        if _STUDY_URL_RE.search(url):
            return 0.95
        if _GENERIC_URL_RE.search(url):
            return 0.9
        has_acc = any(_ACCESSION_RE.match(a) for a in source.accessions)
        repos = {r.lower() for r in source.repositories}
        if has_acc and (
            "scp" in repos or "broad scp" in repos or "singlecell" in repos
        ):
            return 0.8
        if has_acc:
            return 0.7
        return 0.0

    def extract(self, source: Source, cache_dir: Path) -> PartialDesign:
        accession = _extract_accession(source)
        if accession is None:
            return PartialDesign(
                extractor=self.name,
                failures={
                    "accession": (
                        "no SCP accession; pass accessions=['SCP<digits>'] or a "
                        "https://singlecell.broadinstitute.org/single_cell/study/SCP<digits> URL"
                    ),
                },
            )

        cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            data = _fetch_study(accession, cache_dir)
        except Exception as e:  # noqa: BLE001
            return PartialDesign(
                extractor=self.name,
                dataset_id=accession,
                source=Source(accessions=[accession], repositories=["SCP"]),
                failures={"api": f"{type(e).__name__}: {e}"},
            )

        studies = data.get("studies") or []
        record = next(
            (s for s in studies if str(s.get("accession", "")).upper() == accession),
            None,
        )
        if record is None:
            record = studies[0] if studies else None

        if record is None or record.get("detached") or record.get("public") is False:
            reason = (
                f"{accession} not found"
                if record is None
                else f"{accession} is "
                + ("detached" if record.get("detached") else "private")
            )
            return PartialDesign(
                extractor=self.name,
                dataset_id=accession,
                source=Source(accessions=[accession], repositories=["SCP"]),
                failures={"study": reason},
            )

        return _build_partial(record, accession)


def _build_partial(record: dict[str, Any], accession: str) -> PartialDesign:
    metadata = record.get("metadata") or {}

    species = _first(metadata.get("species"))
    lib_prep = _first(metadata.get("library_preparation_protocol"))
    organ = _first(metadata.get("organ"))
    disease = _first(metadata.get("disease"))
    sex = _first(metadata.get("sex"))

    organism_pv = _pv(species, "metadata.species[0]") if species else None
    assay_pv = _pv(lib_prep, "metadata.library_preparation_protocol[0]") if lib_prep else None

    sample = PartialSample(sample_id=accession)
    sample.accession = _pv(accession, "SCP accession", confidence=1.0)
    if organism_pv:
        sample.organism = organism_pv
    if organ:
        sample.tissue = _pv(organ, "metadata.organ[0]")
    if lib_prep:
        sample.extra["library_prep"] = _pv(lib_prep, "metadata.library_preparation_protocol[0]")
    if disease:
        sample.extra["disease"] = _pv(disease, "metadata.disease[0]")
    if sex:
        sample.extra["sex"] = _pv(sex, "metadata.sex[0]")
    if (cc := record.get("cell_count")) is not None:
        sample.extra["cell_count"] = _pv(str(cc), "cell_count", confidence=1.0)
    if (title := record.get("name")):
        sample.extra["title"] = _pv(str(title), "name")
    if (study_url := record.get("study_url")):
        full = study_url if str(study_url).startswith("http") else f"{WEB_BASE}{study_url}"
        sample.extra["study_url"] = _pv(full, "study_url")
        study_evidence = full
    else:
        study_evidence = f"{WEB_BASE}/single_cell/study/{accession}"

    failures = {
        "files": (
            "SCP file listing requires bearer token; download files manually via "
            f"SCP web UI ({study_evidence}) or Terra"
        ),
    }

    notes_parts: list[str] = []
    if title := record.get("name"):
        notes_parts.append(str(title))
    if (first_sent := _first_sentence(record.get("description"))):
        notes_parts.append(first_sent)
    notes = " | ".join(notes_parts) or None

    return PartialDesign(
        extractor="scp-broad",
        dataset_id=accession,
        source=Source(
            accessions=[accession],
            repositories=["SCP"],
            paper_doi=None,
        ),
        organism=organism_pv,
        assay=assay_pv,
        samples=[sample],
        failures=failures,
        notes=notes,
    )


register(ScpBroadExtractor())
