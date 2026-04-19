"""cngbdb — China National GeneBank Database / CNSA (Sequence Archive).

CNGBdb (BGI, Shenzhen) hosts CNSA — the Chinese counterpart to NCBI SRA /
EBI ENA / NGDC GSA. Accession families:

- ``CNP`` — project (study-level landing)
- ``CNS`` — sample
- ``CNX`` — experiment
- ``CNR`` — run

URL pattern: ``https://db.cngb.org/search/project/<CNP>/``. The page is a
JavaScript SPA but the server-rendered ``<title>`` and
``<meta name="description">`` tags are populated, so a lightweight HTML
head scrape recovers title + abstract without headless browser support.
There is no documented unauthenticated per-project JSON API.

File downloads live under ``https://ftp.cngb.org/pub/CNSA/data<N>/<CNP>/``
where the ``data<N>`` shard (data1..data9) is non-deterministic — it must
be resolved from the JS-rendered page. First-ship strategy mirrors
``sciencedb-cn``: emit a study-level PartialSample with metadata + a
``files`` failure explaining the manual resolution step. Supplement with
DataCite lookup (some CNP projects are DOI-registered) for creators and
publication year.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..schema import PartialDesign, PartialSample, Source
from .base import make_pv, register

_pv = make_pv("cngbdb", default_confidence=0.9)

_ACCESSION_RE = re.compile(r"^CN[PXRSE]\d+$")
_PROJECT_RE = re.compile(r"^CNP\d+$")
_PAPER_URL_RE = re.compile(r"db\.cngb\.org/search/project/(CNP\d+)", re.I)

# <title>Spatiotemporal ... - Project - Data resources - CNGBdb</title>
_TITLE_RE = re.compile(r"<title>\s*(.*?)\s*</title>", re.IGNORECASE | re.DOTALL)
_DESC_RE = re.compile(
    r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']\s*/?>',
    re.IGNORECASE | re.DOTALL,
)
_CNS_IN_BODY_RE = re.compile(r"\bCNS\d{7,}\b")

DATACITE_BASE = "https://api.datacite.org/dois"
FTP_HINT = "https://ftp.cngb.org/pub/CNSA/"


# ---------- source dispatch ----------

def _extract_accession(source: Source) -> str | None:
    """Prefer CNP (project); fall back to any CN[XRSE] accession."""
    if source.paper_url:
        m = _PAPER_URL_RE.search(source.paper_url)
        if m:
            return m.group(1)
    # Prefer CNP over secondary types.
    cnp = next(
        (a for a in source.accessions if _PROJECT_RE.match(a)),
        None,
    )
    if cnp:
        return cnp
    for acc in source.accessions:
        if _ACCESSION_RE.match(acc):
            return acc
    return None


# ---------- fetchers (monkeypatchable) ----------

def _fetch_project_html(accession: str, cache_dir: Path | None) -> str:
    """Fetch the CNGBdb project landing HTML. Cached per-accession."""
    if cache_dir is not None:
        cached = cache_dir / f"cngbdb_{accession}.html"
        if cached.is_file():
            return cached.read_text(encoding="utf-8", errors="replace")

    import requests

    url = f"https://db.cngb.org/search/project/{accession}/"
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    html = r.text
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"cngbdb_{accession}.html").write_text(
            html, encoding="utf-8",
        )
    return html


def _fetch_datacite(accession: str, cache_dir: Path | None) -> dict[str, Any] | None:
    """Search DataCite for a DOI mentioning this CNGBdb accession. Returns
    the first matching DOI record, or None if nothing matches. Caches per
    accession.
    """
    if cache_dir is not None:
        cached = cache_dir / f"cngbdb_datacite_{accession}.json"
        if cached.is_file():
            return json.loads(cached.read_text())

    import requests

    r = requests.get(
        DATACITE_BASE,
        params={"query": f"cngbdb {accession}"},
        headers={"Accept": "application/vnd.api+json"},
        timeout=20,
    )
    r.raise_for_status()
    payload = r.json()
    data = payload.get("data") or []
    record = data[0] if data else None

    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"cngbdb_datacite_{accession}.json").write_text(
            json.dumps(record if record is not None else {})
        )
    return record


# ---------- HTML parsing ----------

def _parse_html(html: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if (m := _TITLE_RE.search(html)):
        raw = m.group(1).strip()
        # Strip CNGBdb suffix boilerplate if present.
        title = re.sub(
            r"\s*-\s*Project\s*-\s*Data resources\s*-\s*CNGBdb\s*$",
            "",
            raw,
            flags=re.IGNORECASE,
        ).strip()
        if title:
            out["title"] = title
    if (m := _DESC_RE.search(html)):
        desc = m.group(1).strip()
        if desc:
            out["description"] = desc
    cns = sorted(set(_CNS_IN_BODY_RE.findall(html)))
    if cns:
        out["samples_in_html"] = cns
    return out


# ---------- DataCite helpers (small subset) ----------

def _datacite_doi(record: dict[str, Any]) -> str | None:
    """Extract DOI string from a DataCite search-result record."""
    attrs = record.get("attributes") if isinstance(record, dict) else None
    if isinstance(attrs, dict):
        doi = attrs.get("doi")
        if doi:
            return str(doi)
    # Fallback for the single-DOI endpoint shape.
    return record.get("doi") if isinstance(record.get("doi"), str) else None


def _datacite_subjects(record: dict[str, Any]) -> list[str]:
    attrs = record.get("attributes") or record
    subs = attrs.get("subjects") if isinstance(attrs, dict) else None
    out: list[str] = []
    for s in subs or []:
        if isinstance(s, dict) and s.get("subject"):
            out.append(str(s["subject"]))
    return out


def _datacite_year(record: dict[str, Any]) -> str | None:
    attrs = record.get("attributes") or record
    y = attrs.get("publicationYear") if isinstance(attrs, dict) else None
    return str(y) if y else None


# Minimal taxon/species heuristic — DataCite subjects sometimes include
# "Homo sapiens" / "Mus musculus" strings. Kept conservative.
_ORGANISM_HINTS = {
    "homo sapiens": "Homo sapiens",
    "human": "Homo sapiens",
    "mus musculus": "Mus musculus",
    "mouse": "Mus musculus",
    "rattus norvegicus": "Rattus norvegicus",
    "rat": "Rattus norvegicus",
    "danio rerio": "Danio rerio",
    "zebrafish": "Danio rerio",
    "drosophila melanogaster": "Drosophila melanogaster",
}


def _organism_from_subjects(subjects: list[str]) -> str | None:
    for s in subjects:
        key = s.strip().lower()
        if key in _ORGANISM_HINTS:
            return _ORGANISM_HINTS[key]
    return None


# ---------- extractor ----------

class CNGBdbExtractor:
    name = "cngbdb"

    def can_handle(self, source: Source) -> float:
        if source.paper_url and _PAPER_URL_RE.search(source.paper_url):
            return 0.95
        repos_lower = {r.lower() for r in source.repositories}
        repo_hit = any(k in repos_lower for k in ("cngbdb", "cnsa", "cngb"))
        has_acc = any(_ACCESSION_RE.match(a) for a in source.accessions)
        if repo_hit and has_acc:
            return 0.85
        if has_acc and any(_PROJECT_RE.match(a) for a in source.accessions):
            return 0.7
        return 0.0

    def extract(self, source: Source, cache_dir: Path) -> PartialDesign:
        accession = _extract_accession(source)
        if accession is None:
            return PartialDesign(
                extractor=self.name,
                failures={
                    "accession": (
                        "no CNGBdb accession found; pass accessions=['CNP...'] "
                        "or paper_url=https://db.cngb.org/search/project/<CNP>/"
                    ),
                },
            )

        cache_dir.mkdir(parents=True, exist_ok=True)

        failures: dict[str, str] = {}
        parsed: dict[str, Any] = {}
        try:
            html = _fetch_project_html(accession, cache_dir)
        except Exception as e:  # noqa: BLE001
            html = ""
            failures["html_fetch"] = f"{type(e).__name__}: {e}"

        if html:
            parsed = _parse_html(html)
            if "title" not in parsed:
                failures["title"] = (
                    "could not parse <title> from CNGBdb project page; "
                    "page layout may have changed"
                )

        datacite_record: dict[str, Any] | None = None
        try:
            datacite_record = _fetch_datacite(accession, cache_dir)
        except Exception as e:  # noqa: BLE001
            failures["datacite"] = f"{type(e).__name__}: {e}"

        # Normalize empty-dict cache sentinel back to None.
        if isinstance(datacite_record, dict) and not datacite_record:
            datacite_record = None

        return _build_partial(
            accession=accession,
            parsed=parsed,
            datacite_record=datacite_record,
            failures=failures,
        )


def _build_partial(
    accession: str,
    parsed: dict[str, Any],
    datacite_record: dict[str, Any] | None,
    failures: dict[str, str],
) -> PartialDesign:
    doi = _datacite_doi(datacite_record) if datacite_record else None
    subjects = _datacite_subjects(datacite_record) if datacite_record else []
    year = _datacite_year(datacite_record) if datacite_record else None
    organism = _organism_from_subjects(subjects) if subjects else None

    organism_pv = (
        _pv(organism, f"datacite subjects → species lookup ({accession})")
        if organism else None
    )

    sample = PartialSample(sample_id=accession)
    sample.accession = _pv(accession, "cngbdb accession", confidence=1.0)
    if organism_pv:
        sample.organism = organism_pv

    if (desc := parsed.get("description")):
        # First sentence only for a compact sample-level abstract.
        first_sentence = re.split(r"(?<=[.!?])\s+", desc.strip(), maxsplit=1)[0]
        sample.extra["description"] = _pv(
            first_sentence, "cngbdb <meta name=description>",
        )

    if (cns_list := parsed.get("samples_in_html")):
        sample.extra["samples_in_html"] = _pv(
            "; ".join(cns_list[:50]),
            "CNS accessions found in CNGBdb project page body",
        )

    sample.extra["ftp_hint"] = _pv(
        FTP_HINT,
        "CNSA FTP root; actual shard (data1..data9) requires page JS to resolve",
    )

    if subjects:
        sample.extra["subjects"] = _pv(
            "; ".join(subjects[:10]), "datacite.subjects[].subject",
        )
    if year:
        sample.extra["publication_year"] = _pv(year, "datacite.publicationYear")

    # Files aren't auto-resolvable without JS; record the limitation.
    failures = dict(failures)
    failures.setdefault(
        "files",
        (
            f"CNGBdb/CNSA file URLs live under {FTP_HINT}data<N>/{accession}/ "
            "where the data<N> shard (data1..data9) is only exposed via the "
            "project page's JavaScript. Browse to "
            f"https://db.cngb.org/search/project/{accession}/ manually to "
            "find the shard, then fetch the FTP listing."
        ),
    )

    notes = parsed.get("title")

    return PartialDesign(
        extractor="cngbdb",
        dataset_id=accession,
        source=Source(
            accessions=[accession],
            repositories=["CNGBdb"],
            paper_doi=doi,
        ),
        organism=organism_pv,
        samples=[sample],
        failures=failures,
        notes=notes,
    )


register(CNGBdbExtractor())
